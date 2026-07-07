"""Duplicate detection. DETECTION ONLY -- never deletes.

Exact duplicates: identical sha256. Optional near-duplicate detection for
text-like files uses Ollama embeddings (bge-m3). Output is a report; the
primary copy is chosen deterministically (shortest path, then name).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .logging_conf import get_logger
from .manifest import Entry, Manifest
from .ollama_client import OllamaClient, cosine

log = get_logger("dedup")


@dataclass
class DuplicateGroup:
    kind: str            # "exact" | "near"
    key: str             # hash or similarity anchor
    primary: str         # rel_path kept
    duplicates: list[str] = field(default_factory=list)
    reclaimable_bytes: int = 0


@dataclass
class DedupReport:
    groups: list[DuplicateGroup] = field(default_factory=list)

    @property
    def duplicate_count(self) -> int:
        return sum(len(g.duplicates) for g in self.groups)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(g.reclaimable_bytes for g in self.groups)


def _choose_primary(entries: list[Entry]) -> Entry:
    return sorted(entries, key=lambda e: (e.rel_path.count("/"), len(e.rel_path), e.rel_path))[0]


def find_exact_duplicates(manifest: Manifest) -> DedupReport:
    by_hash: dict[str, list[Entry]] = {}
    for e in manifest.done_entries():
        if e.sha256:
            by_hash.setdefault(e.sha256, []).append(e)
    report = DedupReport()
    for h, entries in by_hash.items():
        if len(entries) < 2:
            continue
        primary = _choose_primary(entries)
        dups = [e for e in entries if e is not primary]
        report.groups.append(DuplicateGroup(
            kind="exact", key=h, primary=primary.rel_path,
            duplicates=[e.rel_path for e in dups],
            reclaimable_bytes=sum(e.size for e in dups),
        ))
    log.info("Doublons exacts: %d groupes, %d fichiers redondants, %.2f Go récupérables",
             len(report.groups), report.duplicate_count, report.reclaimable_bytes / (1024**3))
    return report


_TEXT_EXT = {".txt", ".md", ".csv", ".json", ".html", ".xml", ".log", ".rtf"}


def find_near_duplicates(manifest: Manifest, backup_dir: Path, ollama: OllamaClient,
                         threshold: float = 0.95, max_bytes: int = 200_000) -> DedupReport:
    """Optional semantic near-dup detection for small text files. Skips if Ollama is down."""
    report = DedupReport()
    if not ollama.available():
        log.info("Ollama indisponible -- dédup sémantique ignorée.")
        return report
    vectors: list[tuple[Entry, list[float]]] = []
    for e in manifest.done_entries():
        if Path(e.rel_path).suffix.lower() not in _TEXT_EXT or e.size > max_bytes:
            continue
        p = backup_dir / e.rel_path
        if not p.is_file():
            continue
        vec = ollama.embed(p.read_text(encoding="utf-8", errors="ignore")[:8000])
        if vec:
            vectors.append((e, vec))
    used: set[str] = set()
    for i, (ea, va) in enumerate(vectors):
        if ea.rel_path in used:
            continue
        near: list[Entry] = []
        for eb, vb in vectors[i + 1:]:
            if eb.rel_path in used:
                continue
            if cosine(va, vb) >= threshold:
                near.append(eb)
                used.add(eb.rel_path)
        if near:
            report.groups.append(DuplicateGroup(
                kind="near", key=ea.rel_path, primary=ea.rel_path,
                duplicates=[e.rel_path for e in near],
                reclaimable_bytes=sum(e.size for e in near),
            ))
    log.info("Quasi-doublons (sémantique): %d groupes", len(report.groups))
    return report
