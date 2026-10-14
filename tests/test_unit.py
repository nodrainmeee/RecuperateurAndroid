#!/usr/bin/env python3
"""Tests unitaires purs (sans Tk) pour recup_gui helpers."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recup_gui as r


def test_utf8_accents_decode():
    # Simulate bytes decoded with utf-8 replace like _run
    raw = "café|2024-01-01 10:00|/sdcard/Photos/été.jpg".encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    parsed = r.parse_find_line(text)
    assert parsed is not None
    assert "été.jpg" in parsed["path"]
    assert "café" in text
    print("OK: utf-8 accents")


def test_permission_denied_filename_kept():
    line = "100|2024-01-01 10:00|/sdcard/Download/Permission denied.txt"
    parsed = r.parse_find_line(line)
    assert parsed is not None
    assert parsed["path"].endswith("Permission denied.txt")
    # stderr counting only
    assert r.count_permission_denied("find: /sdcard/Android/data: Permission denied\n") == 1
    assert r.count_permission_denied("") == 0
    print("OK: Permission denied.txt kept")


def test_collision_suffixes():
    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "photo.jpg")
        open(a, "w").write("x")
        used = set()
        d1 = r.unique_dest_path(a, used)
        assert d1.endswith("photo_1.jpg"), d1
        open(d1, "w").write("y")
        d2 = r.unique_dest_path(a, used)
        assert d2.endswith("photo_2.jpg"), d2
        # case-insensitive tracking
        used2 = {os.path.join(td, "File.TXT").lower()}
        d3 = r.unique_dest_path(os.path.join(td, "file.txt"), used2)
        assert d3.lower().endswith("file_1.txt"), d3
    print("OK: collision suffixes")


def test_reserved_names():
    assert r.sanitize_filename("CON") == "_CON"
    assert r.sanitize_filename("con.txt") == "_con.txt"
    assert r.sanitize_filename("aux") == "_aux"
    assert r.sanitize_filename("COM1.log") == "_COM1.log"
    assert r.sanitize_filename("lpt9") == "_lpt9"
    assert r.sanitize_filename("photo.jpg") == "photo.jpg"
    assert r.sanitize_filename("test. ") == "test"
    print("OK: reserved names")


def test_whatsapp_a11_classification():
    wa = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG.jpg"
    assert r.tag_source(wa) == "Messagerie"
    tg = "/sdcard/Android/media/org.telegram.messenger/Telegram/Telegram Images/a.jpg"
    assert r.tag_source(tg) == "Messagerie"
    # /sent/ only under messaging
    assert r.tag_source("/sdcard/WhatsApp/Media/Sent/x.jpg") == "Messagerie"
    assert r.tag_source("/sdcard/Documents/sent/notes.txt") != "Messagerie"
    # Google Photos trash vs cache
    assert r.tag_source(
        "/sdcard/Android/data/com.google.android.apps.photos/files/trash/a.jpg"
    ) == "Corbeille"
    assert r.tag_source(
        "/sdcard/Android/data/com.google.android.apps.photos/cache/thumb/a.jpg"
    ) != "Corbeille"
    # android/media alone is NOT thumbnail
    assert r.tag_source("/sdcard/Android/media/com.other.app/file.jpg") != "Miniature"
    print("OK: WhatsApp A11+ / Photos trash classification")


def test_pending_trashed_names():
    assert r.display_name(".pending-1700000000-IMG_001.jpg") == "IMG_001.jpg"
    assert r.display_name(".trashed-1700000000-vacation.png") == "vacation.png"
    p = "/sdcard/DCIM/.trashed-1700000000-vacation.png"
    assert r.tag_source(p) == "Corbeille"
    assert r.categorize(p) == "Photos"
    assert r.make_item({"size": 1, "mtime": "x", "path": p})["name"] == "vacation.png"
    print("OK: pending/trashed name parsing")


def test_devices_parsing():
    out = """List of devices attached
* daemon not running; starting now
* daemon started successfully
SERIAL1             device product:p1 model:Pixel_5
SERIAL2             unauthorized
"""
    pairs = r.parse_devices_output(out)
    assert pairs == [("SERIAL1", "device"), ("SERIAL2", "unauthorized")]
    print("OK: devices parsing")


def test_sha256_and_resume():
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "a.bin")
        open(f, "wb").write(b"abc")
        assert r.sha256_file(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert r.already_present(f, 3)
        assert not r.already_present(f, 4)
        assert not r.already_present(f, None)
        ok, free = r.free_space_ok(td, 1, margin=0)
        assert ok and free > 0
        assert not r.free_space_ok(td, 10**18)[0]
    print("OK: sha256 + resume + free space")


if __name__ == "__main__":
    test_sha256_and_resume()
    test_utf8_accents_decode()
    test_permission_denied_filename_kept()
    test_collision_suffixes()
    test_reserved_names()
    test_whatsapp_a11_classification()
    test_pending_trashed_names()
    test_devices_parsing()
    print("ALL UNIT TESTS PASSED", flush=True)
