from pathlib import Path

from gdrivefilter.dedup import find_exact_duplicates
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.reorganize import category_for, reorganize
from tests.fakes import sample_tree


def test_category_mapping():
    assert category_for("x/a.jpg") == "Images"
    assert category_for("x/report.pdf") == "Documents"
    assert category_for("x/data.csv") == "Spreadsheets"
    assert category_for("x/weird.xyz") == "Other"


def test_reorganize_copies_and_leaves_source_intact(cfg, tmp_path: Path):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="R")
    primary = cfg.backup_root / "R/default"
    dest = tmp_path / "clean"
    m = Manifest.load(primary / "manifest.json")

    before = sorted(p.relative_to(primary).as_posix() for p in primary.rglob("*") if p.is_file())
    dedup = find_exact_duplicates(m)
    rep = reorganize(primary, dest, m, dedup=dedup, by_year=True)

    # Source mirror untouched.
    after = sorted(p.relative_to(primary).as_posix() for p in primary.rglob("*") if p.is_file())
    assert before == after

    # One duplicate quarantined, the rest placed by category/year.
    assert rep.quarantined == 1
    assert (dest / "_quarantine/duplicates").exists()
    assert any((dest / "Images").glob("*/*.jpg"))
    assert (dest / "Documents").exists()


def test_reorganize_refuses_inside_source(cfg, tmp_path: Path):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="R2")
    primary = cfg.backup_root / "R2/default"
    m = Manifest.load(primary / "manifest.json")
    import pytest
    with pytest.raises(ValueError):
        reorganize(primary, primary / "sub", m)
