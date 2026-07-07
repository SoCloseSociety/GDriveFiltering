"""Disk-space preflight.

Before any extraction we estimate the total Drive size and compare it to the
free space at each destination. If it does not fit (plus a safety margin), we
STOP and tell the user to plug in a hard drive -- exactly as requested.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .logging_conf import get_logger

log = get_logger("preflight")
GB = 1024 ** 3


class InsufficientSpace(Exception):
    """Raised when a destination cannot hold the backup."""


class DriveNotMounted(Exception):
    """Raised when a destination lives on an external volume that is not mounted."""


def check_mounted(destinations: list[Path]) -> None:
    """Refuse to run when a /Volumes/<X> destination is not actually mounted.

    Without this, an unplugged drive would silently become a plain FOLDER on the
    internal disk and the backup would fill the Mac instead of the hard drive.
    """
    for dest in destinations:
        d = Path(dest).expanduser()
        parts = d.parts
        if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
            volume = Path("/Volumes") / parts[2]
            if not volume.exists():
                raise DriveNotMounted(
                    f"Le disque '{volume}' n'est pas branché/monté.\n"
                    ">>> ACTION REQUISE: branche le disque dur externe puis relance. "
                    "(Refus de créer un faux dossier sur le disque interne.)"
                )


@dataclass
class SpaceCheck:
    destination: Path
    required_bytes: int
    free_bytes: int
    margin_bytes: int

    @property
    def ok(self) -> bool:
        return self.free_bytes >= self.required_bytes + self.margin_bytes

    @property
    def shortfall_bytes(self) -> int:
        return max(0, (self.required_bytes + self.margin_bytes) - self.free_bytes)

    def human(self) -> str:
        return (f"{self.destination}: besoin ~{self.required_bytes/GB:.2f} Go "
                f"(+{self.margin_bytes/GB:.1f} Go marge), libre {self.free_bytes/GB:.2f} Go "
                f"-> {'OK' if self.ok else f'MANQUE {self.shortfall_bytes/GB:.2f} Go'}")


def _free_bytes(path: Path) -> int:
    # Walk up to the nearest existing parent (destination may not exist yet).
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    return shutil.disk_usage(probe).free


def check_destinations(destinations: list[Path], required_bytes: int,
                       margin_gb: float, raise_on_fail: bool = True) -> list[SpaceCheck]:
    """Check every destination. Raise InsufficientSpace on the first that fails."""
    margin = int(margin_gb * GB)
    checks: list[SpaceCheck] = []
    for dest in destinations:
        chk = SpaceCheck(dest, required_bytes, _free_bytes(dest), margin)
        checks.append(chk)
        log.info(chk.human())

    failed = [c for c in checks if not c.ok]
    if failed and raise_on_fail:
        lines = "\n".join("  - " + c.human() for c in failed)
        raise InsufficientSpace(
            "Espace disque insuffisant pour la backup.\n" + lines +
            "\n\n>>> ACTION REQUISE: branche un disque dur externe puis renseigne "
            "BACKUP_MIRROR_EXT (ou BACKUP_ROOT) dans .env vers ce disque, et relance."
        )
    return checks
