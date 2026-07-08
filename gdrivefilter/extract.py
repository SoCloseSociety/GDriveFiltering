"""Extraction / backup -- the core. READ-ONLY on Google Drive.

Mirrors every file to one or more destinations, resumable via the manifest,
after a disk-space preflight that stops and asks for a hard drive if needed.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import os
import shutil
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .drive_client import DriveClient, RemoteFile
from .logging_conf import get_logger
from .manifest import Entry, Manifest
from .preflight import check_destinations, check_mounted
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
    imported: int = 0        # unchanged files copied locally from a previous backup
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


def _reconcile_mirrors(dests: list[Path], rel_path: str, fetch,
                       expected_size: int | None = None) -> int:
    """Ensure every destination has rel_path AND at the right size (a crash
    mid-copy can leave a truncated file that a bare existence check would keep
    forever). Copies from a healthy mirror when possible, else re-fetches."""
    def healthy(d: Path) -> bool:
        f = d / rel_path
        if not f.is_file():
            return False
        if expected_size is not None:
            try:
                return f.stat().st_size == expected_size
            except OSError:
                return False
        return True

    missing = [d for d in dests if not healthy(d)]
    if not missing:
        return 0
    existing = next((d for d in dests if healthy(d)), None)
    data = (existing / rel_path).read_bytes() if existing else fetch()
    for d in missing:
        _write_one(d, rel_path, data)  # atomic (.part + rename)
    return len(missing)


class _HashingWriter:
    """File-like wrapper that sha256s bytes as they stream to disk."""
    __slots__ = ("_fh", "_h")

    def __init__(self, fh, h):
        self._fh, self._h = fh, h

    def write(self, b) -> int:
        self._h.update(b)
        return self._fh.write(b)


# Transient network drops that warrant a full-file retry with a fresh connection.
# http.client.HTTPException covers mid-stream server drops (IncompleteRead,
# BadStatusLine, RemoteDisconnected...) -- Drive API errors arrive as
# googleapiclient HttpError, never as these, so they are all connection-level.
_NET_ERRORS = (ConnectionError, TimeoutError, OSError, http.client.HTTPException)
# Local-filesystem failures that retrying can never fix (disk full, drive
# unmounted, permissions, read-only FS): fail fast instead of 4 retry cycles.
_FATAL_ERRNOS = {errno.ENOSPC, errno.ENOENT, errno.EACCES, errno.EROFS, errno.EDQUOT}
_FILE_ATTEMPTS = 4


def _is_transient(e: BaseException) -> bool:
    if isinstance(e, (ConnectionError, TimeoutError, ssl.SSLError,
                      http.client.HTTPException)):
        return True
    if isinstance(e, OSError):
        return e.errno not in _FATAL_ERRNOS
    return False


def _download_one(client: DriveClient, dests: list[Path], rf: RemoteFile, rel_path: str,
                  inflight: dict | None = None):
    """Worker: STREAM one file to disk (memory-bounded), hashing as it goes.

    Retries the whole file on a dropped connection (rebuilding the HTTP
    connection and truncating the temp), so large files survive Google closing
    a long-lived socket. Writes to a per-file unique temp (no shared .part ->
    no cross-thread race), atomic rename, then copies to extra destinations.
    Registers its temp in `inflight` so the heartbeat can count in-progress
    bytes. Returns a plain tuple; the main thread owns the manifest (lock-free)."""
    primary = dests[0]
    target = primary / rel_path
    token = (rf.file_id or "x").replace("/", "_")[:16]
    tmp = target.parent / f".part-{token}-{target.name}"
    if inflight is not None:
        inflight[token] = tmp
    last = ""
    try:
        for attempt in range(_FILE_ATTEMPTS):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with open(tmp, "wb") as fh:  # 'wb' truncates -> clean restart each attempt
                    client.download_to_file(rf, _HashingWriter(fh, h))
                size = tmp.stat().st_size
                tmp.replace(target)
                for d in dests[1:]:
                    t2 = d / rel_path
                    t2.parent.mkdir(parents=True, exist_ok=True)
                    t2_tmp = t2.parent / f".part-{token}-{t2.name}"
                    shutil.copy2(target, t2_tmp)   # atomic on the mirror too: a crash
                    t2_tmp.replace(t2)             # mid-copy never leaves a truncated file
                return rf, rel_path, size, h.hexdigest(), None
            except _NET_ERRORS as e:
                last = str(e)
                if not _is_transient(e):  # disk full/unmounted/permissions: no retry
                    break
                client.reset_connection()  # transient: fresh connection, retry the file
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:  # noqa: BLE001 - non-network: don't retry, report
                last = str(e)
                break
        try:
            tmp.unlink()
        except OSError:
            pass
        return rf, rel_path, rf.size, "", last
    finally:
        if inflight is not None:
            inflight.pop(token, None)


class BackupLocked(Exception):
    """Another backup process already owns this backup directory."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _acquire_lock(primary: Path) -> Path:
    """PID lockfile: two backup processes must never share a manifest/dir
    (both would rewrite manifest.json and sweep each other's .part files)."""
    primary.mkdir(parents=True, exist_ok=True)
    lock = primary / ".backup.lock"
    if lock.is_file():
        try:
            other = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            raise BackupLocked(
                f"Un backup est déjà en cours sur {primary} (pid {other}). "
                "Attends sa fin ou arrête-le avant de relancer.")
        log.warning("Lock périmé (pid %s mort) -- reprise.", other or "?")
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def run_backup(cfg: Config, client: DriveClient, account: str = "default",
               timestamp: str = "manual", dry_run: bool = False,
               save_every: int = 100, workers: int | None = None,
               prev_dir: Path | None = None) -> BackupResult:
    """Mirror the account's drives into timestamped backup dirs under each destination.

    Downloads run concurrently (thread pool) so wall-clock is bound by bandwidth,
    not by per-file round-trip latency. Manifest updates stay on the main thread.
    `prev_dir` (a previous COMPLETE backup) enables incremental mode: unchanged
    files are hash-verified local copies instead of re-downloads.
    """
    subdir = f"{timestamp}/{account}"
    primary = cfg.backup_root / subdir
    dests = [d / subdir for d in cfg.destinations]

    lock = None
    if not dry_run:
        check_mounted(cfg.destinations)  # unplugged drive must never become a folder
        lock = _acquire_lock(primary)
    try:
        return _run_backup_locked(cfg, client, account, primary, dests,
                                  dry_run, save_every, workers, prev_dir)
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


