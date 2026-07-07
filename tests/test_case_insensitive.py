"""Reproduce the exFAT bug: two files differing only by case must NOT collide
into one physical path (which caused the .part race / data loss)."""
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import _unique_rel_path, run_backup
from gdrivefilter.manifest import Manifest
from tests.fakes import FakeBackend


def test_unique_rel_path_is_case_insensitive():
    used: set[str] = set()
    a = _unique_rel_path("My Drive/French Links.xlsx", "id1", used)
    b = _unique_rel_path("My Drive/french links.xlsx", "id2", used)
    assert a != b
    # And neither maps to the same casefolded key.
    assert a.casefold() != b.casefold()


def test_case_colliding_files_both_survive(cfg):
    files = [
        {"id": "u1", "name": "Report.pdf", "mimeType": "application/pdf", "parents": [],
         "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "me@x.com"}]},
        {"id": "u2", "name": "report.pdf", "mimeType": "application/pdf", "parents": [],
         "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "me@x.com"}]},
    ]
    content = {"u1": b"AAA", "u2": b"BBB"}
    be = FakeBackend(files_by_drive={"": files}, shared_drives=[], content=content, exports={})
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="CI", workers=8)
    assert res.downloaded == 2 and res.errors == 0

    primary = cfg.backup_root / "CI/default"
    m = Manifest.load(primary / "manifest.json")
    paths = {e.rel_path.casefold() for e in m.done_entries()}
    assert len(paths) == 2  # distinct even case-insensitively -> no overwrite
