#!/usr/bin/env python3
"""Smoke tests self-contained (faux adb dans tempdir, chemins relatifs)."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_FAKE_DIR = Path(tempfile.mkdtemp(prefix="recup_fake_adb_"))
_FAKE_LOG = _FAKE_DIR / "adb.log"
_FAKE_ADB = _FAKE_DIR / "adb"

_FAKE_ADB.write_text(
    r'''#!/bin/sh
LOG="${FAKE_LOG:-/tmp/adb.log}"
echo "ADB $*" >> "$LOG"

if [ -n "$FAKE_HANG" ]; then
  case "$*" in *"devices"*|*"getprop"*|*"find"*) sleep "$FAKE_HANG";; esac
fi

if echo "$*" | grep -q ' pull '; then
  remote=$(printf '%s\n' "$*" | awk '{for(i=1;i<=NF;i++) if($i=="pull"){j=i+1; if($j=="-a")j++; print $j; exit}}')
  localp=$(printf '%s\n' "$*" | awk '{for(i=1;i<=NF;i++) if($i=="pull"){j=i+1; if($j=="-a")j++; print $(j+1); exit}}')
  if [ -n "$FAKE_PULL_FAIL" ] && echo "$remote" | grep -q "$FAKE_PULL_FAIL"; then
    echo "error: failed" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$localp")" 2>/dev/null || true
  printf 'FAKE:%s\n' "$remote" > "$localp"
  echo "1 file pulled"
  exit 0
fi

case "$*" in
  "devices -l")
    printf 'List of devices attached\n* daemon started successfully\nSERIAL1             device product:p1 model:Pixel_5 device:redfin\nSERIAL2             device product:p2 model:Galaxy_S21 device:o1s\n'
    ;;
  *"-s SERIAL1 shell getprop ro.product.model"*) echo "Pixel_5" ;;
  *"-s SERIAL2 shell getprop ro.product.model"*) echo "Galaxy_S21" ;;
  *"readlink -f /sdcard"*) echo "/storage/emulated/0" ;;
  *"shell ls -1 /storage"*) printf 'emulated\nXXXX-SDCARD\nself\n' ;;
  *"find /sdcard -maxdepth 0 -printf"*|*"find /storage/emulated/0 -maxdepth 0 -printf"*)
    echo "0"
    ;;
  *"shell ls /storage/emulated/0"*|*"shell ls /sdcard"*|*"shell ls /storage/XXXX-SDCARD"*)
    echo "DCIM"
    ;;
  *"find /storage/emulated/0/ -type f -printf"*|*"find /sdcard/ -type f -printf"*|*"find /storage/emulated/0 -type f -printf"*|*"find /sdcard -type f -printf"*)
    cat <<'FIND'
4096|2024-01-15 10:00|/storage/emulated/0/DCIM/Camera/IMG_001.jpg
2048|2024-02-01 11:00|/storage/emulated/0/.Trash/photo_old.png
1024|2024-03-01 12:00|/storage/emulated/0/.thumbnails/thumb123.jpg
8192|2024-04-01 13:00|/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-2024.jpg
512|2024-05-01 14:00|/storage/emulated/0/Download/file.part
256|2024-05-02 15:00|/storage/emulated/0/.hidden_note.txt
100|2024-05-03 16:00|/storage/emulated/0/Download/Permission denied.txt
200|2024-05-04 17:00|/storage/emulated/0/DCIM/.trashed-1700000000-old.jpg
16384|2024-06-01 16:00|/storage/emulated/0/Movies/clip.mp4
FIND
    # permission denied on stderr only
    echo "find: /storage/emulated/0/Android/data: Permission denied" >&2
    ;;
  *"find /storage/XXXX-SDCARD"*)
    cat <<'FIND'
3000|2024-07-01 17:00|/storage/XXXX-SDCARD/DCIM/sd_pic.jpg
1500|2024-07-02 18:00|/storage/XXXX-SDCARD/.Trash-1000/files/old.doc
FIND
    ;;
esac
exit 0
''',
    encoding="utf-8",
)
_FAKE_ADB.chmod(0o755)

os.environ["PATH"] = str(_FAKE_DIR) + os.pathsep + os.environ.get("PATH", "")
os.environ["FAKE_LOG"] = str(_FAKE_LOG)
os.environ.pop("FAKE_HANG", None)
os.environ.pop("FAKE_PULL_FAIL", None)

import recup_gui as r

_REAL_SLEEP = time.sleep


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            app.update()
        except Exception:
            break
        _REAL_SLEEP(0.02)


def clear_log():
    _FAKE_LOG.write_text("", encoding="utf-8")


def read_log():
    return _FAKE_LOG.read_text(encoding="utf-8") if _FAKE_LOG.exists() else ""


def make_app():
    errs = []
    app = r.RecupApp()
    app.report_callback_exception = lambda *a: errs.append(a)
    return app, errs


def safe_destroy(app):
    t = getattr(app, "worker_thread", None)
    deadline = time.time() + 20
    while t is not None and t.is_alive() and time.time() < deadline:
        try:
            app.update()
        except Exception:
            break
        _REAL_SLEEP(0.02)
    if t is not None:
        t.join(timeout=1)
    try:
        pump(app, 0.15)
    except Exception:
        pass
    try:
        app.destroy()
    except Exception:
        pass


def wait_worker(app, timeout=25):
    t = app.worker_thread
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            app.update()
        except Exception:
            break
        if t is None or not t.is_alive():
            pump(app, 0.3)
            return
        _REAL_SLEEP(0.02)
    pump(app, 0.3)


def test_detect_scan_pull():
    clear_log()
    app, errs = make_app()
    tmp_base = tempfile.mkdtemp(prefix="recup_out_")
    try:
        app.on_detect()
        pump(app, 1.5)
        assert app.device_id == "SERIAL1"
        assert len(app.devices) == 2
        app.on_scan()
        wait_worker(app, 20)
        assert app._items, "expected items"
        sources = {it["source"] for it in app._items}
        assert "Messagerie" in sources
        assert "Corbeille" in sources
        # Permission denied.txt kept
        assert any(it["path"].endswith("Permission denied.txt") for it in app._items)
        # trashed display name
        assert any(it["name"] == "old.jpg" for it in app._items)
        journal = app.log_text.get("1.0", "end").lower()
        assert "accès refusé" in journal or "permission" in read_log().lower()
        for it in app._items[:2]:
            it["checked"] = True
        app._refresh_tree()
        r.filedialog.askdirectory = lambda **k: tmp_base
        app.on_recover()
        wait_worker(app, 15)
        dirs = glob.glob(os.path.join(tmp_base, "Récupéré_*"))
        assert dirs
        report = os.path.join(dirs[0], "rapport_recuperation.txt")
        assert os.path.isfile(report)
        body = open(report, encoding="utf-8").read()
        assert "Réussis" in body and "Déjà présents" in body
        import csv as _csv
        rows = list(_csv.DictReader(open(os.path.join(dirs[0], "rapport_recuperation.csv"), encoding="utf-8-sig"), delimiter=";"))
        assert len(rows) == 2 and all(len(x["sha256"]) == 64 for x in rows), rows
        assert " pull -a " in read_log()
        # resume: create next folder content? reuse same dest via base with existing same-size file
        assert not errs
        print("OK: detect+scan+pull (-a, sha256, csv)", flush=True)
    finally:
        safe_destroy(app)
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_pull_timeout():
    clear_log()
    app, errs = make_app()
    tmp_base = tempfile.mkdtemp(prefix="recup_to_")
    try:
        app.on_detect(); pump(app, 1.5)
        app.on_scan(); wait_worker(app, 20)
        app._items[0]["checked"] = True
        app._refresh_tree()
        r.filedialog.askdirectory = lambda **k: tmp_base
        orig = app._run

        def boom(cmd, **kwargs):
            if "pull" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return orig(cmd, **kwargs)

        app._run = boom
        app.on_recover(); wait_worker(app, 10)
        journal = app.log_text.get("1.0", "end")
        assert "délai dépassé" in journal.lower() or "Délai dépassé" in journal
        assert not errs
        print("OK: pull timeout", flush=True)
    finally:
        safe_destroy(app)
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_detect_timeout():
    clear_log()
    os.environ["FAKE_HANG"] = "3"
    app, errs = make_app()
    real = r.subprocess.run

    def short(*a, **kw):
        if kw.get("timeout") is not None:
            kw["timeout"] = 0.4
        return real(*a, **kw)

    r.subprocess.run = short
    try:
        app.on_detect(); pump(app, 3.5)
        journal = app.log_text.get("1.0", "end")
        assert "délai dépassé" in journal.lower() or "ne répond plus" in journal
        assert not errs
        print("OK: detect timeout", flush=True)
    finally:
        r.subprocess.run = real
        os.environ.pop("FAKE_HANG", None)
        safe_destroy(app)


def test_free_space_abort_and_resume():
    clear_log()
    app, errs = make_app()
    tmp_base = tempfile.mkdtemp(prefix="recup_fs_")
    try:
        app.on_detect(); pump(app, 1.5)
        app.on_scan(); wait_worker(app, 20)
        app._items[0]["checked"] = True
        r.filedialog.askdirectory = lambda **k: tmp_base
        shown = []
        orig_err = r.messagebox.showerror
        r.messagebox.showerror = lambda *a, **k: shown.append(a)
        orig_fs = r.free_space_ok
        r.free_space_ok = lambda d, n, margin=0: (False, 10)
        try:
            app.on_recover(); pump(app, 0.5)
        finally:
            r.free_space_ok = orig_fs
            r.messagebox.showerror = orig_err
        assert shown and "insuffisant" in shown[0][0].lower()
        assert not glob.glob(os.path.join(tmp_base, "Récupéré_*"))
        # debounce + incremental counter
        app._refresh_tree()
        before = app._checked_count
        iid = app.tree.get_children()[1]
        app._toggle_iid(iid)
        assert app._checked_count == before + 1
        assert app._search_after is None
        app.search_var.set("IMG")
        assert app._search_after is not None
        pump(app, 0.4)
        assert app._search_after is None
        assert all("img" in app.tree.item(i, "values")[6].lower() or "img" in app.tree.item(i, "values")[1].lower()
                   for i in app.tree.get_children())
        assert not errs
        print("OK: free-space abort + debounce + counters", flush=True)
    finally:
        safe_destroy(app)
        shutil.rmtree(tmp_base, ignore_errors=True)


if __name__ == "__main__":
    test_detect_scan_pull()
    test_free_space_abort_and_resume()
    test_pull_timeout()
    test_detect_timeout()
    print("ALL SMOKE TESTS PASSED", flush=True)
    shutil.rmtree(_FAKE_DIR, ignore_errors=True)
