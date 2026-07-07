"""Drive v3 wrapper covering My Drive + ALL shared drives + shared items.

The client talks to a `backend` object with a small, mockable surface, so the
whole traversal is unit-testable without hitting Google (see tests/fakes.py).
"""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from .logging_conf import get_logger

log = get_logger("drive")

# Google-native mime -> (export mime, extension) for office/pdf output.
_EXPORT = {
    "office": {
        "application/vnd.google-apps.document":
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
        "application/vnd.google-apps.spreadsheet":
            ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation":
            ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    },
    "pdf": {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    },
}
_FOLDER_MIME = "application/vnd.google-apps.folder"
_FILE_FIELDS = ("id, name, mimeType, parents, size, modifiedTime, "
                "owners(emailAddress), driveId, trashed, shortcutDetails")


class DriveBackend(Protocol):
    def files_list(self, **params) -> dict: ...
    def drives_list(self, **params) -> dict: ...
    def download(self, file_id: str) -> bytes: ...
    def export(self, file_id: str, mime: str) -> bytes: ...
    def download_to(self, file_id: str, fileobj) -> None: ...
    def export_to(self, file_id: str, mime: str, fileobj) -> None: ...


@dataclass
class RemoteFile:
    file_id: str
    name: str
    mime_type: str
    size: int
    rel_path: str
    drive_id: str
    drive_name: str
    owner: str
    modified_time: str
    is_native: bool
    export_mime: str = ""
    export_ext: str = ""


