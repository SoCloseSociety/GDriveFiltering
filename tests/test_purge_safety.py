"""The most important tests: purge must refuse unless everything is safe."""
from pathlib import Path

import pytest

from gdrivefilter.clean import PurgeRefused, purge_duplicates
from gdrivefilter.dedup import find_exact_duplicates
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.reorganize import reorganize
from tests.fakes import sample_tree


def _prepare(cfg, tmp_path: Path):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="P")
    primary = cfg.backup_root / "P/default"
    external = cfg.backup_mirror_ext / "P/default"
    m = Manifest.load(primary / "manifest.json")
    dedup = find_exact_duplicates(m)
    clean_tree = tmp_path / "clean"
    reorganize(primary, clean_tree, m, dedup=dedup)
    return primary, external, clean_tree, dedup


def test_purge_refused_without_confirm(cfg, tmp_path):
    primary, external, clean_tree, dedup = _prepare(cfg, tmp_path)
    with pytest.raises(PurgeRefused, match="confirmation"):
        purge_duplicates(cfg, primary, external, clean_tree, dedup,
                         confirm=False, dry_run=True)


def test_purge_refused_without_external_backup(cfg, tmp_path):
    primary, external, clean_tree, dedup = _prepare(cfg, tmp_path)
    with pytest.raises(PurgeRefused):
        # No external mirror provided -> gate fails even with confirm.
        purge_duplicates(cfg, primary, None, clean_tree, dedup,
                         confirm=True, dry_run=True)


def test_purge_refuses_to_touch_source_mirror(cfg, tmp_path):
    primary, external, clean_tree, dedup = _prepare(cfg, tmp_path)
    with pytest.raises(PurgeRefused, match="miroir de backup"):
        purge_duplicates(cfg, primary, external, primary, dedup,
                         confirm=True, dry_run=True)


def test_purge_dryrun_then_apply_when_fully_safe(cfg, tmp_path):
    primary, external, clean_tree, dedup = _prepare(cfg, tmp_path)
    # dry-run: reports but deletes nothing
    res = purge_duplicates(cfg, primary, external, clean_tree, dedup,
                           confirm=True, dry_run=True)
    assert res.dry_run and res.deleted  # something to delete detected

    # Kept primaries in the clean tree (outside _quarantine) before purge.
    primaries_before = sorted(
        p.relative_to(clean_tree).as_posix()
        for p in clean_tree.rglob("*")
        if p.is_file() and "_quarantine" not in p.parts
    )

    # apply: actually removes duplicate copies from the CLEAN tree only
    res2 = purge_duplicates(cfg, primary, external, clean_tree, dedup,
                            confirm=True, dry_run=False)
    assert not res2.dry_run
    # source mirror still intact
    assert (primary / "My Drive/b.jpg").exists()
    assert (primary / "My Drive/Photos/a.jpg").exists()
    # every deleted file was inside _quarantine
    assert all(d.startswith("_quarantine") for d in res2.deleted)
    # kept primaries in the clean tree are untouched
    primaries_after = sorted(
        p.relative_to(clean_tree).as_posix()
        for p in clean_tree.rglob("*")
        if p.is_file() and "_quarantine" not in p.parts
    )
    assert primaries_before == primaries_after and primaries_after
