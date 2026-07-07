from gdrivefilter.manifest import Entry, Manifest
from gdrivefilter.progress import read_snapshot, write_progress


def _seed_manifest(d, done=3, expected=10):
    m = Manifest(d / "manifest.json")
    m.expected_total = expected
    for i in range(done):
        m.upsert(Entry(file_id=str(i), name=f"f{i}", rel_path=f"f{i}", mime_type="x",
                       size=100, drive_id="", drive_name="My Drive", owner="", modified_time="",
                       status="done"))
    m.save()


def test_snapshot_from_manifest_head(tmp_path):
    _seed_manifest(tmp_path, done=3, expected=10)
    snap = read_snapshot(tmp_path)
    assert snap is not None
    assert snap.done == 3 and snap.expected == 10
    assert snap.bytes_written == 300
    assert 29 < snap.pct < 31
    assert snap.source == "manifest"


def test_heartbeat_is_preferred(tmp_path):
    _seed_manifest(tmp_path, done=3, expected=10)
    write_progress(tmp_path, done=7, expected=10, errors=1,
                   bytes_written=700, elapsed_s=10.0)
    snap = read_snapshot(tmp_path)
    assert snap.source == "heartbeat"
    assert snap.done == 7 and snap.errors == 1
    assert snap.rate_bps == 70.0
