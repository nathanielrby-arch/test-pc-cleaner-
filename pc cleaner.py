from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import stat
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import customtkinter as ctk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PC Cleaner"
SETTINGS_FILE = APP_DIR / "settings.json"
SYSTEM_NAMES = {"windows", "program files", "program files (x86)", "system32", "boot"}
DEFAULT_SETTINGS = {
	"dark_mode": True,
	"auto_scan": False,
	"confirm_delete": True,
	"max_files": 250,
}


def format_size(value: int | float) -> str:
	units = ("B", "KB", "MB", "GB", "TB")
	amount = float(max(0, value))
	for unit in units:
		if amount < 1024 or unit == units[-1]:
			return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
		amount /= 1024
	return "0 B"


def format_date(timestamp: float) -> str:
	try:
		return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")
	except (OSError, ValueError):
		return "Unbekannt"


def load_settings() -> dict:
	try:
		with SETTINGS_FILE.open("r", encoding="utf-8") as file:
			data = json.load(file)
		return {**DEFAULT_SETTINGS, **data}
	except (OSError, json.JSONDecodeError):
		return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> None:
	APP_DIR.mkdir(parents=True, exist_ok=True)
	with SETTINGS_FILE.open("w", encoding="utf-8") as file:
		json.dump(settings, file, indent=2)


def windows_drives() -> list[str]:
	if os.name != "nt":
		return [str(Path.home().anchor or "/")]
	drives = []
	bitmask = ctypes.windll.kernel32.GetLogicalDrives()
	for index in range(26):
		if bitmask & (1 << index):
			drive = f"{chr(65 + index)}:\\"
			if os.path.exists(drive):
				drives.append(drive)
	return drives or ["C:\\"]


def is_protected_path(path: Path) -> bool:
	try:
		resolved = path.resolve(strict=False)
	except OSError:
		return True
	text = str(resolved).lower().rstrip("\\/")
	parts = {part.lower() for part in resolved.parts}
	windows_temp = (Path(os.environ.get("WINDIR", "C:\\Windows")) / "Temp").resolve()
	try:
		if resolved != windows_temp and windows_temp in resolved.parents:
			return False
	except OSError:
		return True
	if any(name in parts for name in SYSTEM_NAMES):
		return True
	for drive in windows_drives():
		root = str(Path(drive).resolve()).lower().rstrip("\\/")
		if text == root:
			return True
	return False


def iter_files(root: Path, limit: int | None = None) -> Iterable[Path]:
	stack = [root]
	visited: set[tuple[int, int]] = set()
	yielded = 0
	while stack:
		current = stack.pop()
		try:
			identity = (current.stat().st_dev, current.stat().st_ino)
			if identity in visited:
				continue
			visited.add(identity)
			entries = list(os.scandir(current))
		except (OSError, PermissionError):
			continue
		for entry in entries:
			try:
				if entry.is_symlink():
					continue
				if entry.is_dir(follow_symlinks=False):
					stack.append(Path(entry.path))
				elif entry.is_file(follow_symlinks=False):
					yield Path(entry.path)
					yielded += 1
					if limit and yielded >= limit:
						return
			except (OSError, PermissionError):
				continue


def scan_large_files(root: Path, minimum: int, max_files: int) -> list[dict]:
	results = []
	for path in iter_files(root):
		try:
			info = path.stat()
			if info.st_size >= minimum:
				results.append({
					"path": path,
					"name": path.name,
					"size": info.st_size,
					"date": format_date(info.st_mtime),
				})
		except (OSError, PermissionError):
			continue
	results.sort(key=lambda item: item["size"], reverse=True)
	return results[:max_files]


def folder_summary(root: Path) -> tuple[int, int, int]:
	total = files = folders = 0
	for path in iter_files(root):
		try:
			total += path.stat().st_size
			files += 1
		except (OSError, PermissionError):
			continue
	try:
		folders = sum(1 for item in root.rglob("*") if item.is_dir())
	except (OSError, PermissionError):
		folders = 0
	return total, files, folders


