"""Tests for the audit-pass fixes: completeness gate, mirror repair, type coercion."""
import json
import shutil
from pathlib import Path

from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.verify import is_backup_safe, verify_backup
from tests.fakes import sample_tree


def test_verify_fails_on_incomplete_backup(cfg):
    # Make one download fail -> backup is incomplete.
    be = sample_tree()
    orig = be.download_to

    def boom(fid, fileobj):
        if fid == "swm1":
            raise RuntimeError("network drop")
        return orig(fid, fileobj)

    be.download_to = boom
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="E")
    assert res.errors == 1 and res.downloaded == 6

    primary = cfg.backup_root / "E/default"
    rep = verify_backup(primary)
    # The whole point: an incomplete backup must NOT be reported clean.
    assert not rep.complete and not rep.clean

    safe, reason = is_backup_safe(primary, cfg.backup_mirror_ext / "E/default")
    assert not safe  # gate must refuse -> nothing can be deleted on a partial backup


def test_complete_backup_passes_gate(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="OK")
    primary = cfg.backup_root / "OK/default"
    rep = verify_backup(primary)
    assert rep.complete and rep.clean
    safe, _ = is_backup_safe(primary, cfg.backup_mirror_ext / "OK/default")
    assert safe


def test_resume_repopulates_fresh_external_mirror(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="F")
    external = cfg.backup_mirror_ext / "F/default"
    primary = cfg.backup_root / "F/default"

    # Simulate swapping in a brand-new empty external drive.
    shutil.rmtree(external)

    res = run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="F")
    assert res.downloaded == 0 and res.repaired >= 1  # repaired, not re-downloaded

    # External mirror is fully repopulated from the primary (no data loss).
    assert (external / "My Drive/Photos/a.jpg").read_bytes() == b"HELLO"
    canonical = Manifest.load(primary / "manifest.json")
    assert verify_backup(external, manifest=canonical).clean


def test_manifest_size_coercion(tmp_path: Path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "account": "x",
        "entries": [{"file_id": "a", "name": "n", "rel_path": "r",
                     "mime_type": "m", "size": "5", "status": "done"}],
    }), encoding="utf-8")
    m = Manifest.load(p)
    assert m.entries["a"].size == 5          # coerced str -> int
    assert m.total_bytes() == 5              # no TypeError
