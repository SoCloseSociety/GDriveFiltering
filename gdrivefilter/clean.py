"""Purge -- the ONLY code path that can delete, and it is ultra-guarded.

It refuses to run unless:
  1. A verified primary backup exists, AND
  2. A verified external mirror exists (require_external), AND
  3. The caller passes confirm=True (the CLI maps this to
     --i-have-a-verified-backup).
Even then, it only removes files explicitly listed as duplicates in a dedup
report, and only from the reorganized COPY -- never from the source mirror.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .dedup import DedupReport
from .logging_conf import get_logger
from .verify import is_backup_safe

log = get_logger("clean")


class PurgeRefused(Exception):
    """Raised whenever the safety preconditions are not met."""


@dataclass
class PurgeResult:
    deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    dry_run: bool = True


def purge_duplicates(cfg: Config, primary_dir: Path, external_dir: Path | None,
                     target_tree: Path, dedup: DedupReport, confirm: bool,
                     dry_run: bool = True) -> PurgeResult:
    """Delete duplicate files from `target_tree` only, after passing every gate."""
    target_tree = Path(target_tree).resolve()
    primary_dir = Path(primary_dir).resolve()

    # Gate 0: never delete inside the source mirror (primary OR external).
    guarded = [primary_dir]
    if external_dir is not None:
        guarded.append(Path(external_dir).resolve())
    for mirror in guarded:
        if target_tree == mirror or mirror in target_tree.parents:
            raise PurgeRefused("Refus: la cible de purge est un miroir de backup. Interdit.")

    # Gate 1: explicit confirmation.
    if not confirm:
        raise PurgeRefused("Refus: confirmation manquante (--i-have-a-verified-backup).")

    # Gate 2: verified backup (primary + external).
    safe, reason = is_backup_safe(primary_dir, external_dir,
                                  require_external=True, check_hash=True)
    if not safe:
        raise PurgeRefused(f"Refus: {reason}")

    result = PurgeResult(dry_run=dry_run)
    # Only ever delete inside the quarantine folder of the reorganized COPY.
    # reorganize() routes duplicates there; kept primaries live OUTSIDE it and are
    # therefore never reachable by purge -- no risk of deleting a kept file.
    quarantine = target_tree / "_quarantine"
    if not quarantine.exists():
        log.info("Aucun dossier _quarantine dans %s -- rien à purger.", target_tree)
        return result
    for candidate in sorted(quarantine.rglob("*"), reverse=True):
        if candidate.is_file():
            size = candidate.stat().st_size
            result.deleted.append(str(candidate.relative_to(target_tree)))
            result.freed_bytes += size
            if not dry_run:
                candidate.unlink()
        elif candidate.is_dir() and not dry_run:
            try:
                candidate.rmdir()  # remove now-empty quarantine subdirs
            except OSError:
                pass
    log.info("Purge %s: %d fichiers, %.2f Go%s",
             "(dry-run)" if dry_run else "EFFECTIVE", len(result.deleted),
             result.freed_bytes / (1024**3), "" if dry_run else " SUPPRIMÉS")
    return result
