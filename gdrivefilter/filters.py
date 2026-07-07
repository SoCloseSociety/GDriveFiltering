"""Relevance filters: detect junk/clutter to route OUT of the clean tree.

Detection only -- like the rest of the pipeline, nothing is deleted. Junk is
routed to a quarantine folder in the reorganized COPY so the user can review it.
"""
from __future__ import annotations

from pathlib import Path

_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", "icon\r"}
_JUNK_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part", ".partial", ".~lock"}
_JUNK_PREFIXES = ("~$", ".~")


def classify_junk(rel_path: str, size: int) -> tuple[bool, str]:
    """Return (is_junk, reason). Conservative: only obvious clutter."""
    name = Path(rel_path).name
    lname = name.lower()
    if lname in _JUNK_NAMES:
        return True, "fichier système"
    if any(name.startswith(p) for p in _JUNK_PREFIXES):
        return True, "fichier temporaire"
    if Path(name).suffix.lower() in _JUNK_SUFFIXES:
        return True, "fichier temporaire"
    if size == 0:
        return True, "fichier vide (0 octet)"
    return False, ""


def junk_paths(entries) -> dict[str, str]:
    """Map rel_path -> reason for every junk entry in a manifest's done entries."""
    out: dict[str, str] = {}
    for e in entries:
        is_junk, reason = classify_junk(e.rel_path, e.size)
        if is_junk:
            out[e.rel_path] = reason
    return out
