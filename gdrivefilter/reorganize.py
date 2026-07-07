"""Reorganize into a clean tree -- always as a COPY. Source mirror is untouched.

Files are classified by category (extension/mime) and optionally bucketed by
year. Name collisions are resolved with a short hash suffix. Exact duplicates
(from the dedup report) are routed to a quarantine folder instead of the clean
tree -- still copied, never deleted.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .dedup import DedupReport
from .logging_conf import get_logger
from .manifest import Entry, Manifest

log = get_logger("reorganize")

CATEGORIES: dict[str, set[str]] = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic", ".svg"},
    "Videos": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages"},
    "Spreadsheets": {".xls", ".xlsx", ".ods", ".csv", ".numbers"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".sh", ".html", ".css", ".json", ".yaml", ".yml"},
    "Data": {".db", ".sqlite", ".parquet", ".xml"},
}


@dataclass
class ReorgReport:
    dest_root: Path
    copied: int = 0
    quarantined_dup: int = 0
    quarantined_junk: int = 0
    bytes_copied: int = 0
    bytes_quarantined: int = 0
    mapping: list[tuple[str, str]] = field(default_factory=list)  # (src_rel, dest_rel)

    @property
    def quarantined(self) -> int:
        return self.quarantined_dup + self.quarantined_junk


def category_for(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def _dest_rel(entry: Entry, by_year: bool) -> str:
    cat = category_for(entry.rel_path)
    name = Path(entry.rel_path).name
    if by_year:
        year = (entry.modified_time or "0000")[:4] or "unknown"
        return f"{cat}/{year}/{name}"
    return f"{cat}/{name}"


def _unique(dest_root: Path, rel: str, sha: str, taken: set[str]) -> str:
    """Unique destination path. `taken` holds CASEFOLDED paths: many targets
    (exFAT/APFS/NTFS) are case-insensitive, so 'Report.pdf' and 'report.pdf'
    must not map to the same physical file (also keeps dry-run mappings exact)."""
    if rel.casefold() not in taken and not (dest_root / rel).exists():
        taken.add(rel.casefold())
        return rel
    p = Path(rel)
    suffix = (sha or "dup")[:8]
    candidate = str(p.with_name(f"{p.stem}__{suffix}{p.suffix}"))
    n = 1
    while candidate.casefold() in taken or (dest_root / candidate).exists():
        candidate = str(p.with_name(f"{p.stem}__{suffix}_{n}{p.suffix}"))
        n += 1
    taken.add(candidate.casefold())
    return candidate


def _safe_plan_dest(dest_rel: str) -> str:
    """A user-edited plan destination must stay INSIDE dest_root."""
    p = Path(dest_rel)
    if p.is_absolute() or ".." in p.parts or not str(p).strip():
        raise ValueError(f"Destination de plan invalide (hors de l'arbre): {dest_rel!r}")
    return str(p)


def reorganize(source_dir: Path, dest_root: Path, manifest: Manifest,
               dedup: DedupReport | None = None, junk: dict[str, str] | None = None,
               by_year: bool = True, dry_run: bool = False,
               plan: list[dict] | None = None) -> ReorgReport:
    """Copy every manifest file into a clean categorized tree under dest_root.

    Default routing: duplicates -> _quarantine/duplicates, junk ->
    _quarantine/junk, everything else -> Category[/Year]. When a user-edited
    `plan` is given (rows action/src_rel/dest_rel), it fully drives the routing:
    action=keep copies to dest_rel, quarantine copies under _quarantine/,
    skip leaves the file out of the clean tree. Always copy-only: dest_root MUST
    differ from source_dir (enforced) so the source mirror is never mutated.
    """
    source_dir = Path(source_dir).resolve()
    dest_root = Path(dest_root).resolve()
    if dest_root == source_dir or source_dir in dest_root.parents:
        raise ValueError("La destination de réorg doit être HORS du miroir source (sécurité).")

    plan_by_src: dict[str, dict] | None = None
    if plan is not None:
        plan_by_src = {}
        for row in plan:
            action = (row.get("action") or "keep").strip().lower()
            if action not in ("keep", "quarantine", "skip"):
                raise ValueError(f"Action de plan inconnue: {action!r} "
                                 "(attendu: keep | quarantine | skip)")
            plan_by_src[row.get("src_rel", "")] = row

    dup_rel = {p for g in (dedup.groups if dedup else []) for p in g.duplicates}
    junk = junk or {}
    report = ReorgReport(dest_root)
    taken: set[str] = set()

    for e in manifest.done_entries():
        src = source_dir / e.rel_path
        if not src.is_file():
            continue
        name = Path(e.rel_path).name
        if plan_by_src is not None:
            row = plan_by_src.get(e.rel_path)
            if row is None or row["action"] == "skip":
                continue  # user chose to leave this file out of the clean tree
            wanted = _safe_plan_dest(row.get("dest_rel") or _dest_rel(e, by_year))
            if row["action"] == "quarantine":
                if not wanted.startswith("_quarantine/"):
                    wanted = f"_quarantine/{wanted}"
                rel = _unique(dest_root, wanted, e.sha256, taken)
                report.quarantined_dup += 1
                report.bytes_quarantined += e.size
            else:  # keep
                rel = _unique(dest_root, wanted, e.sha256, taken)
                report.copied += 1
                report.bytes_copied += e.size
        elif e.rel_path in junk:
            rel = _unique(dest_root, f"_quarantine/junk/{name}", e.sha256, taken)
            report.quarantined_junk += 1
            report.bytes_quarantined += e.size
        elif e.rel_path in dup_rel:
            rel = _unique(dest_root, f"_quarantine/duplicates/{name}", e.sha256, taken)
            report.quarantined_dup += 1
            report.bytes_quarantined += e.size
        else:
            rel = _unique(dest_root, _dest_rel(e, by_year), e.sha256, taken)
            report.copied += 1
            report.bytes_copied += e.size
        report.mapping.append((e.rel_path, rel))
        if not dry_run:
            target = dest_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

    log.info("Réorg %s: %d copiés, %d doublons + %d junk en quarantaine, %.2f Go utiles%s",
             "(dry-run) " if dry_run else "", report.copied, report.quarantined_dup,
             report.quarantined_junk, report.bytes_copied / (1024**3),
             " -> " + str(dest_root))
    return report
