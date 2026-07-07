"""Incremental backups (no re-download), drive-mount guard, never-delete
guarantee, and the user-editable reorganization plan."""
from pathlib import Path

import pytest

from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.preflight import DriveNotMounted, check_mounted
from gdrivefilter.propose import build_plan, read_plan_csv, write_plan_csv
from gdrivefilter.reorganize import reorganize
from gdrivefilter.verify import verify_backup
from tests.fakes import sample_tree


# --- incremental: a NEW backup reuses local data, no re-download -----------
def test_new_backup_imports_locally_without_redownload(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="B1")
    prev = cfg.backup_root / "B1/default"
    assert verify_backup(prev).clean

    client2 = DriveClient(sample_tree())
    res = run_backup(cfg, client2, account="default", timestamp="B2", prev_dir=prev)
    assert res.imported == 7 and res.downloaded == 0 and res.errors == 0
    assert client2.backend.download_calls == []       # ZERO bytes re-downloaded
    assert client2.backend.export_calls == []
    # The new backup is complete, self-contained and verified.
    assert verify_backup(cfg.backup_root / "B2/default").clean


def test_changed_file_is_redownloaded_not_imported(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="C1")
    prev = cfg.backup_root / "C1/default"

    be = sample_tree()
    # One file changed on Drive since the previous backup.
    for f in be.files_by_drive[""]:
        if f["id"] == "img2":
            f["modifiedTime"] = "2025-12-31T00:00:00Z"
    be.content["img2"] = b"NEWDATA"
    f_sizes = {f["id"]: f for f in be.files_by_drive[""]}
    f_sizes["img2"]["size"] = "7"

    res = run_backup(cfg, DriveClient(be), account="default", timestamp="C2", prev_dir=prev)
    assert res.downloaded == 1 and res.imported == 6  # only the changed file
    new = cfg.backup_root / "C2/default"
    m = Manifest.load(new / "manifest.json")
    e = next(x for x in m.done_entries() if x.file_id == "img2")
    assert (new / e.rel_path).read_bytes() == b"NEWDATA"


def test_corrupted_previous_copy_falls_back_to_download(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="D1")
    prev = cfg.backup_root / "D1/default"
    # Silent corruption on the drive: same size, wrong bytes.
    (prev / "My Drive/Photos/a.jpg").write_bytes(b"XXXXX")

    client2 = DriveClient(sample_tree())
    res = run_backup(cfg, client2, account="default", timestamp="D2", prev_dir=prev)
    assert res.errors == 0
    assert "img1" in client2.backend.download_calls   # hash mismatch -> re-downloaded
    new = cfg.backup_root / "D2/default"
    assert (new / "My Drive/Photos/a.jpg").read_bytes() == b"HELLO"


# --- never-delete guarantee -------------------------------------------------
def test_backup_never_deletes_existing_user_data(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="ND")
    primary = cfg.backup_root / "ND/default"
    stray = primary / "My Drive/USER_PRECIOUS_FILE.txt"
    stray.write_text("do not touch", encoding="utf-8")
    old_backup = cfg.backup_root / "20200101_000000" / "default"
    old_backup.mkdir(parents=True)
    (old_backup / "old_data.bin").write_bytes(b"OLD")

    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="ND")
    assert stray.read_text(encoding="utf-8") == "do not touch"   # untouched
    assert (old_backup / "old_data.bin").read_bytes() == b"OLD"  # untouched


# --- drive-mount guard ------------------------------------------------------
def test_unmounted_volume_is_refused():
    with pytest.raises(DriveNotMounted):
        check_mounted([Path("/Volumes/DISQUE_INEXISTANT_XYZ/Backups")])


def test_mounted_or_local_paths_pass(tmp_path):
    check_mounted([tmp_path])                    # local path: fine
    check_mounted([Path("/Volumes")])            # not a volume subpath: fine


# --- editable plan ------------------------------------------------------------
def test_plan_roundtrip_and_edited_apply(cfg, tmp_path):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="PL")
    primary = cfg.backup_root / "PL/default"
    m = Manifest.load(primary / "manifest.json")

    rows = build_plan(m)
    assert len(rows) == 7
    actions = {r["action"] for r in rows}
    assert actions <= {"keep", "quarantine"}

    # User edits: custom folder for the pdf, skip the shared note.
    for r in rows:
        if r["src_rel"].endswith("report.pdf"):
            r["dest_rel"] = "Clients/SoClose/rapport_final.pdf"
        if r["src_rel"].endswith("shared_note.txt"):
            r["action"] = "skip"
    plan_csv = tmp_path / "plan.csv"
    write_plan_csv(rows, plan_csv)
    loaded = read_plan_csv(plan_csv)
    assert len(loaded) == 7

    dest = tmp_path / "clean"
    rep = reorganize(primary, dest, m, plan=loaded)
    assert (dest / "Clients/SoClose/rapport_final.pdf").is_file()   # edited dest applied
    assert not list(dest.rglob("shared_note.txt"))                   # skipped
    # Quarantine routing from the plan still works.
    assert (dest / "_quarantine").is_dir()


def test_plan_rejects_path_traversal(cfg, tmp_path):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="PT")
    primary = cfg.backup_root / "PT/default"
    m = Manifest.load(primary / "manifest.json")
    rows = build_plan(m)
    rows[0]["dest_rel"] = "../../outside/evil.bin"
    with pytest.raises(ValueError):
        reorganize(primary, tmp_path / "clean", m, plan=rows)