from pathlib import Path

from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from tests.fakes import sample_tree


def _client():
    return DriveClient(sample_tree(), export_format="office")


def test_walk_covers_all_drives_and_shared_with_me():
    files = list(_client().walk())
    paths = sorted(f.rel_path for f in files)
    assert paths == [
        "My Drive/Photos/a.jpg",
        "My Drive/b.jpg",
        "My Drive/dup.txt",              # two files with the same name...
        "My Drive/dup.txt",              # ...yielded twice (disambiguated on write)
        "My Drive/notes.docx",           # native doc exported
        "Shared with me/shared_note.txt",  # shared-with-me coverage
        "TeamDrive/report.pdf",          # shared drive covered
    ]


def test_backup_writes_both_destinations_and_manifest(cfg):
    res = run_backup(cfg, _client(), account="default", timestamp="T1")
    assert res.downloaded == 7 and res.errors == 0

    primary = cfg.backup_root / "T1/default"
    external = cfg.backup_mirror_ext / "T1/default"
    for root in (primary, external):
        assert (root / "My Drive/Photos/a.jpg").read_bytes() == b"HELLO"
        assert (root / "TeamDrive/report.pdf").read_bytes() == b"PDF"
        assert (root / "My Drive/notes.docx").read_bytes() == b"DOCX-BYTES"
        assert (root / "Shared with me/shared_note.txt").read_bytes() == b"SHARED"

    m = Manifest.load(primary / "manifest.json")
    assert m.count_done() == 7
    # Expected/actual byte tracking is persisted for accurate %/ETA.
    assert m.expected_bytes == 27          # listing sizes: 5+5+3+4+4+6 (native doc = 0)
    assert m.total_bytes() == 37           # actual bytes incl. the 10-byte .docx export


def test_same_name_files_do_not_overwrite(cfg):
    # Drive allows two files "dup.txt" in the same folder; both bytes must survive.
    run_backup(cfg, _client(), account="default", timestamp="C1")
    primary = cfg.backup_root / "C1/default"
    dup_files = sorted((primary / "My Drive").glob("dup*.txt"))
    contents = sorted(p.read_bytes() for p in dup_files)
    assert contents == [b"AAAA", b"BBBB"]  # neither overwrote the other


def test_backup_is_resumable(cfg):
    # First run downloads everything.
    run_backup(cfg, _client(), account="default", timestamp="T2")
    client2 = _client()
    res2 = run_backup(cfg, client2, account="default", timestamp="T2")
    # Second run skips all (nothing re-downloaded).
    assert res2.skipped == 7 and res2.downloaded == 0
    assert client2.backend.download_calls == []


def test_dry_run_downloads_nothing(cfg):
    client = _client()
    res = run_backup(cfg, client, account="default", timestamp="T3", dry_run=True)
    assert res.downloaded == 0 and res.skipped == 7
    assert client.backend.download_calls == []
    assert not (cfg.backup_root / "T3").exists()
