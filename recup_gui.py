#!/usr/bin/env python3
"""
Récupérateur Android — interface graphique (lecture seule)

Analyse un téléphone Android connecté en USB (débogage USB) pour trouver
des fichiers récupérables (corbeille, miniatures, messagerie, cachés), puis
les copie vers le PC via adb pull. Ne modifie jamais rien sur le téléphone.
"""

from __future__ import annotations

import csv
import hashlib
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

APP_TITLE = "Récupérateur Android"
CHECKED = "☑"
UNCHECKED = "☐"

PHOTO_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".raw", ".dng", ".cr2", ".nef",
}
VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".3gp", ".webm", ".m4v", ".flv", ".wmv",
}
AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".amr", ".wma",
}
DOC_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".rtf", ".odt", ".csv", ".json", ".xml", ".html", ".htm", ".epub",
}

TRASH_HINTS = (
    "/.trash/", "/.trash", "/.recycle", "/recycle bin", "/.recyclebin",
    "/.trash-1000", "/.globaltrash", "/.trashbin",
    "/android/data/com.sec.android.gallery3d",
)
GPHOTOS_TRASH_HINTS = (
    "/com.google.android.apps.photos/trash",
    "/com.google.android.apps.photos/files/trash",
    "/.trash/com.google.android.apps.photos",
)
THUMB_HINTS = (
    "/.thumbnails", "/.thumbdata", "/thumbnails/", "/cache/thumb",
)
MSG_PKG_HINTS = (
    "com.whatsapp", "org.telegram.messenger", "org.thoughtcrime.securesms",
    "/whatsapp/", "/telegram/", "/signal/",
)
MSG_PATH_HINTS = (
    "/.statuses", "/statuses/",
    "/whatsapp images", "/whatsapp video", "/whatsapp audio",
    "/telegram images", "/telegram video", "/telegram audio",
    "/signal/attachments",
)
TEMP_EXTS = {".tmp", ".part", ".crdownload", ".download", ".partial"}
WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
PENDING_RE = re.compile(r"^\.pending-\d+-(.+)$", re.IGNORECASE)
TRASHED_RE = re.compile(r"^\.trashed-\d+-(.+)$", re.IGNORECASE)

SCAN_INACTIVITY_TIMEOUT = 120
SCAN_GLOBAL_TIMEOUT = 900
UI_BATCH_SIZE = 25


def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


ADB = resource_path("platform-tools/adb.exe") if os.name == "nt" else "adb"


def display_name(basename: str) -> str:
    m = PENDING_RE.match(basename) or TRASHED_RE.match(basename)
    if m:
        return m.group(1) or basename
    return basename


def categorize(path: str) -> str:
    name = display_name(os.path.basename(path))
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "Photos"
    if ext in VIDEO_EXT:
        return "Vidéos"
    if ext in AUDIO_EXT:
        return "Audio"
    if ext in DOC_EXT:
        return "Documents"
    return "Autres"


def _is_messaging_path(low: str) -> bool:
    if any(h in low for h in MSG_PKG_HINTS):
        return True
    if "/android/media/" in low and any(
        p in low for p in (
            "/android/media/com.whatsapp",
            "/android/media/org.telegram",
            "/android/media/org.thoughtcrime.securesms",
        )
    ):
        return True
    if any(h in low for h in MSG_PATH_HINTS):
        return True
    if "/sent/" in low or low.endswith("/sent"):
        if any(h in low for h in MSG_PKG_HINTS) or any(
            x in low for x in ("whatsapp", "telegram", "signal")
        ):
            return True
    return False


def tag_source(path: str) -> str:
    low = path.replace("\\", "/").lower()
    name = os.path.basename(low)
    # Messagerie avant miniatures
    if _is_messaging_path(low):
        return "Messagerie"
    if (
        name.startswith(".trashed-")
        or TRASHED_RE.match(os.path.basename(path))
        or any(h in low for h in TRASH_HINTS)
        or any(h in low for h in GPHOTOS_TRASH_HINTS)
    ):
        return "Corbeille"
    if any(h in low for h in THUMB_HINTS) or name.startswith(".thumbdata"):
        return "Miniature"
    if name.startswith(".") or os.path.splitext(name)[1].lower() in TEMP_EXTS:
        return "Caché"
    parts = low.split("/")
    if any(p.startswith(".") and p not in (".", "..") for p in parts[:-1]):
        return "Caché"
    return "Stockage"


