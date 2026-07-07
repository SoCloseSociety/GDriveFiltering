"""Stress the parallel download path: many files + duplicate names, many workers.
Verifies no races in path assignment / manifest updates / disk writes."""
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.verify import verify_backup
from tests.fakes import FakeBackend


def _big_backend(n=120):
    files, content = [], {}
    for i in range(n):
        # Every 3rd file shares a name with a sibling in the same folder.
        name = "same.bin" if i % 3 == 0 else f"file_{i}.bin"
        fid = f"f{i}"
        files.append({"id": fid, "name": name, "mimeType": "application/octet-stream",
                      "parents": [], "size": str(10 + i),
                      "modifiedTime": "2023-01-01T00:00:00Z",
                      "owners": [{"emailAddress": "me@example.com"}]})
        content[fid] = f"payload-{i}".encode()
    return FakeBackend(files_by_drive={"": files}, shared_drives=[],
                       content=content, exports={}), n


def test_parallel_backup_no_races(cfg):
    backend, n = _big_backend(120)
    res = run_backup(cfg, DriveClient(backend), account="default",
                     timestamp="PAR", workers=16, save_every=10)
    assert res.downloaded == n and res.errors == 0

    primary = cfg.backup_root / "PAR/default"
    m = Manifest.load(primary / "manifest.json")
    # Every file recorded, every local path distinct (no overwrite despite dup names).
    assert m.count_done() == n
    paths = [e.rel_path for e in m.done_entries()]
    assert len(set(paths)) == n
    # Every recorded file exists on disk with the right bytes.
    for e in m.done_entries():
        assert (primary / e.rel_path).is_file()
    assert verify_backup(primary).clean


def test_parallel_resume_after_partial(cfg):
    backend, n = _big_backend(60)
    # First pass fully completes.
    run_backup(cfg, DriveClient(backend), account="default", timestamp="PR", workers=8)
    # Second pass: everything is already done -> zero re-downloads.
    backend2, _ = _big_backend(60)
    res2 = run_backup(cfg, DriveClient(backend2), account="default", timestamp="PR", workers=8)
    assert res2.downloaded == 0 and res2.skipped == n
    assert backend2.download_calls == []