class PCCleaner(ctk.CTk):
	def __init__(self) -> None:
		super().__init__()
		self.title("PC Cleaner")
		self.geometry("1180x760")
		self.minsize(980, 650)
		self.settings = load_settings()
		self.results_queue: queue.Queue = queue.Queue()
		self.pages: dict[str, ctk.CTkFrame] = {}
		self.nav_buttons: dict[str, ctk.CTkButton] = {}
		self.current_page = "Übersicht"
		self.large_results: list[dict] = []
		self.temp_items: list[dict] = []
		self.analysis_running = False
		self._configure_theme()
		self._build_layout()
		self.show_page("Übersicht")
		if self.settings.get("auto_scan"):
			self.after(500, self.start_dashboard_scan)

	def _configure_theme(self) -> None:
		ctk.set_appearance_mode("Dark" if self.settings["dark_mode"] else "Light")
		ctk.set_default_color_theme("blue")
		self.colors = {
			"background": "#10141c",
			"sidebar": "#171d28",
			"panel": "#1d2532",
			"panel_alt": "#232d3b",
			"text": "#f3f6fb",
			"muted": "#9aa7b8",
			"accent": "#42c8a5",
			"accent_dark": "#24987b",
			"danger": "#e76f63",
		}
		self.configure(fg_color=self.colors["background"])

	def _build_layout(self) -> None:
		self.sidebar = ctk.CTkFrame(self, width=236, corner_radius=0, fg_color=self.colors["sidebar"])
		self.sidebar.grid(row=0, column=0, sticky="nsew")
		self.content = ctk.CTkFrame(self, corner_radius=0, fg_color=self.colors["background"])
		self.content.grid(row=0, column=1, sticky="nsew")
		self.grid_columnconfigure(1, weight=1)
		self.grid_rowconfigure(0, weight=1)
		self.content.grid_rowconfigure(0, weight=1)
		self.content.grid_columnconfigure(0, weight=1)

		ctk.CTkLabel(self.sidebar, text="PC Cleaner", font=ctk.CTkFont(size=24, weight="bold"), text_color=self.colors["text"]).pack(anchor="w", padx=25, pady=(32, 6))
		ctk.CTkLabel(self.sidebar, text="Lokale Speicherpflege", font=ctk.CTkFont(size=12), text_color=self.colors["muted"]).pack(anchor="w", padx=26, pady=(0, 35))
		for name, icon in [("Übersicht", "⌂"), ("Große Dateien", "▣"), ("Temporäre Dateien", "♲"), ("Speicheranalyse", "◫"), ("Einstellungen", "⚙")]:
			button = ctk.CTkButton(self.sidebar, text=f"  {icon}   {name}", anchor="w", height=44, corner_radius=10, fg_color="transparent", hover_color=self.colors["panel_alt"], text_color=self.colors["muted"], font=ctk.CTkFont(size=14), command=lambda item=name: self.show_page(item))
			button.pack(fill="x", padx=14, pady=3)
			self.nav_buttons[name] = button
		ctk.CTkLabel(self.sidebar, text="", height=1).pack(expand=True)
		ctk.CTkLabel(self.sidebar, text="Sicher und lokal\nKeine Uploads", justify="left", font=ctk.CTkFont(size=12), text_color=self.colors["muted"]).pack(anchor="w", padx=26, pady=(0, 24))

		self._create_dashboard()
		self._create_large_files()
		self._create_temp_page()
		self._create_storage_page()
		self._create_settings_page()

	def _page(self, name: str) -> ctk.CTkFrame:
		frame = ctk.CTkFrame(self.content, fg_color=self.colors["background"])
		frame.grid(row=0, column=0, sticky="nsew", padx=34, pady=30)
		self.pages[name] = frame
		return frame

	def _header(self, parent: ctk.CTkFrame, title: str, subtitle: str) -> None:
		ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=29, weight="bold"), text_color=self.colors["text"]).pack(anchor="w")
		ctk.CTkLabel(parent, text=subtitle, font=ctk.CTkFont(size=13), text_color=self.colors["muted"]).pack(anchor="w", pady=(5, 24))

	def _card(self, parent: ctk.CTkFrame, **kwargs) -> ctk.CTkFrame:
		return ctk.CTkFrame(parent, fg_color=self.colors["panel"], corner_radius=14, **kwargs)

	def _create_dashboard(self) -> None:
		page = self._page("Übersicht")
		self._header(page, "Übersicht", "Behalte den Speicherzustand deines PCs im Blick.")
		overview = self._card(page)
		overview.pack(fill="x", pady=(0, 18))
		overview.grid_columnconfigure(0, weight=1)
		ctk.CTkLabel(overview, text="SPEICHER", font=ctk.CTkFont(size=12, weight="bold"), text_color=self.colors["accent"]).grid(row=0, column=0, sticky="w", padx=24, pady=(22, 4))
		self.disk_label = ctk.CTkLabel(overview, text="Speicher wird ermittelt ...", font=ctk.CTkFont(size=20, weight="bold"), text_color=self.colors["text"])
		self.disk_label.grid(row=1, column=0, sticky="w", padx=24)
		self.disk_progress = ctk.CTkProgressBar(overview, height=12, corner_radius=6, progress_color=self.colors["accent"])
		self.disk_progress.grid(row=2, column=0, sticky="ew", padx=24, pady=(16, 8))
		self.disk_progress.set(0)
		self.disk_detail = ctk.CTkLabel(overview, text="Noch keine Analyse durchgeführt", font=ctk.CTkFont(size=13), text_color=self.colors["muted"])
		self.disk_detail.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 22))
		self.analyse_button = ctk.CTkButton(overview, text="PC analysieren", height=42, width=170, corner_radius=10, fg_color=self.colors["accent_dark"], hover_color=self.colors["accent"], command=self.start_dashboard_scan)
		self.analyse_button.grid(row=1, column=1, rowspan=3, padx=24)

		cards = ctk.CTkFrame(page, fg_color="transparent")
		cards.pack(fill="x")
		for column in range(3):
			cards.grid_columnconfigure(column, weight=1)
		self.dashboard_values: dict[str, ctk.CTkLabel] = {}
		for index, (title, icon, value) in enumerate((("Temporäre Dateien", "♲", "Nicht analysiert"), ("Große Dateien", "▣", "Nicht analysiert"), ("Speicher", "◫", "Nicht analysiert"))):
			card = self._card(cards)
			card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 7 if index < 2 else 0))
			ctk.CTkLabel(card, text=f"{icon}  {title}", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.colors["text"]).pack(anchor="w", padx=20, pady=(20, 10))
			value_label = ctk.CTkLabel(card, text=value, font=ctk.CTkFont(size=17, weight="bold"), text_color=self.colors["accent"])
			value_label.pack(anchor="w", padx=20, pady=(0, 20))
			self.dashboard_values[title] = value_label
		self.dashboard_status = ctk.CTkLabel(page, text="Bereit für eine lokale Analyse.", text_color=self.colors["muted"])
		self.dashboard_status.pack(anchor="w", pady=(20, 0))

	def _create_large_files(self) -> None:
		page = self._page("Große Dateien")
		self._header(page, "Große Dateien", "Finde große Dateien, bevor sie unnötig Platz belegen.")
		controls = self._card(page)
		controls.pack(fill="x", pady=(0, 14))
		ctk.CTkLabel(controls, text="Laufwerk", text_color=self.colors["muted"]).grid(row=0, column=0, padx=(20, 8), pady=18)
		self.drive_combo = ctk.CTkComboBox(controls, values=windows_drives(), width=130, state="readonly")
		self.drive_combo.set(windows_drives()[0])
		self.drive_combo.grid(row=0, column=1, pady=18)
		ctk.CTkLabel(controls, text="Mindestgröße", text_color=self.colors["muted"]).grid(row=0, column=2, padx=(25, 8))
		self.size_filter = ctk.CTkComboBox(controls, values=["Über 100 MB", "Über 500 MB", "Über 1 GB", "Über 5 GB"], width=145, state="readonly", command=lambda _: self._filter_large_results())
		self.size_filter.set("Über 100 MB")
		self.size_filter.grid(row=0, column=3)
		self.large_search = ctk.CTkEntry(controls, placeholder_text="Datei suchen ...", width=190)
		self.large_search.grid(row=0, column=4, padx=12)
		self.large_search.bind("<KeyRelease>", lambda _: self._filter_large_results())
		self.large_scan_button = ctk.CTkButton(controls, text="Analyse starten", width=140, command=self.start_large_scan)
		self.large_scan_button.grid(row=0, column=5, padx=(0, 20))
		table_frame = self._card(page)
		table_frame.pack(fill="both", expand=True)
		columns = ("name", "path", "size", "date")
		self.large_tree = self._tree(table_frame, columns, ("Dateiname", "Pfad", "Größe", "Änderungsdatum"), (180, 420, 100, 145))
		self.large_tree.bind("<Double-1>", self._open_selected_file)
		self.large_status = ctk.CTkLabel(page, text="Noch keine Analyse gestartet.", text_color=self.colors["muted"])
		self.large_status.pack(anchor="w", pady=(10, 0))
		delete = ctk.CTkButton(page, text="Ausgewählte Datei löschen", fg_color=self.colors["danger"], hover_color="#bd5148", command=self.delete_large_file)
		delete.pack(anchor="e", pady=(8, 0))

	def _tree(self, parent, columns, headings, widths):
		style = ttk.Style()
		style.theme_use("clam")
		style.configure("Treeview", background=self.colors["panel"], foreground=self.colors["text"], fieldbackground=self.colors["panel"], rowheight=34, borderwidth=0, font=("Segoe UI", 10))
		style.configure("Treeview.Heading", background=self.colors["panel_alt"], foreground=self.colors["muted"], relief="flat", font=("Segoe UI", 10, "bold"))
		style.map("Treeview", background=[("selected", self.colors["accent_dark"])], foreground=[("selected", "white")])
		tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
		for column, heading, width in zip(columns, headings, widths):
			tree.heading(column, text=heading, command=lambda c=column: self._sort_tree(tree, c))
			tree.column(column, width=width, anchor="w", stretch=column in ("path", "name"))
		tree.pack(side="left", fill="both", expand=True, padx=10, pady=10)
		scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
		scrollbar.pack(side="right", fill="y", pady=10)
		tree.configure(yscrollcommand=scrollbar.set)
		return tree

	def _create_temp_page(self) -> None:
		page = self._page("Temporäre Dateien")
		self._header(page, "Temporäre Dateien", "Entferne eindeutig temporäre Daten und behalte die Kontrolle.")
		self.temp_total = ctk.CTkLabel(page, text="Temporäre Dateien gefunden: Noch nicht analysiert", font=ctk.CTkFont(size=19, weight="bold"), text_color=self.colors["text"])
		self.temp_total.pack(anchor="w", pady=(0, 16))
		self.temp_list = ctk.CTkFrame(page, fg_color="transparent")
		self.temp_list.pack(fill="x")
		self.temp_status = ctk.CTkLabel(page, text="Die Analyse berücksichtigt nur eindeutig temporäre Ordner.", text_color=self.colors["muted"])
		self.temp_status.pack(anchor="w", pady=18)
		buttons = ctk.CTkFrame(page, fg_color="transparent")
		buttons.pack(fill="x")
		ctk.CTkButton(buttons, text="Alles auswählen", width=150, command=self.select_all_temp).pack(side="left")
		ctk.CTkButton(buttons, text="Auswahl löschen", width=150, fg_color=self.colors["danger"], hover_color="#bd5148", command=self.delete_temp).pack(side="right")
		self.after(200, self.start_temp_scan)

	def _create_storage_page(self) -> None:
		page = self._page("Speicheranalyse")
		self._header(page, "Speicheranalyse", "Erkenne die Ordner mit dem größten Speicherverbrauch.")
		controls = self._card(page)
		controls.pack(fill="x", pady=(0, 14))
		self.storage_path = ctk.CTkEntry(controls, placeholder_text="Laufwerk oder Ordner", width=440)
		self.storage_path.insert(0, windows_drives()[0])
		self.storage_path.grid(row=0, column=0, padx=20, pady=18)
		ctk.CTkButton(controls, text="Ordner wählen", width=120, command=self.choose_storage_folder).grid(row=0, column=1, padx=8)
		ctk.CTkButton(controls, text="Analyse starten", width=130, command=self.start_storage_scan).grid(row=0, column=2, padx=(0, 20))
		table_frame = self._card(page)
		table_frame.pack(fill="both", expand=True)
		self.storage_tree = self._tree(table_frame, ("folder", "size", "files", "folders"), ("Ordner", "Größe", "Dateien", "Unterordner"), (480, 120, 110, 130))
		self.storage_status = ctk.CTkLabel(page, text="Noch keine Analyse gestartet.", text_color=self.colors["muted"])
		self.storage_status.pack(anchor="w", pady=(10, 0))

	def _create_settings_page(self) -> None:
		page = self._page("Einstellungen")
		self._header(page, "Einstellungen", "Passe PC Cleaner an deine Arbeitsweise an.")
		card = self._card(page)
		card.pack(fill="x")
		self.dark_var = ctk.BooleanVar(value=self.settings["dark_mode"])
		self.auto_var = ctk.BooleanVar(value=self.settings["auto_scan"])
		self.confirm_var = ctk.BooleanVar(value=self.settings["confirm_delete"])
		self._setting_switch(card, "Dark Mode", "Dunkles Erscheinungsbild für längere Sitzungen.", self.dark_var, self.toggle_dark)
		self._setting_switch(card, "Beim Start automatisch analysieren", "Startet beim Öffnen eine Speicherübersicht.", self.auto_var, self.update_settings)
		self._setting_switch(card, "Bestätigungsdialog vor dem Löschen", "Zeigt vor jeder Löschaktion eine Sicherheitsabfrage.", self.confirm_var, self.update_settings)
		row = ctk.CTkFrame(card, fg_color="transparent")
		row.pack(fill="x", padx=22, pady=18)
		ctk.CTkLabel(row, text="Maximale Anzahl angezeigter Dateien", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.colors["text"]).pack(side="left")
		self.max_files_combo = ctk.CTkComboBox(row, values=["100", "250", "500", "1000"], width=95, state="readonly", command=self.update_settings)
		self.max_files_combo.set(str(self.settings["max_files"]))
		self.max_files_combo.pack(side="right")
		ctk.CTkLabel(page, text=f"Einstellungen werden lokal gespeichert unter:\n{SETTINGS_FILE}", justify="left", text_color=self.colors["muted"]).pack(anchor="w", pady=20)

	def _setting_switch(self, parent, title, subtitle, variable, command):
		row = ctk.CTkFrame(parent, fg_color="transparent")
		row.pack(fill="x", padx=22, pady=16)
		text = ctk.CTkFrame(row, fg_color="transparent")
		text.pack(side="left", fill="x", expand=True)
		ctk.CTkLabel(text, text=title, anchor="w", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.colors["text"]).pack(anchor="w")
		ctk.CTkLabel(text, text=subtitle, anchor="w", text_color=self.colors["muted"]).pack(anchor="w", pady=(3, 0))
		ctk.CTkSwitch(row, text="", variable=variable, command=command, progress_color=self.colors["accent"]).pack(side="right")

	def show_page(self, name: str) -> None:
		self.current_page = name
		for page_name, page in self.pages.items():
			if page_name == name:
				page.tkraise()
		for page_name, button in self.nav_buttons.items():
			selected = page_name == name
			button.configure(fg_color=self.colors["accent_dark"] if selected else "transparent", text_color="white" if selected else self.colors["muted"])

	def _run_async(self, work: Callable, done: Callable, status: ctk.CTkLabel | None = None) -> None:
		def worker():
			try:
				result = work()
				self.results_queue.put((done, result, None))
			except Exception as error:
				self.results_queue.put((done, None, error))
		threading.Thread(target=worker, daemon=True).start()
		self.after(100, self._poll_queue)
		if status:
			status.configure(text="Analyse läuft ...")

	def _poll_queue(self) -> None:
		try:
			callback, result, error = self.results_queue.get_nowait()
		except queue.Empty:
			self.after(100, self._poll_queue)
			return
		if error:
			messagebox.showerror("Analyse fehlgeschlagen", str(error))
		else:
			callback(result)

	def start_dashboard_scan(self) -> None:
		if self.analysis_running:
			return
		self.analysis_running = True
		self.analyse_button.configure(state="disabled", text="Analyse läuft ...")
		root = Path(windows_drives()[0])
		self._run_async(lambda: (shutil.disk_usage(root), self._temp_size()), self.finish_dashboard_scan, self.dashboard_status)

	def finish_dashboard_scan(self, data) -> None:
		usage, temp_size = data
		used = usage.total - usage.free
		percentage = used / usage.total if usage.total else 0
		self.disk_progress.set(percentage)
		self.disk_label.configure(text=f"{percentage * 100:.0f} % belegt")
		self.disk_detail.configure(text=f"Belegt: {format_size(used)}    |    Frei: {format_size(usage.free)}    |    Gesamt: {format_size(usage.total)}")
		self.dashboard_values["Temporäre Dateien"].configure(text=f"{format_size(temp_size)} gefunden")
		self.dashboard_values["Speicher"].configure(text=f"{format_size(used)} von {format_size(usage.total)}")
		self.dashboard_status.configure(text="Analyse abgeschlossen.")
		self.analyse_button.configure(state="normal", text="PC analysieren")
		self.analysis_running = False

	def _temp_size(self) -> int:
		total = 0
		for item in self._temp_locations():
			if item["path"].exists():
				for path in iter_files(item["path"]):
					try:
						total += path.stat().st_size
					except OSError:
						pass
		return total

	def _temp_locations(self) -> list[dict]:
		windows_temp = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Temp"
		user_temp = Path(tempfile.gettempdir())
		return [{"name": "Windows Temp", "path": windows_temp}, {"name": "Benutzer Temp", "path": user_temp}]

	def start_large_scan(self) -> None:
		self.large_scan_button.configure(state="disabled", text="Suche läuft ...")
		root = Path(self.drive_combo.get())
		minimum = self._minimum_size()
		self._run_async(lambda: scan_large_files(root, minimum, int(self.settings["max_files"])), self.finish_large_scan, self.large_status)

	def finish_large_scan(self, results) -> None:
		self.large_results = results
		self._filter_large_results()
		self.large_scan_button.configure(state="normal", text="Analyse starten")
		self.large_status.configure(text=f"{len(results)} große Dateien gefunden. Sortierung: größte zuerst.")
		self.dashboard_values["Große Dateien"].configure(text=f"{len(results)} gefunden")

	def _minimum_size(self) -> int:
		return {"Über 100 MB": 100 * 1024**2, "Über 500 MB": 500 * 1024**2, "Über 1 GB": 1024**3, "Über 5 GB": 5 * 1024**3}.get(self.size_filter.get(), 100 * 1024**2)

	def _filter_large_results(self) -> None:
		if not hasattr(self, "large_tree"):
			return
		self.large_tree.delete(*self.large_tree.get_children())
		query = self.large_search.get().lower().strip()
		for index, item in enumerate(self.large_results):
			if item["size"] < self._minimum_size() or (query and query not in str(item["name"]).lower() and query not in str(item["path"]).lower()):
				continue
			self.large_tree.insert("", "end", iid=str(index), values=(item["name"], str(item["path"]), format_size(item["size"]), item["date"]))

	def _sort_tree(self, tree, column: str) -> None:
		items = [(tree.set(item, column), item) for item in tree.get_children("")]
		items.sort(reverse=True, key=lambda value: value[0].lower())
		for index, (_, item) in enumerate(items):
			tree.move(item, "", index)

	def _open_selected_file(self, _event) -> None:
		selection = self.large_tree.selection()
		if selection:
			path = self.large_results[int(selection[0])]["path"]
			try:
				os.startfile(path.parent)
			except OSError as error:
				messagebox.showerror("Ordner konnte nicht geöffnet werden", str(error))

	def _delete_confirmed(self, paths: list[Path], title: str, skip_confirmation: bool = False) -> None:
		safe_paths = [path for path in paths if not is_protected_path(path) and path.exists()]
		if not safe_paths:
			messagebox.showwarning("Keine sichere Auswahl", "Die Auswahl enthält keine löschbaren Dateien.")
			return
		total = sum(path.stat().st_size for path in safe_paths if path.is_file())
		if self.settings["confirm_delete"] and not skip_confirmation and not messagebox.askyesno(title, f"Sollen {len(safe_paths)} Datei(en) mit {format_size(total)} gelöscht werden?\n\nSystem- und Programmordner werden immer geschützt."):
			return
		failed = []
		for path in safe_paths:
			try:
				if path.is_file() or path.is_symlink():
					path.unlink()
				elif path.is_dir():
					shutil.rmtree(path, onerror=self._remove_readonly)
			except (OSError, PermissionError):
				failed.append(str(path))
		message = f"{len(safe_paths) - len(failed)} Element(e) gelöscht."
		if failed:
			message += f"\n{len(failed)} Element(e) konnten nicht gelöscht werden."
		messagebox.showinfo("Bereinigung abgeschlossen", message)

	@staticmethod
	def _remove_readonly(func, path, _exc_info) -> None:
		try:
			os.chmod(path, stat.S_IWRITE)
			func(path)
		except OSError:
			pass

	def delete_large_file(self) -> None:
		selection = self.large_tree.selection()
		if not selection:
			messagebox.showinfo("Keine Auswahl", "Bitte wähle zuerst eine Datei aus.")
			return
		self._delete_confirmed([self.large_results[int(selection[0])]["path"]], "Datei sicher löschen")
		self.large_results.pop(int(selection[0]))
		self._filter_large_results()

	def start_temp_scan(self) -> None:
		self._run_async(self._scan_temp, self.finish_temp_scan, self.temp_status)

	def _scan_temp(self) -> list[dict]:
		items = []
		for entry in self._temp_locations():
			size = 0
			if entry["path"].exists():
				for file in iter_files(entry["path"]):
					try:
						size += file.stat().st_size
					except OSError:
						pass
			items.append({**entry, "size": size})
		items.append({"name": "Papierkorb", "path": None, "size": 0, "recycle": True})
		return items

	def finish_temp_scan(self, items) -> None:
		self.temp_items = items
		for widget in self.temp_list.winfo_children():
			widget.destroy()
		for item in items:
			row = self._card(self.temp_list)
			row.pack(fill="x", pady=4)
			item["selected"] = ctk.BooleanVar(value=False)
			ctk.CTkCheckBox(row, text=item["name"], variable=item["selected"], font=ctk.CTkFont(size=14, weight="bold"), text_color=self.colors["text"]).pack(side="left", padx=18, pady=15)
			label = format_size(item["size"]) if not item.get("recycle") else "über Windows verwalten"
			ctk.CTkLabel(row, text=label, text_color=self.colors["accent"]).pack(side="right", padx=18)
		total = sum(item["size"] for item in items)
		self.temp_total.configure(text=f"Temporäre Dateien gefunden: {format_size(total)}")
		self.temp_status.configure(text="Papierkorb wird aus Sicherheitsgründen über Windows geleert.")
		self.dashboard_values["Temporäre Dateien"].configure(text=f"{format_size(total)} gefunden")

	def select_all_temp(self) -> None:
		for item in self.temp_items:
			item["selected"].set(True)

	def delete_temp(self) -> None:
		selected = [item for item in self.temp_items if item["selected"].get()]
		paths = [item["path"] for item in selected if item.get("path")]
		recycle = any(item.get("recycle") for item in selected)
		if not selected:
			messagebox.showinfo("Keine Auswahl", "Bitte wähle mindestens eine Kategorie aus.")
			return
		if self.settings["confirm_delete"] and not messagebox.askyesno("Bereinigung bestätigen", f"Ausgewählte Kategorien löschen?\n\nDateien: {sum(item['size'] for item in selected):,} Bytes"):
			return
		temp_contents = []
		for path in paths:
			if path.exists() and path.is_dir():
				try:
					temp_contents.extend(path.iterdir())
				except (OSError, PermissionError):
					pass
		self._delete_confirmed(temp_contents, "Temporäre Dateien löschen", skip_confirmation=True) if temp_contents else None
		if recycle and os.name == "nt":
			result = ctypes.windll.shell32.SHEmptyRecycleBinW(self.winfo_id(), None, 0x00000001)
			if result != 0:
				messagebox.showwarning("Papierkorb", "Der Papierkorb konnte nicht vollständig geleert werden.")
		self.start_temp_scan()

	def choose_storage_folder(self) -> None:
		selected = filedialog.askdirectory(title="Ordner für Speicheranalyse auswählen")
		if selected:
			self.storage_path.delete(0, "end")
			self.storage_path.insert(0, selected)

	def start_storage_scan(self) -> None:
		root = Path(self.storage_path.get())
		if not root.is_dir():
			messagebox.showerror("Ungültiger Ordner", "Der ausgewählte Ordner ist nicht verfügbar.")
			return
		self._run_async(lambda: self._scan_storage(root), self.finish_storage_scan, self.storage_status)

	def _scan_storage(self, root: Path) -> list[dict]:
		results = []
		try:
			children = list(root.iterdir())
		except (OSError, PermissionError):
			return results
		for child in children:
			if not child.is_dir() or child.is_symlink():
				continue
			size, files, folders = folder_summary(child)
			results.append({"folder": str(child), "size": size, "files": files, "folders": folders})
		return sorted(results, key=lambda item: item["size"], reverse=True)

	def finish_storage_scan(self, results) -> None:
		self.storage_tree.delete(*self.storage_tree.get_children())
		for item in results:
			self.storage_tree.insert("", "end", values=(item["folder"], format_size(item["size"]), item["files"], item["folders"]))
		self.storage_status.configure(text=f"{len(results)} Ordner analysiert, größte zuerst sortiert.")

	def update_settings(self, _value=None) -> None:
		self.settings.update({"auto_scan": self.auto_var.get(), "confirm_delete": self.confirm_var.get(), "max_files": int(self.max_files_combo.get())})
		try:
			save_settings(self.settings)
		except OSError as error:
			messagebox.showerror("Einstellungen", f"Einstellungen konnten nicht gespeichert werden:\n{error}")

	def toggle_dark(self) -> None:
		self.settings["dark_mode"] = self.dark_var.get()
		save_settings(self.settings)
		ctk.set_appearance_mode("Dark" if self.dark_var.get() else "Light")


if __name__ == "__main__":
	app = PCCleaner()
	app.mainloop()