def format_size(n: int | None) -> str:
    if n is None:
        return "?"
    units = ["o", "Ko", "Mo", "Go", "To"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            if u == "o":
                return f"{int(size)} {u}"
            return f"{size:.1f} {u}"
        size /= 1024
    return f"{n} o"


def _reserve_prefix(name: str) -> str:
    stem, _ext = os.path.splitext(name)
    if stem.upper() in WIN_RESERVED:
        return "_" + name
    return name


def sanitize_filename(name: str) -> str:
    name = name.replace("\x00", "")
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "_", name)
    name = name.rstrip(" .")
    name = _reserve_prefix(name)
    return name or "_"


def sanitize_rel_path(rel: str) -> str:
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        if not part or part in (".", ".."):
            continue
        parts.append(sanitize_filename(part))
    return "/".join(parts) if parts else "_"


def unique_dest_path(dest: str, used_lower: set[str]) -> str:
    directory, base = os.path.dirname(dest), os.path.basename(dest)
    stem, ext = os.path.splitext(base)
    candidate = dest
    n = 0
    while candidate.lower() in used_lower or os.path.exists(candidate):
        n += 1
        candidate = os.path.join(directory, f"{stem}_{n}{ext}")
    used_lower.add(candidate.lower())
    return candidate


def win_long_path(path: str) -> str:
    if os.name != "nt":
        return path
    ap = os.path.abspath(path)
    if len(ap) > 240 and not ap.startswith("\\\\?\\"):
        if ap.startswith("\\\\"):
            return "\\\\?\\UNC\\" + ap.lstrip("\\")
        return "\\\\?\\" + ap
    return path


def parse_find_line(line: str) -> dict | None:
    """Parse SIZE|MTIME|PATH. Ne rejette pas un fichier nommé Permission denied."""
    line = line.strip("\r\n")
    if not line or line.startswith("find:"):
        return None
    if "|" in line:
        parts = line.split("|", 2)
        if len(parts) != 3:
            return None
        size_s, mtime_s, path = parts
        try:
            size = int(size_s)
        except ValueError:
            size = None
        return {"size": size, "mtime": mtime_s.strip(), "path": path.strip()}
    m = re.match(
        r"^[-lbcsp].*?\s+(\d+)\s+(\d{4}-\d{2}-\d{2}|[A-Za-z]{3})\s+"
        r"([\d:]+|\d{1,2})\s+(.+)$",
        line,
    )
    if m:
        try:
            size = int(m.group(1))
        except ValueError:
            size = None
        return {
            "size": size,
            "mtime": f"{m.group(2)} {m.group(3)}",
            "path": m.group(4).strip(),
        }
    if line.startswith("/"):
        return {"size": None, "mtime": "", "path": line}
    return None


def parse_devices_output(stdout: str) -> list[tuple[str, str]]:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        out.append((parts[0], parts[1]))
    return out


def count_permission_denied(stderr: str) -> int:
    n = 0
    for line in (stderr or "").splitlines():
        if "Permission denied" in line or "permission denied" in line.lower():
            n += 1
    return n


def interesting_file(path: str) -> bool:
    name = os.path.basename(path)
    src = tag_source(path)
    if src in ("Corbeille", "Miniature", "Messagerie", "Caché"):
        return True
    if categorize(path) != "Autres":
        return True
    disp = display_name(name)
    if name.startswith(".") or os.path.splitext(disp)[1].lower() in TEMP_EXTS:
        return True
    return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def free_space_ok(directory: str, needed: int, margin: int = 50 * 1024 * 1024) -> tuple[bool, int]:
    free = shutil.disk_usage(directory).free
    return free >= needed + margin, free


def already_present(dest: str, size) -> bool:
    return size is not None and os.path.isfile(dest) and os.path.getsize(dest) == size


def make_item(parsed: dict) -> dict | None:
    path = parsed.get("path") or ""
    if not path or not interesting_file(path):
        return None
    base = os.path.basename(path)
    return {
        "path": path,
        "name": display_name(base),
        "size": parsed.get("size"),
        "mtime": parsed.get("mtime") or "",
        "type": categorize(path),
        "source": tag_source(path),
        "checked": False,
    }


class RecupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x720")
        self.minsize(900, 640)

        self.device_id = None
        self.device_model = None
        self.devices = []
        self.worker_thread = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._scan_proc: subprocess.Popen | None = None
        self._items: list[dict] = []
        self._iid_map: dict[str, dict] = {}
        self._preview_imgtk = None
        self._shown_count = 0
        self._checked_count = 0

        self._build_ui()
        self._check_tools_available()
        self.after(50, self._process_ui_queue)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        tk.Label(self, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(pady=(12, 2))
        tk.Label(
            self,
            text="Lecture seule : rien n'est modifié sur le téléphone. "
                 "Branchez l'appareil (débogage USB).",
            font=("Segoe UI", 9), fg="#555",
        ).pack(pady=(0, 8))

        top = tk.Frame(self)
        top.pack(fill="x", **pad)
        tk.Label(top, text="Statut :", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.status_label = tk.Label(top, text="Non détecté", fg="#b00")
        self.status_label.pack(side="left", padx=6)

        row = tk.Frame(self)
        row.pack(fill="x", **pad)
        self.detect_btn = tk.Button(row, text="Détecter", command=self.on_detect)
        self.detect_btn.pack(side="left")
        tk.Label(row, text="Appareil :", font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=(12, 4)
        )
        self.device_var = tk.StringVar(value="")
        self.device_combo = ttk.Combobox(
            row, textvariable=self.device_var, state="disabled", width=42
        )
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        actions = tk.Frame(self)
        actions.pack(fill="x", **pad)
        self.scan_btn = tk.Button(
            actions, text="Analyser (scan approfondi)", state="disabled",
            command=self.on_scan, bg="#2471a3", fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        self.scan_btn.pack(side="left", padx=(0, 6))
        self.cancel_btn = tk.Button(
            actions, text="Annuler", state="disabled", command=self.on_cancel
        )
        self.cancel_btn.pack(side="left", padx=(0, 6))
        self.recup_btn = tk.Button(
            actions, text="Récupérer la sélection", state="disabled",
            command=self.on_recover, bg="#1e8449", fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        self.recup_btn.pack(side="left", padx=(0, 6))
        self.sel_all_btn = tk.Button(
            actions, text="Tout sélectionner", state="disabled",
            command=lambda: self._set_all_checks(True),
        )
        self.sel_all_btn.pack(side="left", padx=(0, 4))
        self.sel_none_btn = tk.Button(
            actions, text="Tout désélectionner", state="disabled",
            command=lambda: self._set_all_checks(False),
        )
        self.sel_none_btn.pack(side="left")

        filt = tk.LabelFrame(self, text="Filtres")
        filt.pack(fill="x", **pad)
        ff = tk.Frame(filt)
        ff.pack(fill="x", padx=6, pady=4)
        tk.Label(ff, text="Catégorie :").pack(side="left")
        self.cat_var = tk.StringVar(value="Toutes")
        self.cat_combo = ttk.Combobox(
            ff, textvariable=self.cat_var, state="readonly", width=12,
            values=["Toutes", "Photos", "Vidéos", "Audio", "Documents", "Autres"],
        )
        self.cat_combo.pack(side="left", padx=4)
        self.cat_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_tree())
        tk.Label(ff, text="Source :").pack(side="left", padx=(10, 0))
        self.src_var = tk.StringVar(value="Toutes")
        self.src_combo = ttk.Combobox(
            ff, textvariable=self.src_var, state="readonly", width=12,
            values=["Toutes", "Corbeille", "Miniature", "Messagerie", "Caché", "Stockage"],
        )
        self.src_combo.pack(side="left", padx=4)
        self.src_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_tree())
        tk.Label(ff, text="Recherche :").pack(side="left", padx=(10, 0))
        self.search_var = tk.StringVar()
        self._search_after = None
        self.search_var.trace_add("write", lambda *_: self._debounce_search())
        tk.Entry(ff, textvariable=self.search_var, width=28).pack(side="left", padx=4)

        mid = tk.Frame(self)
        mid.pack(fill="both", expand=True, **pad)
        # Aperçu packé en premier pour ne jamais être écrasé par la liste
        preview_frame = tk.LabelFrame(mid, text="Aperçu", width=280)
        preview_frame.pack(side="right", fill="y", padx=(8, 0))
        preview_frame.pack_propagate(False)
        tree_frame = tk.Frame(mid)
        tree_frame.pack(side="left", fill="both", expand=True)
        cols = ("sel", "nom", "type", "taille", "date", "source", "chemin")
        self.tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="browse"
        )
        headings = {
            "sel": ("", 36), "nom": ("Nom", 160), "type": ("Type", 80),
            "taille": ("Taille", 70), "date": ("Date", 120),
            "source": ("Source", 90), "chemin": ("Chemin", 260),
        }
        for c, (text, w) in headings.items():
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, minwidth=30, stretch=(c in ("nom", "chemin")))
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        self.preview_label = tk.Label(
            preview_frame, text="Sélectionnez une image\n(Pillow optionnel)",
            fg="#666", wraplength=260, justify="center",
        )
        self.preview_label.pack(expand=True, fill="both", padx=6, pady=6)

        self.progress = ttk.Progressbar(self, mode="indeterminate", length=900)
        self.progress.pack(pady=4)
        self.count_label = tk.Label(self, text="0 fichier(s)", fg="#333")
        self.count_label.pack()

        log_frame = tk.LabelFrame(self, text="Journal")
        log_frame.pack(fill="x", **pad)
        self.log_text = tk.Text(
            log_frame, height=6, state="disabled", font=("Consolas", 9)
        )
        self.log_text.pack(fill="x", padx=4, pady=4)

    def destroy(self):
        self._cancel.set()
        self._kill_scan_proc()
        try:
            self._ui_queue = queue.Queue()
        except Exception:
            pass
        super().destroy()

    def _ui(self, func, *args, **kwargs):
        self._ui_queue.put((func, args, kwargs))

    def _process_ui_queue(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                func, args, kwargs = self._ui_queue.get_nowait()
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    self._log_now(f"Erreur interface : {e}")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(50, self._process_ui_queue)

    def _log_now(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def log(self, msg):
        self._ui(self._log_now, msg)

    def _set_widget(self, widget, **options):
        self._ui(widget.config, **options)

    def _set_status(self, text, color):
        self._set_widget(self.status_label, text=text, fg=color)

    def _on_tool_timeout(self):
        self.log(
            "ADB ne répond plus (délai dépassé) : opération arrêtée. "
            "Vérifiez le câble et le pilote USB."
        )
        self._set_status("Erreur : délai dépassé", "#b00")

    def _run(self, cmd, **kwargs):
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        kwargs.pop("text", None)
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        return subprocess.run(cmd, **kwargs)

    def _popen(self, cmd, **kwargs):
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        kwargs.pop("text", None)
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        return subprocess.Popen(cmd, **kwargs)

    def _kill_scan_proc(self):
        proc = self._scan_proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        self._scan_proc = None

    def _check_tools_available(self):
        if os.name == "nt":
            ok = all(
                os.path.isfile(resource_path(f"platform-tools/{n}"))
                for n in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
            )
        else:
            ok = shutil.which("adb") is not None
        if not ok:
            self.log(
                "ERREUR : adb introuvable. Placez 'platform-tools' à côté de l'exécutable."
            )
            self.detect_btn.config(state="disabled")

    def _get_prop(self, serial, prop):
        r = self._run(
            [ADB, "-s", serial, "shell", "getprop", prop],
            capture_output=True, check=False, timeout=15,
        )
        return (r.stdout or "").strip()

    def _adb_shell(self, serial, command, timeout=30):
        return self._run(
            [ADB, "-s", serial, "shell", command],
            capture_output=True, check=False, timeout=timeout,
        )

    def _list_devices(self):
        result = self._run(
            [ADB, "devices", "-l"],
            capture_output=True, check=False, timeout=15,
        )
        devices = []
        for serial, state in parse_devices_output(result.stdout or ""):
            model = ""
            if state == "device":
                model = self._get_prop(serial, "ro.product.model") or "Modèle inconnu"
            label = f"{model} ({serial})" if model else f"{serial} [{state}]"
            devices.append(
                {"serial": serial, "state": state, "model": model, "label": label}
            )
        return devices

    def _apply_devices(self, devices):
        self.devices = devices
        self.device_combo["values"] = [d["label"] for d in devices]
        if not devices:
            self.device_var.set("")
            self.device_combo.config(state="disabled")
            self.device_id = None
            self.device_model = None
            self.scan_btn.config(state="disabled")
            self.recup_btn.config(state="disabled")
            return
        pick = next((d for d in devices if d["state"] == "device"), devices[0])
        self.device_var.set(pick["label"])
        self.device_combo.config(state="readonly")
        self._select_device(pick)

    def _select_device(self, info):
        if info["state"] != "device":
            self.device_id = None
            self.device_model = None
            self.status_label.config(text=f"Statut: {info['state']}", fg="#b00")
            self.scan_btn.config(state="disabled")
            if info["state"] == "unauthorized":
                self.log("Débogage USB non autorisé. Acceptez la popup sur le téléphone.")
            return
        self.device_id = info["serial"]
        self.device_model = info["model"]
        self.status_label.config(
            text=f"Connecté — {info['model']} ({info['serial']})", fg="#1e8449",
        )
        self.scan_btn.config(state="normal")
        self.log(f"Appareil sélectionné : {info['model']} ({info['serial']})")

    def _on_device_selected(self, _event=None):
        label = self.device_var.get()
        for d in self.devices:
            if d["label"] == label:
                self._select_device(d)
                return

    def on_detect(self):
        self.log("Recherche d'appareils...")
        self.status_label.config(text="Recherche...", fg="#e67e22")
        self.scan_btn.config(state="disabled")
        self.device_combo.config(state="disabled")
        self.device_id = None
        self.device_model = None

        def task():
            try:
                devices = self._list_devices()
                if not devices:
                    self._ui(self._apply_devices, [])
                    self._set_status("Non détecté", "#b00")
                    self.log("Aucun appareil détecté.")
                    return
                self._ui(self._apply_devices, devices)
                self.log(f"{len(devices)} appareil(s) trouvé(s).")
            except subprocess.TimeoutExpired:
                self._on_tool_timeout()
            except Exception as e:
                self._set_status("Erreur", "#b00")
                self.log(f"Erreur : {e}")

        threading.Thread(target=task, daemon=True).start()

    def on_cancel(self):
        self._cancel.set()
        self._kill_scan_proc()
        self.log("Annulation demandée...")

    def on_scan(self):
        if not self.device_id:
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self._cancel.clear()
        self._items = []
        self._refresh_tree()
        self.scan_btn.config(state="disabled")
        self.recup_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.detect_btn.config(state="disabled")
        self.device_combo.config(state="disabled")
        self.progress.start(12)
        serial = self.device_id

        def task():
            partial = False
            try:
                items = self._scan_device(serial)
                if self._cancel.is_set():
                    self.log(
                        f"Analyse annulée — {len(items)} résultat(s) partiel(s) conservé(s)."
                    )
                    self._set_status(f"Annulé — {len(items)} fichier(s)", "#e67e22")
                else:
                    self.log(f"Analyse terminée : {len(items)} fichier(s) pertinents.")
                    self._set_status(f"Analyse OK — {len(items)} fichier(s)", "#1e8449")
                self._ui(self._set_items, items)
            except subprocess.TimeoutExpired:
                self._on_tool_timeout()
            except Exception as e:
                self.log(f"Erreur d'analyse : {e}")
                self._set_status("Erreur d'analyse", "#b00")
            finally:
                self._scan_proc = None
                self._ui(self.progress.stop)
                self._set_widget(self.scan_btn, state="normal")
                self._set_widget(self.cancel_btn, state="disabled")
                self._set_widget(self.detect_btn, state="normal")
                self._ui(self.device_combo.config, state="readonly")
                self._set_widget(self.sel_all_btn, state="normal")
                self._set_widget(self.sel_none_btn, state="normal")
                self._set_widget(self.recup_btn, state="normal")

        self.worker_thread = threading.Thread(target=task, daemon=True)
        self.worker_thread.start()

    def _resolve_sdcard(self, serial) -> str | None:
        r = self._adb_shell(serial, "readlink -f /sdcard", timeout=15)
        target = (r.stdout or "").strip().splitlines()
        if target and target[0].startswith("/"):
            return target[0].rstrip("/") + "/"
        # fallback
        r2 = self._adb_shell(serial, "ls /sdcard", timeout=15)
        if r2.returncode == 0 and not count_permission_denied(r2.stderr or ""):
            return "/sdcard/"
        return None

    def _scan_roots(self, serial) -> list[str]:
        """Racines uniques, /sdcard résolu, sans doublon emulated/0."""
        roots: list[str] = []
        sd = self._resolve_sdcard(serial)
        if sd:
            roots.append(sd if sd.endswith("/") else sd + "/")

        r = self._adb_shell(serial, "ls -1 /storage", timeout=20)
        denied = count_permission_denied(r.stderr or "")
        if denied:
            self.log(f"/storage : {denied} accès refusé (stderr).")
        sd_real = (sd or "").rstrip("/")
        for name in (r.stdout or "").splitlines():
            name = name.strip()
            if not name or name in ("self", "emulated", "."):
                continue
            cand = f"/storage/{name}"
            # Skip duplicate of resolved sdcard (typically /storage/emulated/0)
            if sd_real and cand.rstrip("/") == sd_real:
                continue
            if cand.rstrip("/") == "/storage/emulated/0" and sd_real in (
                "/storage/emulated/0", "/sdcard"
            ):
                # If sdcard unresolved symlink still points there via readlink
                if sd_real == "/storage/emulated/0":
                    continue
            roots.append(cand + "/")

        seen: set[str] = set()
        out: list[str] = []
        for root in roots:
            key = root.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(root if root.endswith("/") else root + "/")
        return out

    def _supports_printf(self, serial) -> bool:
        r = self._adb_shell(
            serial,
            "find /sdcard -maxdepth 0 -printf '%s\\n'",
            timeout=15,
        )
        out = (r.stdout or "").strip().splitlines()
        return bool(out and out[0].isdigit())

    def _append_batch(self, batch: list[dict]):
        if not batch:
            return
        self._items.extend(batch)
        self._refresh_tree()

    def _stream_find(self, serial, root: str, use_printf: bool) -> tuple[list[dict], int, bool]:
        """Stream find/ls. Returns (items, denied_count, timed_out_partial)."""
        qroot = shlex.quote(root)
        if use_printf:
            shell_cmd = f"find {qroot} -type f -printf '%s|%TY-%Tm-%Td %TH:%TM|%p\\n'"
        else:
            # Single streamed ls -lR (no per-file stat processes)
            shell_cmd = f"ls -lR {qroot}"
        cmd = [ADB, "-s", serial, "shell", shell_cmd]
        items: list[dict] = []
        batch: list[dict] = []
        denied = 0
        timed_out = False
        current_dir = root.rstrip("/")
        last_activity = time.time()
        started = time.time()

        proc = self._popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._scan_proc = proc
        assert proc.stdout is not None
        assert proc.stderr is not None

        # Drain stderr in background
        err_chunks: list[str] = []

        def _read_err():
            try:
                for eline in proc.stderr:
                    err_chunks.append(eline)
            except Exception:
                pass

        err_thread = threading.Thread(target=_read_err, daemon=True)
        err_thread.start()

        try:
            while True:
                if self._cancel.is_set():
                    proc.kill()
                    break
                now = time.time()
                if now - started > SCAN_GLOBAL_TIMEOUT:
                    timed_out = True
                    proc.kill()
                    self.log(
                        f"Délai global atteint sur {root} — "
                        f"résultats partiels conservés ({len(items) + len(batch)})."
                    )
                    break
                if now - last_activity > SCAN_INACTIVITY_TIMEOUT:
                    timed_out = True
                    proc.kill()
                    self.log(
                        f"Inactivité ADB sur {root} — "
                        f"résultats partiels conservés ({len(items) + len(batch)})."
                    )
                    break
                line = proc.stdout.readline()
                if line == "" and proc.poll() is not None:
                    break
                if line == "":
                    time.sleep(0.05)
                    continue
                last_activity = time.time()
                if use_printf:
                    parsed = parse_find_line(line)
                    if not parsed:
                        continue
                    it = make_item(parsed)
                    if it:
                        items.append(it)
                        batch.append(it)
                else:
                    # ls -lR
                    raw = line.rstrip("\r\n")
                    if raw.endswith(":") and (
                        raw.startswith("/") or raw.startswith(root.rstrip("/"))
                    ):
                        current_dir = raw[:-1]
                        continue
                    if raw.startswith("total "):
                        continue
                    parsed = parse_find_line(raw)
                    if not parsed:
                        continue
                    path = parsed["path"]
                    if not path.startswith("/"):
                        path = current_dir.rstrip("/") + "/" + path
                        parsed["path"] = path
                    it = make_item(parsed)
                    if it:
                        items.append(it)
                        batch.append(it)
                if len(batch) >= UI_BATCH_SIZE:
                    self._ui(self._append_batch, list(batch))
                    batch.clear()
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            err_thread.join(timeout=2)
            denied = count_permission_denied("".join(err_chunks))
            if batch:
                self._ui(self._append_batch, list(batch))
                batch.clear()
            self._scan_proc = None
        return items, denied, timed_out

    def _scan_device(self, serial) -> list[dict]:
        roots = self._scan_roots(serial)
        self.log(f"Racines à scanner : {', '.join(roots)}")
        use_printf = self._supports_printf(serial)
        self.log("Méthode : " + ("find -printf (flux)" if use_printf else "ls -lR (flux)"))

        found: dict[str, dict] = {}
        for root in roots:
            if self._cancel.is_set():
                break
            self.log(f"Scan de {root} ...")
            # Probe access (stdout/stderr separated)
            probe = self._adb_shell(serial, f"ls {shlex.quote(root.rstrip('/'))}", timeout=15)
            if probe.returncode != 0 or count_permission_denied(probe.stderr or ""):
                # also treat empty+error
                if "No such file" in (probe.stderr or "") or probe.returncode != 0:
                    self.log(f"Accès refusé ou introuvable : {root}")
                    continue

            items, denied, _partial = self._stream_find(serial, root, use_printf)
            if denied:
                self.log(
                    f"{root} : {denied} accès refusé (stderr, Android 11+) — ignorés."
                )
            for it in items:
                found[it["path"]] = it

        return sorted(
            found.values(), key=lambda x: (x["source"], x["type"], x["path"])
        )

    def _set_items(self, items):
        # Merge with progressive items already shown
        by_path = {it["path"]: it for it in self._items}
        for it in items:
            by_path[it["path"]] = it
        self._items = sorted(
            by_path.values(), key=lambda x: (x["source"], x["type"], x["path"])
        )
        self._refresh_tree()

    def _debounce_search(self):
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.after(150, self._run_search)

    def _run_search(self):
        self._search_after = None
        self._refresh_tree()

    def _update_count_label(self):
        self.count_label.config(
            text=f"{self._shown_count} affiché(s) / {len(self._items)} total — "
                 f"{self._checked_count} sélectionné(s)"
        )

    def _filtered_items(self):
        cat = self.cat_var.get()
        src = self.src_var.get()
        q = (self.search_var.get() or "").lower().strip()
        out = []
        for it in self._items:
            if cat != "Toutes" and it["type"] != cat:
                continue
            if src != "Toutes" and it["source"] != src:
                continue
            if q and q not in it["path"].lower() and q not in it["name"].lower():
                continue
            out.append(it)
        return out

    def _refresh_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._iid_map.clear()
        items = self._filtered_items()
        for i, it in enumerate(items):
            iid = f"i{i}"
            mark = CHECKED if it.get("checked") else UNCHECKED
            self.tree.insert(
                "", "end", iid=iid,
                values=(
                    mark, it["name"], it["type"], format_size(it.get("size")),
                    it.get("mtime") or "", it["source"], it["path"],
                ),
            )
            self._iid_map[iid] = it
        self._shown_count = len(items)
        self._checked_count = sum(1 for it in self._items if it.get("checked"))
        self._update_count_label()

    def _toggle_iid(self, iid):
        it = self._iid_map.get(iid)
        if not it:
            return
        it["checked"] = not it.get("checked")
        vals = list(self.tree.item(iid, "values"))
        vals[0] = CHECKED if it["checked"] else UNCHECKED
        self.tree.item(iid, values=vals)
        self._checked_count += 1 if it["checked"] else -1
        self._update_count_label()

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if iid and col == "#1":
            self._toggle_iid(iid)
            return "break"

    def _on_tree_space(self, _event=None):
        sel = self.tree.selection()
        if sel:
            self._toggle_iid(sel[0])
        return "break"

    def _set_all_checks(self, value: bool):
        for it in self._filtered_items():
            it["checked"] = value
        self._refresh_tree()

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self._iid_map.get(sel[0])
        if it:
            self._update_preview(it)

    def _update_preview(self, it):
        if not HAS_PIL:
            self.preview_label.config(
                image="", text="Aperçu indisponible\n(Pillow non installé)"
            )
            self._preview_imgtk = None
            return
        if it["type"] != "Photos":
            self.preview_label.config(image="", text=f"{it['type']}\n{it['name']}")
            self._preview_imgtk = None
            return
        if not self.device_id:
            return

        def task():
            tmp = os.path.join(
                os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                "recup_preview_" + sanitize_filename(it["name"]),
            )
            try:
                r = self._run(
                    [ADB, "-s", self.device_id, "pull", it["path"], tmp],
                    capture_output=True, check=False, timeout=60,
                )
                if r.returncode != 0 or not os.path.isfile(tmp):
                    try:
                        if os.path.isfile(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    self._ui(self.preview_label.config, image="", text="Aperçu impossible")
                    return
                img = Image.open(tmp)
                img.thumbnail((260, 260))
                imgtk = ImageTk.PhotoImage(img)

                def show():
                    self._preview_imgtk = imgtk
                    self.preview_label.config(image=imgtk, text="")

                self._ui(show)
            except Exception as e:
                self._ui(self.preview_label.config, image="", text=f"Aperçu : {e}")
            finally:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

        threading.Thread(target=task, daemon=True).start()

    def _default_docs(self) -> str:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        return docs if os.path.isdir(docs) else os.path.expanduser("~")

    @staticmethod
    def _row(it, dest, statut):
        digest = ""
        if statut in ("OK", "déjà présent") and os.path.isfile(dest):
            try:
                digest = sha256_file(dest)
            except Exception:
                digest = ""
        return {
            "nom": it["name"], "chemin_source": it["path"], "destination": dest,
            "taille": it.get("size") if it.get("size") is not None else "",
            "date": it.get("mtime") or "", "sha256": digest, "statut": statut,
        }

    def on_recover(self):
        selected = [it for it in self._items if it.get("checked")]
        if not selected:
            messagebox.showinfo("Récupération", "Aucun fichier sélectionné.", parent=self)
            return
        if not self.device_id:
            return
        base = filedialog.askdirectory(
            parent=self, title="Dossier de destination",
            initialdir=self._default_docs(),
        )
        if not base:
            return
        total = sum((it.get("size") or 0) for it in selected)
        try:
            ok_space, free = free_space_ok(base, total)
        except Exception:
            ok_space, free = True, -1
        if not ok_space:
            messagebox.showerror(
                "Espace insuffisant",
                f"Sélection : {format_size(total)}\nEspace libre : {format_size(free)}\n"
                "Libérez de l'espace ou choisissez un autre dossier.",
                parent=self,
            )
            return
        self.log(f"Sélection : {len(selected)} fichier(s), {format_size(total)}.")
        safe_model = sanitize_filename(self.device_model or "Android")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_root = os.path.join(base, f"Récupéré_{safe_model}_{stamp}")
        try:
            os.makedirs(dest_root, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible de créer le dossier :\n{e}", parent=self)
            return

        self.recup_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._cancel.clear()
        self.progress.start(12)
        serial = self.device_id
        model = self.device_model or ""

        def task():
            ok = fail = skipped = 0
            failures = []
            rows = []
            used_lower: set[str] = set()
            try:
                for it in selected:
                    if self._cancel.is_set():
                        self.log("Récupération annulée.")
                        break
                    rel = sanitize_rel_path(it["path"].lstrip("/"))
                    dest = os.path.join(dest_root, *rel.split("/"))
                    if already_present(dest, it.get("size")):
                        skipped += 1
                        used_lower.add(dest.lower())
                        rows.append(self._row(it, dest, "déjà présent"))
                        continue
                    dest = unique_dest_path(dest, used_lower)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    pull_dest = win_long_path(dest)
                    size = it.get("size") or 0
                    timeout = max(30, min(1800, int(size / (1024 * 1024)) + 60))
                    self.log(f"Pull : {it['path']}")
                    try:
                        r = self._run(
                            [ADB, "-s", serial, "pull", "-a", it["path"], pull_dest],
                            capture_output=True, check=False, timeout=timeout,
                        )
                        if r.returncode == 0 and os.path.isfile(dest):
                            ok += 1
                            rows.append(self._row(it, dest, "OK"))
                        else:
                            rows.append(self._row(it, dest, "échec"))
                            fail += 1
                            err = (r.stderr or r.stdout or f"code {r.returncode}").strip()
                            failures.append(f"{it['path']} → {err}")
                            self.log(f"Échec : {it['path']} ({err[:120]})")
                            try:
                                if os.path.isfile(dest):
                                    os.remove(dest)
                            except Exception:
                                pass
                    except subprocess.TimeoutExpired:
                        fail += 1
                        failures.append(f"{it['path']} → délai dépassé")
                        rows.append(self._row(it, dest, "délai dépassé"))
                        self.log(f"Délai dépassé : {it['path']}")
                        try:
                            if os.path.isfile(dest):
                                os.remove(dest)
                        except Exception:
                            pass
                    except Exception as e:
                        fail += 1
                        failures.append(f"{it['path']} → {e}")
                        rows.append(self._row(it, dest, "erreur"))
                        self.log(f"Erreur : {it['path']} — {e}")
                        try:
                            if os.path.isfile(dest):
                                os.remove(dest)
                        except Exception:
                            pass

                report = os.path.join(dest_root, "rapport_recuperation.txt")
                try:
                    lines = [
                        "Rapport — Récupérateur Android",
                        f"Date/heure : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        f"Modèle     : {model}",
                        f"Série      : {serial}",
                        f"Destination: {dest_root}",
                        f"Sélectionnés : {len(selected)}",
                        f"Réussis      : {ok}",
                        f"Échecs       : {fail}",
                        f"Déjà présents: {skipped}",
                        "",
                        "Fichiers (statut | sha256 | source) :",
                    ]
                    lines += [f"{x['statut']} | {x['sha256'] or '-'} | {x['chemin_source']}" for x in rows]
                    lines.append("")
                    if failures:
                        lines.append("Détail des échecs :")
                        lines.extend(failures)
                        lines.append("")
                    with open(report, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
                    self.log(f"Rapport : {report}")
                    csv_path = os.path.join(dest_root, "rapport_recuperation.csv")
                    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=[
                            "nom", "chemin_source", "destination", "taille",
                            "date", "sha256", "statut",
                        ], delimiter=";")
                        w.writeheader()
                        w.writerows(rows)
                    self.log(f"CSV : {csv_path}")
                except Exception as e:
                    self.log(f"Impossible d'écrire le rapport : {e}")

                self.log(f"Terminé : {ok} réussi(s), {skipped} déjà présent(s), {fail} échec(s).")
                self._set_status(
                    f"Récupération : {ok} OK / {fail} échec(s)",
                    "#1e8449" if ok else "#b00",
                )
            except subprocess.TimeoutExpired:
                self._on_tool_timeout()
            except Exception as e:
                self.log(f"Erreur de récupération : {e}")
                self._set_status("Erreur de récupération", "#b00")
            finally:
                self._ui(self.progress.stop)
                self._set_widget(self.recup_btn, state="normal")
                self._set_widget(self.scan_btn, state="normal")
                self._set_widget(self.cancel_btn, state="disabled")

        self.worker_thread = threading.Thread(target=task, daemon=True)
        self.worker_thread.start()


if __name__ == "__main__":
    app = RecupApp()
    app.mainloop()
