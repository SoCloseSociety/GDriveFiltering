"""Manifest: the resumable index + point of truth for verification.

A manifest is a JSON file listing every backed-up item with metadata and a
sha256 of the local bytes. It doubles as the resume log (a file already in the
manifest with status=done is skipped on re-run).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
_HASH_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Entry:
    file_id: str
    name: str
    rel_path: str          # path relative to the backup root
    mime_type: str
    size: int
    drive_id: str          # "" for My Drive
    drive_name: str        # "My Drive" or shared drive name
    owner: str
    modified_time: str
    sha256: str = ""
    exported_as: str = ""  # export mime for Google-native files, else ""
    status: str = "pending"  # pending | done | error | skipped
    error: str = ""


class Manifest:
    def __init__(self, path: Path, account: str = ""):
        self.path = Path(path)
        self.account = account
        self.entries: dict[str, Entry] = {}  # keyed by file_id
        self.expected_total = 0  # files the walk said should be backed up this run

    # ---- persistence -------------------------------------------------
    @staticmethod
    def _entry_from_row(row: dict) -> Entry:
        """Build an Entry, coercing types so hand-edited/older manifests can't
        poison size (str) and later crash total_bytes()/verify."""
        def s(k: str) -> str:
            v = row.get(k, "")
            return "" if v is None else str(v)
        return Entry(
            file_id=s("file_id"), name=s("name"), rel_path=s("rel_path"),
            mime_type=s("mime_type"), size=int(row.get("size", 0) or 0),
            drive_id=s("drive_id"), drive_name=s("drive_name"), owner=s("owner"),
            modified_time=s("modified_time"), sha256=s("sha256"),
            exported_as=s("exported_as"), status=s("status") or "pending",
            error=s("error"),
        )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        p = Path(path)
        m = cls(p)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            m.account = data.get("account", "")
            m.expected_total = int(data.get("expected_total", 0) or 0)
            for row in data.get("entries", []):
                e = cls._entry_from_row(row)
                m.entries[e.file_id] = e
        return m

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": self.account,
            "expected_total": self.expected_total,
            "count": len(self.entries),
            "done": self.count_done(),
            "total_bytes": self.total_bytes(),
            "entries": [asdict(e) for e in self.entries.values()],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ---- queries -----------------------------------------------------
    def is_done(self, file_id: str) -> bool:
        e = self.entries.get(file_id)
        return e is not None and e.status == "done"

    def upsert(self, entry: Entry) -> None:
        self.entries[entry.file_id] = entry

    def done_entries(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.status == "done"]

    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries.values() if e.status == "done")

    def count_done(self) -> int:
        return len(self.done_entries())

    def failed_entries(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.status != "done"]

    def is_complete(self) -> tuple[bool, str]:
        """A backup is complete only if every expected file downloaded OK."""
        failed = self.failed_entries()
        if failed:
            return False, f"{len(failed)} fichier(s) en échec/incomplets"
        if self.expected_total and self.count_done() < self.expected_total:
            return False, (f"{self.count_done()}/{self.expected_total} fichiers sauvegardés "
                           "(des fichiers attendus manquent au manifest)")
        return True, "complet"