def _import_from_previous(prev_dir: Path, prev_entry, dests: list[Path],
                          rel_path: str) -> tuple[int, str] | None:
    """Incremental import: copy an unchanged file from a previous local backup
    into this one (disk-to-disk, hash-verified during the copy) instead of
    re-downloading it. Returns (size, sha256) or None to fall back to download."""
    src = prev_dir / prev_entry.rel_path
    try:
        if not src.is_file() or src.stat().st_size != prev_entry.size:
            return None
        h = hashlib.sha256()
        target = dests[0] / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".part-imp-{target.name}"
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for chunk in iter(lambda: fin.read(4 * 1024 * 1024), b""):
                h.update(chunk)
                fout.write(chunk)
        if prev_entry.sha256 and h.hexdigest() != prev_entry.sha256:
            tmp.unlink()   # silent corruption on the drive -> re-download instead
            return None
        size = tmp.stat().st_size
        tmp.replace(target)
        for d in dests[1:]:
            t2 = d / rel_path
            t2.parent.mkdir(parents=True, exist_ok=True)
            t2_tmp = t2.parent / f".part-imp-{t2.name}"
            shutil.copy2(target, t2_tmp)
            t2_tmp.replace(t2)
        return size, h.hexdigest()
    except OSError:
        return None


def _run_backup_locked(cfg: Config, client: DriveClient, account: str,
                       primary: Path, dests: list[Path], dry_run: bool,
                       save_every: int, workers: int | None,
                       prev_dir: Path | None = None) -> BackupResult:
    manifest_path = primary / "manifest.json"
    manifest = Manifest.load(manifest_path)
    manifest.account = account
    result = BackupResult(account, primary, manifest_path)
    total = manifest.expected_total  # refined after the listing
    start = time.monotonic()
    start_bytes = 0
    inflight: dict[str, Path] = {}   # token -> active .part path (for live byte count)

    def _inflight_bytes() -> int:
        total_b = 0
        for p in list(inflight.values()):
            try:
                total_b += p.stat().st_size
            except OSError:
                pass
        return total_b

    # Heartbeat from the very start: the listing + mirror-reconcile phases can
    # run for minutes with no file completing, and `status`/dashboard (and its
    # "already running" guard) must still see activity during them.
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(15):
            try:
                el = time.monotonic() - start
                inflight_b = _inflight_bytes()
                effective = result.bytes_written + inflight_b
                rate = max(0.0, (effective - start_bytes) / el) if el > 0 else 0
                write_progress(primary, result.skipped + result.downloaded, total,
                               result.errors, result.bytes_written, el,
                               manifest.expected_bytes, rate, inflight_b)
            except Exception:  # noqa: BLE001 - heartbeat must never die
                pass

    hb = None
    if not dry_run:
        write_progress(primary, manifest.count_done(), total, 0,
                       manifest.total_bytes(), 0.0, manifest.expected_bytes, 0)
        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

    try:
        log.info("Listing de tous les drives (My Drive + Shared Drives + partagés)...")
        files: list[RemoteFile] = list(client.walk())
    except BaseException:
        stop_hb.set()
        raise
    known_bytes = sum(f.size for f in files)
    n_native = sum(1 for f in files if f.is_native)
    log.info("%d fichiers trouvés (~%.2f Go connus ; %d Google-natifs à exporter, "
             "taille estimée en sus).", len(files), known_bytes / (1024**3), n_native)

    manifest.expected_total = len(files)
    manifest.expected_bytes = known_bytes
    result.total_files = len(files)
    total = len(files)

    # Preflight on the REMAINING work only (a resume must never demand the full
    # backup size again once most bytes are already on disk). Reserve a
    # conservative 10 MB per still-pending native export (they list as size 0;
    # if the estimate is too low the failed writes become errors and the
    # completeness gate refuses -- never a silent "clean" incomplete backup).
    remaining_known = sum(f.size for f in files if not manifest.is_done(f.file_id))
    native_pending = sum(1 for f in files if f.is_native and not manifest.is_done(f.file_id))
    native_reserve = native_pending * 10 * 1024 * 1024
    if native_pending:
        log.info("Réserve estimée pour %d exports Google natifs restants: %.2f Go (approx).",
                 native_pending, native_reserve / (1024**3))
    # In dry-run we only WARN about space (so the listing always shows); a real
    # backup hard-stops and asks for a hard drive if it will not fit.
    check_destinations(cfg.destinations, remaining_known + native_reserve,
                       cfg.disk_margin_gb, raise_on_fail=not dry_run)

    if dry_run:
        log.info("[dry-run] Aucun téléchargement. %d fichiers seraient sauvegardés.", len(files))
        result.skipped = len(files)
        return result

    # Seed used paths (casefolded) from completed entries so a resume never reassigns.
    used_paths: set[str] = {e.rel_path.casefold() for e in manifest.done_entries()}
    # Cumulative bytes already backed up (so %/ETA count resumed data too).
    result.bytes_written = sum(e.size for e in manifest.done_entries())
    start_bytes = result.bytes_written  # delta-rate baseline for this run

    # Prune stale error entries for files that no longer exist on Drive
    # (deleted/unshared since the failed attempt): they can never be retried
    # and would otherwise keep the backup "incomplete" forever.
    listed_ids = {f.file_id for f in files}
    stale = [e.file_id for e in manifest.failed_entries()
             if e.file_id and e.file_id not in listed_ids]
    for fid in stale:
        del manifest.entries[fid]
    if stale:
        log.info("%d entrées en erreur purgées (fichiers disparus de Drive).", len(stale))

    # Incremental source: the manifest of a previous COMPLETE backup, if any.
    prev_manifest = None
    if prev_dir is not None and (prev_dir / "manifest.json").is_file():
        prev_manifest = Manifest.load(prev_dir / "manifest.json")
        log.info("Mode incrémental: réutilisation locale depuis %s (%d fichiers).",
                 prev_dir, prev_manifest.count_done())

    # Phase 1 (single-thread): already-done -> reconcile mirrors; unchanged vs the
    # previous backup -> LOCAL hash-verified copy (no download); else -> pending.
    pending: list[tuple[RemoteFile, str]] = []
    for i, rf in enumerate(files, 1):
        if manifest.is_done(rf.file_id):
            entry = manifest.entries[rf.file_id]
            try:
                result.repaired += _reconcile_mirrors(
                    dests, entry.rel_path, lambda rf=rf: client.fetch_bytes(rf),
                    expected_size=entry.size)
            except Exception as e:  # noqa: BLE001 - a repair failure must not abort the run
                log.warning("Réparation miroir échouée pour %s: %s", entry.rel_path, e)
            result.skipped += 1
            continue
        rel_path = _unique_rel_path(rf.rel_path, rf.file_id, used_paths)
        if prev_manifest is not None:
            pe = prev_manifest.entries.get(rf.file_id)
            unchanged = (pe is not None and pe.status == "done"
                         and pe.modified_time == rf.modified_time
                         and (rf.is_native or pe.size == rf.size))
            if unchanged:
                imported = _import_from_previous(prev_dir, pe, dests, rel_path)
                if imported is not None:
                    size, sha = imported
                    manifest.upsert(Entry(
                        file_id=rf.file_id, name=rf.name, rel_path=rel_path,
                        mime_type=rf.mime_type, size=size, drive_id=rf.drive_id,
                        drive_name=rf.drive_name, owner=rf.owner,
                        modified_time=rf.modified_time, sha256=sha,
                        exported_as=rf.export_mime, status="done",
                    ))
                    result.imported += 1
                    result.bytes_written += size
                    if result.imported % 200 == 0:
                        manifest.save()
                        log.info("Import local: %d fichiers réutilisés (%.1f Go)...",
                                 result.imported, result.bytes_written / (1024**3))
                    continue
        pending.append((rf, rel_path))
    if result.imported:
        manifest.save()
        log.info("Import local terminé: %d fichiers réutilisés sans re-téléchargement.",
                 result.imported)

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
    try:
      with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_download_one, client, dests, rf, rel_path, inflight)
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
                rate = (result.bytes_written - start_bytes) / elapsed if elapsed > 0 else 0
                write_progress(primary, manifest.count_done(), total, result.errors,
                               result.bytes_written, elapsed, manifest.expected_bytes, rate)
                log.info("Progression: %d/%d (dl=%d skip=%d err=%d, %.2f Go, %.2f Mo/s)",
                         result.skipped + processed, total, result.downloaded,
                         result.skipped, result.errors, result.bytes_written / (1024**3),
                         rate / 1e6)
    finally:
        stop_hb.set()

    manifest.save()
    _elapsed = time.monotonic() - start
    write_progress(primary, manifest.count_done(), total, result.errors,
                   result.bytes_written, _elapsed, manifest.expected_bytes,
                   (result.bytes_written - start_bytes) / _elapsed if _elapsed > 0 else 0)
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
