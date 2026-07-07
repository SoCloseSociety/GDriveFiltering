"""Lightweight live progress: a heartbeat file + cheap readers for `status`.

Reading the full manifest (tens of MB, 100k entries) just to show a progress
bar is wasteful, so:
  - the backup writes a tiny progress.json heartbeat as it runs, and
  - readers fall back to parsing ONLY the manifest's head scalars if no
    heartbeat exists (e.g. a run started before this feature).
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

GB = 1024 ** 3


def write_progress(backup_dir: Path, done: int, expected: int, errors: int,
                   bytes_written: int, elapsed_s: float) -> None:
    """Atomically write the heartbeat. Never raises (progress is best-effort)."""
    try:
        p = Path(backup_dir)
        p.mkdir(parents=True, exist_ok=True)
        payload = {
            "done": done, "expected": expected, "errors": errors,
            "bytes_written": bytes_written, "elapsed_s": round(elapsed_s, 1),
            "rate_bps": (bytes_written / elapsed_s) if elapsed_s > 0 else 0,
        }
        # Per-thread tmp name: the heartbeat thread and the main loop both write
        # this file; a shared tmp could interleave/race. Distinct tmp -> safe.
        tmp = p / f"progress.json.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(p / "progress.json")
    except OSError:
        pass


def _manifest_head(backup_dir: Path) -> dict | None:
    """Parse ONLY the top scalar fields of manifest.json (cheap, no full load)."""
    m = Path(backup_dir) / "manifest.json"
    if not m.is_file():
        return None
    try:
        with open(m, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4096)
    except OSError:
        return None

    def num(key: str) -> int:
        mt = re.search(rf'"{key}"\s*:\s*(\d+)', head)
        return int(mt.group(1)) if mt else 0

    return {"done": num("done"), "expected": num("expected_total"),
            "count": num("count"), "bytes_written": num("total_bytes")}


@dataclass
class Snapshot:
    backup_dir: Path
    done: int = 0
    expected: int = 0
    errors: int = 0
    bytes_written: int = 0
    rate_bps: float = 0.0
    source: str = "none"

    @property
    def pct(self) -> float:
        return (100.0 * self.done / self.expected) if self.expected else 0.0


def read_snapshot(backup_dir: Path) -> Snapshot | None:
    """Prefer the heartbeat; fall back to the manifest head."""
    p = Path(backup_dir)
    hb = p / "progress.json"
    if hb.is_file():
        try:
            d = json.loads(hb.read_text(encoding="utf-8"))
            return Snapshot(p, d.get("done", 0), d.get("expected", 0),
                            d.get("errors", 0), d.get("bytes_written", 0),
                            d.get("rate_bps", 0.0), source="heartbeat")
        except (OSError, ValueError):
            pass
    head = _manifest_head(p)
    if head is None:
        return None
    errs = max(0, head["count"] - head["done"])
    return Snapshot(p, head["done"], head["expected"], errs,
                    head["bytes_written"], 0.0, source="manifest")


def render_bar(pct: float, width: int = 32) -> str:
    filled = int(round(width * pct / 100.0))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def human_eta(remaining_bytes: float, rate_bps: float) -> str:
    if rate_bps <= 0:
        return "?"
    secs = remaining_bytes / rate_bps
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def format_line(snap: Snapshot, rate_bps: float | None = None) -> str:
    rate = rate_bps if rate_bps is not None else snap.rate_bps
    avg_size = (snap.bytes_written / snap.done) if snap.done else 0
    remaining = max(0, snap.expected - snap.done) * avg_size
    return (f"{render_bar(snap.pct)} {snap.pct:5.1f}%  "
            f"{snap.done}/{snap.expected} fichiers  "
            f"{snap.bytes_written/GB:6.2f} Go  "
            f"err={snap.errors}  "
            f"{rate/1e6:5.2f} Mo/s  ETA {human_eta(remaining, rate)}")
