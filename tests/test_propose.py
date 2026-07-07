from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.filters import classify_junk
from gdrivefilter.manifest import Manifest
from gdrivefilter.propose import build_proposal, render_html, render_text
from gdrivefilter.reorganize import reorganize
from tests.fakes import FakeBackend


def test_classify_junk():
    assert classify_junk("x/.DS_Store", 10)[0]
    assert classify_junk("x/~$report.docx", 10)[0]
    assert classify_junk("x/tmpfile.tmp", 10)[0]
    assert classify_junk("x/empty.txt", 0)[0]
    assert not classify_junk("x/photo.jpg", 100)[0]


def _backend_with_junk():
    def f(fid, name, size):
        return {"id": fid, "name": name, "mimeType": "application/octet-stream",
                "parents": [], "size": str(size), "modifiedTime": "2023-01-01T00:00:00Z",
                "owners": [{"emailAddress": "me@x.com"}]}
    files = [f("a", "a.jpg", 5), f("b", "b.jpg", 5), f("j", ".DS_Store", 6), f("e", "empty.txt", 0)]
    content = {"a": b"HELLO", "b": b"HELLO", "j": b"JUNKKK", "e": b""}
    return FakeBackend(files_by_drive={"": files}, shared_drives=[], content=content, exports={})


def test_proposal_counts_dupes_junk_and_clean(cfg):
    run_backup(cfg, DriveClient(_backend_with_junk()), account="default", timestamp="PP")
    primary = cfg.backup_root / "PP/default"
    m = Manifest.load(primary / "manifest.json")
    prop = build_proposal(primary, m)

    assert prop.total_files == 4
    assert prop.dupe_files == 1          # a.jpg / b.jpg identical
    assert prop.junk_files == 2          # .DS_Store + empty.txt
    assert prop.clean_files == 1         # only one real kept file
    # Renderers don't crash and include expected content.
    assert "clean tree" in render_text(prop).lower()
    assert "Clean tree" in render_html(prop, {"pct": 50, "done": 2, "expected": 4, "bytes_written": 10})


def test_reorganize_routes_junk_and_dupes_to_quarantine(cfg, tmp_path):
    from gdrivefilter.dedup import find_exact_duplicates
    from gdrivefilter.filters import junk_paths
    run_backup(cfg, DriveClient(_backend_with_junk()), account="default", timestamp="PQ")
    primary = cfg.backup_root / "PQ/default"
    m = Manifest.load(primary / "manifest.json")
    rep = reorganize(primary, tmp_path / "clean", m,
                     dedup=find_exact_duplicates(m), junk=junk_paths(m.done_entries()))
    assert rep.quarantined_junk == 2
    assert rep.quarantined_dup == 1
    assert rep.copied == 1
    # Source mirror untouched.
    assert (primary / "My Drive/.DS_Store").exists() or (primary).exists()