class GoogleDriveBackend:
    """Real backend backed by googleapiclient. Imported lazily.

    googleapiclient/httplib2 is NOT thread-safe, so download()/export() use a
    THREAD-LOCAL service -- each worker thread gets its own. Listing runs on the
    main thread via the shared service. static_discovery avoids a network fetch
    when building the per-thread service.
    """

    def __init__(self, credentials):
        self._creds = credentials
        self._local = threading.local()
        self.service = self._build_service()

    def _build_service(self):
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build
        # Per-connection socket timeout: a stalled chunk raises instead of hanging
        # a worker forever (critical for a multi-hour backup). httplib2.Http is not
        # thread-safe, so this runs once per worker thread via _svc().
        authed = google_auth_httplib2.AuthorizedHttp(
            self._creds, http=httplib2.Http(timeout=180))
        return build("drive", "v3", http=authed,
                     cache_discovery=False, static_discovery=True)

    def _svc(self):
        s = getattr(self._local, "svc", None)
        if s is None:
            s = self._build_service()
            self._local.svc = s
        return s

    def reset(self) -> None:
        """Drop this thread's service so the next call builds a fresh connection.
        Used to recover from a dead/half-closed HTTP connection."""
        self._local.svc = None

    def files_list(self, **params) -> dict:
        return _retry(lambda: self.service.files().list(**params).execute())

    def drives_list(self, **params) -> dict:
        return _retry(lambda: self.service.drives().list(**params).execute())

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        request = self._svc().files().get_media(fileId=file_id, supportsAllDrives=True)
        return _download_stream(request, MediaIoBaseDownload)

    def export(self, file_id: str, mime: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        request = self._svc().files().export_media(fileId=file_id, mimeType=mime)
        return _download_stream(request, MediaIoBaseDownload)

    def download_to(self, file_id: str, fileobj) -> None:
        from googleapiclient.http import MediaIoBaseDownload
        request = self._svc().files().get_media(fileId=file_id, supportsAllDrives=True)
        _download_to_fileobj(request, fileobj, MediaIoBaseDownload)

    def export_to(self, file_id: str, mime: str, fileobj) -> None:
        from googleapiclient.http import MediaIoBaseDownload
        request = self._svc().files().export_media(fileId=file_id, mimeType=mime)
        _download_to_fileobj(request, fileobj, MediaIoBaseDownload)


def _download_stream(request, downloader_cls) -> bytes:
    buf = io.BytesIO()
    downloader = downloader_cls(buf, request)
    done = False
    while not done:
        _, done = _retry(lambda: downloader.next_chunk())
    return buf.getvalue()


# Chunk size for streaming downloads: bounds memory to ~chunksize * workers.
_STREAM_CHUNK = 8 * 1024 * 1024


def _download_to_fileobj(request, fileobj, downloader_cls) -> None:
    """Stream a download straight to a writable file object (not into RAM)."""
    downloader = downloader_cls(fileobj, request, chunksize=_STREAM_CHUNK)
    done = False
    while not done:
        _, done = _retry(lambda: downloader.next_chunk())


def _is_rate_limit_403(e) -> bool:
    """True only for retryable 403s (rate/quota); permission-style 403s are permanent."""
    blob = (str(getattr(e, "reason", "")) + str(getattr(e, "content", b""))).lower()
    return any(k in blob for k in
               ("ratelimitexceeded", "userratelimitexceeded",
                "quotaexceeded", "dailylimitexceeded", "sharinglimitexceeded"))


def _retry(fn, attempts: int = 6, base: float = 1.5):
    """Exponential backoff for 429/5xx (quota) AND network stalls/timeouts."""
    import socket

    from googleapiclient.errors import HttpError
    last = None
    for i in range(attempts):
        try:
            return fn()
        except HttpError as e:  # pragma: no cover - network dependent
            status = getattr(e, "status_code", None) or getattr(e.resp, "status", 0)
            last = e
            if status == 403 and not _is_rate_limit_403(e):
                raise  # permanent 403 (abusive-file, no-download permission): no retry
            if status in (403, 429, 500, 502, 503, 504):
                wait = base ** i
                log.warning("Drive %s -- retry dans %.1fs (%d/%d)", status, wait, i + 1, attempts)
                time.sleep(wait)
                continue
            raise
        except (socket.timeout, TimeoutError, ConnectionError, BrokenPipeError) as e:  # pragma: no cover
            last = e
            wait = base ** i
            log.warning("Réseau %s -- retry dans %.1fs (%d/%d)",
                        type(e).__name__, wait, i + 1, attempts)
            time.sleep(wait)
            continue
    raise last  # pragma: no cover


class DriveClient:
    def __init__(self, backend: DriveBackend, export_format: str = "office"):
        self.backend = backend
        self.export_format = export_format if export_format in _EXPORT else "office"

    def list_drives(self) -> list[dict]:
        """My Drive first, then every shared drive the account can see."""
        drives = [{"id": "", "name": "My Drive"}]
        page = None
        while True:
            resp = self.backend.drives_list(
                pageSize=100, fields="nextPageToken, drives(id, name)", pageToken=page
            )
            for d in resp.get("drives", []):
                drives.append({"id": d["id"], "name": d.get("name", d["id"])})
            page = resp.get("nextPageToken")
            if not page:
                break
        return drives

    def _sources(self) -> list[dict]:
        """All listing sources: My Drive, each shared drive, and 'Shared with me'."""
        sources = list(self.list_drives())  # My Drive + shared drives
        # "Shared with me": files shared with the account but not in My Drive nor a
        # shared drive. Rooted under a virtual "Shared with me" folder.
        sources.append({"id": "", "name": "Shared with me", "shared_with_me": True})
        return sources

    def _list_source(self, source: dict) -> list[dict]:
        """Every file+folder for one source, across all pages."""
        files: list[dict] = []
        page = None
        common = dict(
            pageSize=1000,
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        if source.get("shared_with_me"):
            common.update(corpora="user", q="sharedWithMe = true and trashed = false")
        elif source["id"]:
            common.update(corpora="drive", driveId=source["id"], q="trashed = false")
        else:
            common.update(corpora="user", q="trashed = false")
        pages = 0
        while True:
            resp = self.backend.files_list(pageToken=page, **common)
            files.extend(resp.get("files", []))
            pages += 1
            if pages % 5 == 0:
                log.info("  ... %s: %d éléments listés (%d pages)",
                         source.get("name", "?"), len(files), pages)
            page = resp.get("nextPageToken")
            if not page:
                break
        return files

    def _expand_folders(self, roots: list[dict]) -> list[dict]:
        """BFS-list the contents of folders. Needed for 'Shared with me':
        the API returns only DIRECTLY shared items -- children of a shared
        folder appear in no corpus unless the user already opened them."""
        out: list[dict] = []
        queue = [f["id"] for f in roots if f.get("mimeType") == _FOLDER_MIME]
        seen_folders: set[str] = set(queue)
        while queue:
            fid = queue.pop()
            page = None
            while True:
                resp = self.backend.files_list(
                    pageToken=page, pageSize=1000,
                    fields=f"nextPageToken, files({_FILE_FIELDS})",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                    q=f"'{fid}' in parents and trashed = false",
                )
                for child in resp.get("files", []):
                    out.append(child)
                    cid = child["id"]
                    if child.get("mimeType") == _FOLDER_MIME and cid not in seen_folders:
                        seen_folders.add(cid)
                        queue.append(cid)
                page = resp.get("nextPageToken")
                if not page:
                    break
        return out

    def walk(self) -> Iterator[RemoteFile]:
        """Yield every downloadable file across all sources, deduped by file_id."""
        seen: set[str] = set()
        for source in self._sources():
            log.info("Source: %s ...", source.get("name", "?"))
            raw = self._list_source(source)
            if source.get("shared_with_me"):
                extra = self._expand_folders(raw)
                if extra:
                    log.info("  ... +%d éléments dans les dossiers partagés", len(extra))
                known = {f["id"] for f in raw}
                raw += [f for f in extra if f["id"] not in known]
            log.info("Source: %s -> %d éléments", source.get("name", "?"), len(raw))
            by_id = {f["id"]: f for f in raw}
            for f in raw:
                if f["id"] in seen:
                    continue  # same file reachable from several sources -> once
                if f.get("mimeType") == _FOLDER_MIME:
                    continue
                if f.get("shortcutDetails"):
                    continue  # shortcuts point elsewhere; skip to avoid dup/loop
                rf = self._to_remote(f, by_id, source)
                if rf:
                    seen.add(f["id"])
                    yield rf

    def _to_remote(self, f: dict, by_id: dict, drive: dict) -> RemoteFile | None:
        mime = f.get("mimeType", "")
        is_native = mime.startswith("application/vnd.google-apps")
        export_mime, export_ext = "", ""
        if is_native:
            mapping = _EXPORT[self.export_format].get(mime)
            if not mapping:
                return None  # non-exportable native type (forms, sites, etc.)
            export_mime, export_ext = mapping
        rel = self._build_path(f, by_id, drive)
        owners = f.get("owners") or [{}]
        return RemoteFile(
            file_id=f["id"],
            name=f.get("name", f["id"]),
            mime_type=mime,
            size=int(f.get("size", 0) or 0),
            rel_path=rel + (export_ext if is_native else ""),
            drive_id=drive["id"],
            drive_name=drive["name"],
            owner=owners[0].get("emailAddress", ""),
            modified_time=f.get("modifiedTime", ""),
            is_native=is_native,
            export_mime=export_mime,
            export_ext=export_ext,
        )

    def _build_path(self, f: dict, by_id: dict, drive: dict) -> str:
        parts = [_safe(f.get("name", f["id"]))]
        seen = {f["id"]}
        cur = f
        while True:
            parents = cur.get("parents") or []
            if not parents or parents[0] not in by_id or parents[0] in seen:
                break
            cur = by_id[parents[0]]
            seen.add(cur["id"])
            parts.append(_safe(cur.get("name", cur["id"])))
        parts.reverse()
        root = _safe(drive["name"])
        return f"{root}/" + "/".join(parts)

    def fetch_bytes(self, rf: RemoteFile) -> bytes:
        if rf.is_native:
            return self.backend.export(rf.file_id, rf.export_mime)
        return self.backend.download(rf.file_id)

    def download_to_file(self, rf: RemoteFile, fileobj) -> None:
        """Stream a file's bytes into `fileobj` (memory-bounded)."""
        if rf.is_native:
            self.backend.export_to(rf.file_id, rf.export_mime, fileobj)
        else:
            self.backend.download_to(rf.file_id, fileobj)

    def reset_connection(self) -> None:
        """Force a fresh HTTP connection on the current thread (if supported)."""
        reset = getattr(self.backend, "reset", None)
        if callable(reset):
            reset()


# Cap a path component well under the exFAT/NTFS 255-unit limit, leaving room for
# the temp prefix (".part-<16>-") added during download.
_MAX_COMPONENT = 180


def _safe(name: str) -> str:
    """Filesystem-safe path component (portable, incl. FAT/exFAT).

    Replaces characters illegal on Windows/exFAT and control chars, trims
    trailing spaces/dots (which FAT/exFAT silently drop and Windows rejects),
    and truncates over-long names (preserving the extension) so writes never
    fail with ENAMETOOLONG.
    """
    bad = '<>:"/\\|?*'
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in name)
    out = out.strip().rstrip(" .")
    if len(out) > _MAX_COMPONENT:
        stem, dot, ext = out.rpartition(".")
        ext = (dot + ext) if (dot and len(ext) <= 20) else ""
        keep = _MAX_COMPONENT - len(ext)
        out = ((stem or out)[:keep]).strip().rstrip(" .") + ext
    return out or "unnamed"
