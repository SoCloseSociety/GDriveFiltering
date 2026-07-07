"""Tests for the fourth (full-project) audit fixes."""
import json
import os
from pathlib import Path

import pytest

from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import BackupLocked, _acquire_lock, run_backup
from gdrivefilter.manifest import Entry, Manifest
from gdrivefilter.verify import is_backup_safe
from tests.fakes import FOLDER, FakeBackend, sample_tree


# --- H3: shared-with-me folder contents -----------------------------------
def test_shared_folder_contents_are_backed_up(cfg):
    shared_with_me = [
        {"id": "shf", "name": "SharedAlbum", "mimeType": FOLDER, "parents": [],
         "modifiedTime": "2023-01-01T00:00:00Z", "owners": [{"emailAddress": "f@x.c"}]},
    ]
    children = {
        "shf": [
            {"id": "p1", "name": "photo1.jpg", "mimeType": "image/jpeg",
             "parents": ["shf"], "size": "4", "modifiedTime": "2023-01-01T00:00:00Z",
             "owners": [{"emailAddress": "f@x.c"}]},
            {"id": "sub", "name": "SubDir", "mimeType": FOLDER, "parents": ["shf"],
             "modifiedTime": "2023-01-01T00:00:00Z", "owners": [{"emailAddress": "f@x.c"}]},
        ],
        "sub": [
            {"id": "p2", "name": "photo2.jpg", "mimeType": "image/jpeg",
             "parents": ["sub"], "size": "4", "modifiedTime": "2023-01-01T00:00:00Z",
             "owners": [{"emailAddress": "f@x.c"}]},
        ],
    }
    be = FakeBackend(files_by_drive={"": []}, shared_drives=[],
                     content={"p1": b"AAAA", "p2": b"BBBB"},
                     shared_with_me=shared_with_me, children=children)
    paths = sorted(f.rel_path for f in DriveClient(be).walk())
    # Children AND grandchildren of a shared folder are found, with full paths.
    assert paths == [
        "Shared with me/SharedAlbum/SubDir/photo2.jpg",
        "Shared with me/SharedAlbum/photo1.jpg",
    ]


# --- H1: preflight on resume only needs the remaining bytes ----------------
def test_preflight_on_resume_only_counts_remaining(cfg, monkeypatch):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="H1")

    captured = {}
    def spy(dests, required, margin, raise_on_fail=True):
        captured["required"] = required
        return []
    monkeypatch.setattr("gdrivefilter.extract.check_destinations", spy)

    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="H1")
    assert captured["required"] == 0  # everything already downloaded -> no new space


# --- H2: PID lock forbids concurrent backups -------------------------------
def test_backup_lock_refuses_live_pid(tmp_path):
    # PID 1 (launchd/init) is always alive and never us.
    (tmp_path / ".backup.lock").write_text("1", encoding="utf-8")
    with pytest.raises(BackupLocked):
        _acquire_lock(tmp_path)


def test_backup_lock_reentrant_for_same_pid(tmp_path):
    _acquire_lock(tmp_path)
    _acquire_lock(tmp_path)  # same process may re-acquire (resume in-process)


def test_backup_lock_steals_from_dead_pid(tmp_path):
    (tmp_path / ".backup.lock").write_text("999999999", encoding="utf-8")  # dead pid
    lock = _acquire_lock(tmp_path)          # takes over
    assert int(lock.read_text()) == os.getpid()


# --- M1: truncated external mirror file is repaired on resume --------------
def test_truncated_external_file_repaired(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="M1")
    ext_file = cfg.backup_mirror_ext / "M1/default/My Drive/Photos/a.jpg"
    ext_file.write_bytes(b"HE")  # simulate a crash mid-copy (truncated)

    res = run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="M1")
    assert res.repaired >= 1
    assert ext_file.read_bytes() == b"HELLO"  # size-checked reconcile fixed it


# --- M2: stale error entries for vanished files get pruned -----------------
def test_stale_error_entry_pruned_on_resume(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="M2")
    primary = cfg.backup_root / "M2/default"
    m = Manifest.load(primary / "manifest.json")
    m.upsert(Entry(file_id="ghost", name="gone.bin", rel_path="My Drive/gone.bin",
                   mime_type="application/octet-stream", size=5, drive_id="",
                   drive_name="My Drive", owner="", modified_time="",
                   status="error", error="404"))
    m.save()
    assert not m.is_complete()[0]

    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="M2")
    m2 = Manifest.load(primary / "manifest.json")
    assert "ghost" not in m2.entries        # pruned: it no longer exists on Drive
    assert m2.is_complete()[0]              # backup is complete again


# --- gap: tampered EXTERNAL mirror must fail the safety gate ----------------
def test_tampered_external_mirror_fails_gate(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="TG")
    primary = cfg.backup_root / "TG/default"
    external = cfg.backup_mirror_ext / "TG/default"
    (external / "My Drive/b.jpg").write_bytes(b"EVIL!")  # same size, wrong bytes

    safe, reason = is_backup_safe(primary, external, require_external=True)
    assert not safe and "externe" in reason.lower()
