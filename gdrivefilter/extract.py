"""Extraction / backup -- the core. READ-ONLY on Google Drive.

Mirrors every file to one or more destinations, resumable via the manifest,
after a disk-space preflight that stops and asks for a hard drive if needed.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .drive_client import DriveClient, RemoteFile
from .logging_conf import get_logger
from .manifest import Entry, Manifest
from .preflight import check_destinations
from .progress import write_progress

log = get_logger("extract")


@dataclass
class BackupResult:
    account: str
    backup_dir: Path
    manifest_path: Path
    total_files: int = 0
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    repaired: int = 0        # done files re-mirrored to a destination missing them
    bytes_written: int = 0
    error_samples: list[str] = field(default_factory=list)


def _unique_rel_path(rel_path: str, file_id: str, used: set[str]) -> str:
    """Give each backed-up file a distinct local path so none overwrites another.

    Google Drive allows several files with the same name in one folder, and many
    backup targets (exFAT/APFS/NTFS) are CASE-INSENSITIVE -- so uniqueness is
    checked case-insensitively (`used` holds casefolded paths). Disambiguated
    deterministically with a short file_id suffix.
    """
    if rel_path.casefold() not in used:
        used.add(rel_path.casefold())
        return rel_path
    p = Path(rel_path)
    suffix = (file_id or "dup").replace("/", "_")[:8]
    candidate = str(p.with_name(f"{p.stem}__{suffix}{p.suffix}"))
    n = 1
    while candidate.casefold() in used:
        candidate = str(p.with_name(f"{p.stem}__{suffix}_{n}{p.suffix}"))
        n += 1
    used.add(candidate.casefold())
    return candidate


def _write_one(dest: Path, rel_path: str, data: bytes) -> None:
    target = dest / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)


def _write_all(dests: list[Path], rel_path: str, data: bytes) -> int:
    for dest in dests:
        _write_one(dest, rel_path, data)
    return len(data) * len(dests)


def _reconcile_mirrors(dests: list[Path], rel_path: str, fetch) -> int:
    """Ensure every destination has rel_path. Copies from an existing mirror when
    possible, else re-fetches. Fixes a fresh/replaced external drive on resume."""
    missing = [d for d in dests if not (d / rel_path).is_file()]
    if not missing:
        return 0
    existing = next((d for d in dests if (d / rel_path).is_file()), None)
    data = (existing / rel_path).read_bytes() if existing else fetch()
    for d in missing:
        _write_one(d, rel_path, data)
    return len(missing)


class _HashingWriter:
    """File-like wrapper that sha256s bytes as they stream to disk."""
    __slots__ = ("_fh", "_h")

    def __init__(self, fh, h):
        self._fh, self._h = fh, h

    def write(self, b) -> int:
        self._h.update(b)
        return self._fh.write(b)


def _download_one(client: DriveClient, dests: list[Path], rf: RemoteFile, rel_path: str):
    """Worker: STREAM one file to disk (memory-bounded), hashing as it goes.

    Writes to a per-file unique temp (no shared .part -> no cross-thread race,
    even on case-insensitive filesystems), fsync-free atomic rename, then copies
    to any extra destinations. Returns a plain tuple; the main thread owns the
    manifest (lock-free)."""
    primary = dests[0]
    target = primary / rel_path
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        token = (rf.file_id or "x").replace("/", "_")[:16]
        tmp = target.parent / f".part-{token}-{target.name}"
        h = hashlib.sha256()
        with open(tmp, "wb") as fh:
            client.download_to_file(rf, _HashingWriter(fh, h))
        size = tmp.stat().st_size
        tmp.replace(target)
        tmp = None
        for d in dests[1:]:
            t2 = d / rel_path
            t2.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, t2)
        return rf, rel_path, size, h.hexdigest(), None
    except Exception as e:  # noqa: BLE001 - reported to main thread, never aborts the pool
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return rf, rel_path, rf.size, "", str(e)


def run_backup(cfg: Config, client: DriveClient, account: str = "default",
               timestamp: str = "manual", dry_run: bool = False,
               save_every: int = 100, workers: int | None = None) -> BackupResult:
    """Mirror the account's drives into timestamped backup dirs under each destination.

    Downloads run concurrently (thread pool) so wall-clock is bound by bandwidth,
    not by per-file round-trip latency. Manifest updates stay on the main thread.
    """
    subdir = f"{timestamp}/{account}"
    primary = cfg.backup_root / subdir
    dests = [d / subdir for d in cfg.destinations]

    log.info("Listing de tous les drives (My Drive + Shared Drives + partagés)...")
    files: list[RemoteFile] = list(client.walk())
    known_bytes = sum(f.size for f in files)
    n_native = sum(1 for f in files if f.is_native)
    log.info("%d fichiers trouvés (~%.2f Go connus ; %d Google-natifs à exporter, "
             "taille estimée en sus).", len(files), known_bytes / (1024**3), n_native)

    # Preflight: reserve known bytes + a conservative heuristic for native exports
    # (Docs/Sheets/Slides report size 0 in the listing, so we cannot know exactly).
    # If the estimate is too low and the disk fills mid-run, writes fail -> those
    # entries are recorded as errors -> verify/is_backup_safe then FAIL (never a
    # silent "clean" incomplete backup). Reserve 10 MB per native file.
    native_reserve = n_native * 10 * 1024 * 1024
    if n_native:
        log.info("Réserve estimée pour %d exports Google natifs: %.2f Go (approx).",
                 n_native, native_reserve / (1024**3))
    # In dry-run we only WARN about space (so the listing always shows); a real
    # backup hard-stops and asks for a hard drive if it will not fit.
    check_destinations(cfg.destinations, known_bytes + native_reserve,
                       cfg.disk_margin_gb, raise_on_fail=not dry_run)

    manifest_path = primary / "manifest.json"
    manifest = Manifest.load(manifest_path)
    manifest.account = account
    manifest.expected_total = len(files)
    result = BackupResult(account, primary, manifest_path, total_files=len(files))

    if dry_run:
        log.info("[dry-run] Aucun téléchargement. %d fichiers seraient sauvegardés.", len(files))
        result.skipped = len(files)
        return result

    # Seed used paths (casefolded) from completed entries so a resume never reassigns.
    used_paths: set[str] = {e.rel_path.casefold() for e in manifest.done_entries()}

    # Phase 1 (single-thread, fast): partition into already-done (reconcile mirrors)
    # and pending (assign a unique local path now, before any parallel dispatch).
    pending: list[tuple[RemoteFile, str]] = []
    for rf in files:
        if manifest.is_done(rf.file_id):
            done_rel = manifest.entries[rf.file_id].rel_path
            try:
                result.repaired += _reconcile_mirrors(
                    dests, done_rel, lambda rf=rf: client.fetch_bytes(rf))
            except Exception as e:  # noqa: BLE001 - a repair failure must not abort the run
                log.warning("Réparation miroir échouée pour %s: %s", done_rel, e)
            result.skipped += 1
            continue
        pending.append((rf, _unique_rel_path(rf.rel_path, rf.file_id, used_paths)))

    n_workers = workers or cfg.download_workers
    total = len(files)

    # Sweep orphan .part files from a previous crash (they'll be re-downloaded).
    try:
        for part in primary.rglob(".part-*"):
            try:
                part.unlink()
            except OSError:
                pass
    except OSError:
        pass

    log.info("Téléchargement de %d fichiers (%d déjà présents) avec %d workers parallèles...",
             len(pending), result.skipped, n_workers)

    # Phase 2 (parallel): download concurrently; the main thread updates the manifest.
    processed = 0
    start = time.monotonic()

    # Time-based heartbeat so `status`/dashboard stay fresh even during long
    # large-file downloads (when no file completes for minutes). Reads only ints.
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(15):
            try:
                write_progress(primary, result.skipped + result.downloaded, total,
                               result.errors, result.bytes_written, time.monotonic() - start)
            except Exception:  # noqa: BLE001 - heartbeat must never die
                pass

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    try:
      with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_download_one, client, dests, rf, rel_path)
                   for rf, rel_path in pending]
        for fut in as_completed(futures):
            rf, rel_path, size, sha, err = fut.result()
            if err is None:
                manifest.upsert(Entry(
                    file_id=rf.file_id, name=rf.name, rel_path=rel_path,
                    mime_type=rf.mime_type, size=size, drive_id=rf.drive_id,
                    drive_name=rf.drive_name, owner=rf.owner, modified_time=rf.modified_time,
                    sha256=sha, exported_as=rf.export_mime, status="done",
                ))
                result.downloaded += 1
                result.bytes_written += size
            else:
                result.errors += 1
                if len(result.error_samples) < 10:
                    result.error_samples.append(f"{rel_path}: {err}")
                manifest.upsert(Entry(
                    file_id=rf.file_id, name=rf.name, rel_path=rel_path,
                    mime_type=rf.mime_type, size=rf.size, drive_id=rf.drive_id,
                    drive_name=rf.drive_name, owner=rf.owner, modified_time=rf.modified_time,
                    status="error", error=err,
                ))
                log.warning("Erreur sur %s: %s", rel_path, err)

            processed += 1
            if processed % save_every == 0:
                manifest.save()
                elapsed = time.monotonic() - start
                rate = result.bytes_written / elapsed if elapsed > 0 else 0
                write_progress(primary, manifest.count_done(), total, result.errors,
                               result.bytes_written, elapsed)
                log.info("Progression: %d/%d (dl=%d skip=%d err=%d, %.2f Go, %.2f Mo/s)",
                         result.skipped + processed, total, result.downloaded,
                         result.skipped, result.errors, result.bytes_written / (1024**3),
                         rate / 1e6)
    finally:
        stop_hb.set()

    manifest.save()
    write_progress(primary, manifest.count_done(), total, result.errors,
                   result.bytes_written, time.monotonic() - start)
    # Copy the manifest into every destination so each mirror is self-describing.
    for dest in dests:
        if dest != primary:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "manifest.json").write_bytes(manifest_path.read_bytes())
    complete, reason = manifest.is_complete()
    level = log.info if complete else log.warning
    level("Backup terminé: dl=%d skip=%d err=%d repair=%d complet=%s (%s) -> %s",
          result.downloaded, result.skipped, result.errors, result.repaired,
          complete, reason, primary)
    return result
