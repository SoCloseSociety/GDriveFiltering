from pathlib import Path

from gdrivefilter.manifest import Entry, Manifest, sha256_file


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"HELLO")
    import hashlib
    assert sha256_file(p) == hashlib.sha256(b"HELLO").hexdigest()


def test_manifest_roundtrip_and_resume(tmp_path: Path):
    mp = tmp_path / "manifest.json"
    m = Manifest(mp, account="default")
    m.upsert(Entry(file_id="a", name="a.jpg", rel_path="My Drive/a.jpg",
                   mime_type="image/jpeg", size=5, drive_id="", drive_name="My Drive",
                   owner="me", modified_time="2023", sha256="deadbeef", status="done"))
    m.save()

    loaded = Manifest.load(mp)
    assert loaded.account == "default"
    assert loaded.is_done("a")
    assert loaded.count_done() == 1
    assert loaded.total_bytes() == 5
