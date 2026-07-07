from pathlib import Path

from gdrivefilter.dedup import find_exact_duplicates
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.verify import is_backup_safe, verify_backup
from tests.fakes import sample_tree


def _backup(cfg, ts="V"):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp=ts)
    return cfg.backup_root / f"{ts}/default", cfg.backup_mirror_ext / f"{ts}/default"


def test_exact_duplicates_detected(cfg):
    primary, _ = _backup(cfg)
    m = Manifest.load(primary / "manifest.json")
    report = find_exact_duplicates(m)
    # a.jpg and b.jpg share bytes b"HELLO" -> exactly one duplicate.
    assert report.duplicate_count == 1
    assert report.reclaimable_bytes == 5


def test_verify_clean_then_detects_tampering(cfg):
    primary, _ = _backup(cfg)
    assert verify_backup(primary).clean

    # Corrupt one file -> size/hash mismatch detected.
    (primary / "My Drive/b.jpg").write_bytes(b"TAMPERED")
    rep = verify_backup(primary)
    assert not rep.clean
    assert rep.size_mismatch or rep.hash_mismatch


def test_backup_safe_gate(cfg):
    primary, external = _backup(cfg)
    safe, _ = is_backup_safe(primary, external, require_external=True)
    assert safe

    # Without external mirror the gate refuses.
    safe2, reason = is_backup_safe(primary, None, require_external=True)
    assert not safe2 and "externe" in reason.lower()
