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


def test_inflight_bytes_count_toward_progress(tmp_path):
    # 40 GB completed + 3 GB currently downloading, out of 100 GB expected.
    write_progress(tmp_path, done=10, expected=100, errors=0,
                   bytes_written=40_000_000_000, elapsed_s=100.0,
                   expected_bytes=100_000_000_000, rate_bps=430_000_000,
                   bytes_inflight=3_000_000_000)
    snap = read_snapshot(tmp_path)
    assert snap.bytes_inflight == 3_000_000_000
    assert snap.effective_bytes == 43_000_000_000
    # %/ETA use effective bytes so they don't stall during large downloads.
    assert 42.9 < snap.pct_bytes < 43.1
    from gdrivefilter.progress import remaining_bytes
    assert remaining_bytes(snap) == 57_000_000_000
