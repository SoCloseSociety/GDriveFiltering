from pathlib import Path

import pytest

from gdrivefilter.preflight import InsufficientSpace, check_destinations


def test_check_passes_with_room(tmp_path: Path):
    checks = check_destinations([tmp_path], required_bytes=1024, margin_gb=0.0)
    assert checks[0].ok


def test_check_stops_and_asks_for_hard_drive(tmp_path: Path):
    huge = 10 ** 18  # ~1 EB, cannot fit -> must raise and mention hard drive
    with pytest.raises(InsufficientSpace) as exc:
        check_destinations([tmp_path], required_bytes=huge, margin_gb=1.0)
    assert "disque dur" in str(exc.value)
