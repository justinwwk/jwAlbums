# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The app was renamed from iPhoneAlbumExporter to jwAlbums: old data must carry over."""

from app import settings
from export.manifest import MANIFEST_NAME, OLD_STATE_DIR, STATE_DIR, Manifest


def test_app_folder_moves_to_new_name(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / "iPhoneAlbumExporter"
    old.mkdir()
    (old / "settings.json").write_text('{"destination": "D:\\\\Photos"}', encoding="utf-8")
    root = settings.app_root()
    assert root == tmp_path / "jwAlbums"
    assert not old.exists()
    assert settings.Settings.load().destination == "D:\\Photos"


def test_app_folder_new_name_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "iPhoneAlbumExporter").mkdir()
    (tmp_path / "jwAlbums").mkdir()
    settings.app_root()
    assert (tmp_path / "iPhoneAlbumExporter").exists()     # left alone, never merged


def test_export_history_moves_to_new_name(tmp_path):
    m = Manifest(tmp_path)
    m.close()
    (tmp_path / STATE_DIR).rename(tmp_path / OLD_STATE_DIR)     # an export made before the rename
    m = Manifest(tmp_path)
    try:
        assert m.path == tmp_path / STATE_DIR / MANIFEST_NAME
        assert not (tmp_path / OLD_STATE_DIR).exists()
    finally:
        m.close()


def test_folder_with_old_name_but_no_manifest_is_not_taken(tmp_path):
    (tmp_path / OLD_STATE_DIR).mkdir()          # e.g. an album folder that happens to have that name
    (tmp_path / OLD_STATE_DIR / "IMG_0001.HEIC").write_bytes(b"x")
    m = Manifest(tmp_path)
    m.close()
    assert (tmp_path / OLD_STATE_DIR / "IMG_0001.HEIC").exists()
    assert (tmp_path / STATE_DIR / MANIFEST_NAME).exists()
