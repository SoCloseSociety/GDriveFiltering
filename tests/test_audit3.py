"""Tests for the third audit pass: reorganize case-collisions, _clean/ dir
pollution of backup discovery, and propose junk/dup double-counting."""
import json

from gdrivefilter.dashboard import _discover_accounts
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.propose import build_proposal
from gdrivefilter.reorganize import _unique, reorganize
from tests.fakes import FakeBackend, sample_tree


def test_reorganize_unique_is_case_insensitive(tmp_path):
    taken: set[str] = set()
    a = _unique(tmp_path, "Documents/2023/Report.pdf", "aaaa1111", taken)
    b = _unique(tmp_path, "Documents/2023/report.pdf", "bbbb2222", taken)
    assert a.casefold() != b.casefold()  # distinct even on exFAT/APFS


def test_reorganize_dry_run_mapping_has_no_case_collisions(cfg, tmp_path):
    files = [
        {"id": "u1", "name": "Report.pdf", "mimeType": "application/pdf", "parents": [],
         "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
        {"id": "u2", "name": "report.pdf", "mimeType": "application/pdf", "parents": [],
         "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
    ]
    be = FakeBackend(files_by_drive={"": files}, shared_drives=[],
                     content={"u1": b"AAA", "u2": b"BBB"}, exports={})
    run_backup(cfg, DriveClient(be), account="default", timestamp="A3")
    primary = cfg.backup_root / "A3/default"
    m = Manifest.load(primary / "manifest.json")
    rep = reorganize(primary, tmp_path / "clean", m, dry_run=True)
    dests = [d.casefold() for _, d in rep.mapping]
    assert len(dests) == len(set(dests))  # dry-run mapping already collision-free


def test_clean_dir_never_picked_as_latest_backup(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="A4")
    # Simulate a reorganize output landing under backup_root/_clean/<account>,
    # even with a manifest.json inside ("_" sorts after digits in ASCII).
    trap = cfg.backup_root / "_clean" / "default"
    trap.mkdir(parents=True)
    (trap / "manifest.json").write_text(json.dumps({"entries": []}), encoding="utf-8")

    dirs = _discover_accounts(cfg)
    assert "default" in dirs
    rel = dirs["default"].relative_to(cfg.backup_root)
    assert rel.parts[0] == "A4"  # real backup wins, not the _clean/ trap

    from gdrivefilter.cli import _latest_backup_dir
    latest = _latest_backup_dir(cfg, "default")
    assert latest is not None
    assert latest.relative_to(cfg.backup_root).parts[0] == "A4"


def test_propose_junk_dup_not_double_counted(cfg):
    # Two identical empty-suffix files: junk (0 bytes) AND exact duplicates.
    files = [
        {"id": "a", "name": "x.txt", "mimeType": "text/plain", "parents": [],
         "size": "0", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
        {"id": "b", "name": "y.txt", "mimeType": "text/plain", "parents": [],
         "size": "0", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
    ]
    be = FakeBackend(files_by_drive={"": files}, shared_drives=[],
                     content={"a": b"", "b": b""}, exports={})
    run_backup(cfg, DriveClient(be), account="default", timestamp="A5")
    primary = cfg.backup_root / "A5/default"
    p = build_proposal(primary, Manifest.load(primary / "manifest.json"))
    # Both are junk (0 bytes); neither may ALSO be counted as a duplicate.
    assert p.junk_files == 2
    assert p.dupe_files == 0
    assert p.total_files == p.junk_files + p.dupe_files + p.clean_files
