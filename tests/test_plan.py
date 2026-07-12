"""Tests for the edited-plan reorganization (audit fixes)."""
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from gdrivefilter.propose import build_plan, read_plan_csv
from gdrivefilter.reorganize import reorganize
from tests.fakes import sample_tree


def _backup(cfg, ts):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp=ts)
    d = cfg.backup_root / f"{ts}/default"
    return d, Manifest.load(d / "manifest.json")


def test_keep_never_lands_in_quarantine(cfg, tmp_path):
    primary, m = _backup(cfg, "P1")
    plan = build_plan(m)
    rescued = None
    for row in plan:
        if row["action"] == "quarantine":     # user "rescues" a quarantined dup...
            row["action"] = "keep"             # ...by flipping to keep, dest still _quarantine/
            rescued = row["src_rel"]
            break
    assert rescued
    rep = reorganize(primary, tmp_path / "clean", m, plan=plan)
    dest = dict(rep.mapping)[rescued]
    assert not dest.startswith("_quarantine")  # relocated OUT -> purge can't delete it


def test_plan_action_is_normalized(cfg, tmp_path):
    primary, m = _backup(cfg, "P2")
    plan = build_plan(m)
    plan[0]["action"] = "  Skip  "             # weird casing/whitespace = still skip
    plan[1]["action"] = "QUARANTINE"
    skipped, quarantined = plan[0]["src_rel"], plan[1]["src_rel"]
    rep = reorganize(primary, tmp_path / "clean", m, plan=plan)
    mp = dict(rep.mapping)
    assert skipped not in mp                    # 'Skip ' honored, not treated as keep
    assert mp[quarantined].startswith("_quarantine")  # 'QUARANTINE' honored


def test_read_plan_csv_handles_bom(tmp_path):
    p = tmp_path / "plan.csv"
    p.write_bytes(b"\xef\xbb\xbfaction,src_rel,dest_rel,reason\r\nkeep,a.jpg,X/a.jpg,\r\n")
    rows = read_plan_csv(p)
    assert rows[0]["action"] == "keep" and rows[0]["src_rel"] == "a.jpg"


def test_unplanned_files_are_skipped_not_crash(cfg, tmp_path):
    primary, m = _backup(cfg, "P3")
    plan = build_plan(m)[:2]                     # drop most rows (user filtered the CSV)
    rep = reorganize(primary, tmp_path / "clean", m, plan=plan)  # must not raise
    assert rep.copied + rep.quarantined <= 2     # only planned rows acted on
