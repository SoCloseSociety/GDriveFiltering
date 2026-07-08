"""Verification + the safety gate that everything destructive depends on.

`verify_backup` re-checks every manifest entry against the bytes on disk
(existence + size + sha256). `is_backup_safe` is the single source of truth
that dedup/reorganize/purge consult before touching anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .logging_conf import get_logger
from .manifest import Manifest, sha256_file

log = get_logger("verify")


@dataclass
class VerifyReport:
    backup_dir: Path
    total: int = 0
    ok: int = 0
    missing: list[str] = field(default_factory=list)
    size_mismatch: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)
    complete: bool = True          # every expected file downloaded OK
    complete_reason: str = "complet"
    restricted: int = 0            # un-downloadable by Drive (reported, non-blocking)

    @property
    def clean(self) -> bool:
        return (self.total > 0 and self.complete and not self.missing
                and not self.size_mismatch and not self.hash_mismatch)

    def summary(self) -> str:
        return (f"{self.backup_dir}: {self.ok}/{self.total} OK, "
                f"manquants={len(self.missing)} taille!={len(self.size_mismatch)} "
                f"hash!={len(self.hash_mismatch)} complet={self.complete} "
                f"({self.complete_reason}) -> {'CLEAN' if self.clean else 'PROBLEME'}")


def verify_backup(backup_dir: Path, check_hash: bool = True,
                  manifest: Manifest | None = None) -> VerifyReport:
    """Verify files under backup_dir against a manifest.

    If `manifest` is None it is loaded from backup_dir/manifest.json. Passing an
    explicit manifest lets us verify a mirror (e.g. the external drive) against
    the canonical primary manifest even if the mirror has no manifest of its own.
    """
    backup_dir = Path(backup_dir)
    if manifest is None:
        manifest = Manifest.load(backup_dir / "manifest.json")
    rep = VerifyReport(backup_dir)
    rep.complete, rep.complete_reason = manifest.is_complete()
    rep.restricted = len(manifest.restricted_entries())
    for e in manifest.done_entries():
        rep.total += 1
        path = backup_dir / e.rel_path
        try:
            if not path.is_file():
                rep.missing.append(e.rel_path)
                continue
            if path.stat().st_size != e.size:
                rep.size_mismatch.append(e.rel_path)
                continue
            if check_hash and e.sha256 and sha256_file(path) != e.sha256:
                rep.hash_mismatch.append(e.rel_path)
                continue
        except OSError:
            # Unreadable (bad sector, disconnected mirror): a verification
            # failure to record, never a crash.
            rep.hash_mismatch.append(e.rel_path)
            continue
        rep.ok += 1
    log.info(rep.summary())
    return rep


def is_backup_safe(primary_dir: Path, external_dir: Path | None,
                   require_external: bool = True, check_hash: bool = True) -> tuple[bool, str]:
    """The gate. Returns (safe, reason). Destructive ops MUST pass this first."""
    canonical = Manifest.load(Path(primary_dir) / "manifest.json")
    primary = verify_backup(primary_dir, check_hash=check_hash, manifest=canonical)
    if not primary.clean:
        return False, f"Backup principal non vérifié: {primary.summary()}"
    if require_external:
        if external_dir is None:
            return False, ("Aucun miroir externe. Branche un disque dur et refais une "
                           "backup avant toute suppression.")
        # Verify the external mirror against the SAME (primary) manifest.
        ext = verify_backup(external_dir, check_hash=check_hash, manifest=canonical)
        if not ext.clean:
            return False, f"Miroir externe non vérifié: {ext.summary()}"
    return True, "Backup vérifié (principal" + (" + externe)" if require_external else ")")
