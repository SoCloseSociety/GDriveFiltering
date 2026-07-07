"""Analyze a backup and PROPOSE a final clean tree -- read-only, writes nothing.

Produces stats (by category / source / year), exact-duplicate savings, junk to
quarantine, and a preview of the reorganized structure, as text + an HTML
dashboard. The user reviews this before running `reorganize` (which copies).
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

from .dedup import find_exact_duplicates
from .filters import junk_paths
from .manifest import Manifest
from .reorganize import category_for, reorganize

GB = 1024 ** 3
MB = 1024 ** 2


@dataclass
class Proposal:
    backup_dir: Path
    total_files: int = 0
    total_bytes: int = 0
    by_category: dict = field(default_factory=dict)   # cat -> [count, bytes]
    by_source: dict = field(default_factory=dict)     # drive_name -> [count, bytes]
    by_year: dict = field(default_factory=dict)       # year -> [count, bytes]
    dupe_files: int = 0
    dupe_reclaim: int = 0
    junk_files: int = 0
    junk_bytes: int = 0
    clean_files: int = 0
    clean_bytes: int = 0


def _bump(d: dict, key: str, size: int) -> None:
    row = d.setdefault(key, [0, 0])
    row[0] += 1
    row[1] += size


def build_proposal(backup_dir: Path, manifest: Manifest) -> Proposal:
    entries = manifest.done_entries()
    dedup = find_exact_duplicates(manifest)
    dup_rel = {p for g in dedup.groups for p in g.duplicates}
    junk = junk_paths(entries)

    p = Proposal(Path(backup_dir))
    for e in entries:
        p.total_files += 1
        p.total_bytes += e.size
        if e.rel_path in junk:
            p.junk_files += 1
            p.junk_bytes += e.size
            continue
        if e.rel_path in dup_rel:
            continue  # counted via dedup reclaim
        # This is a "clean" kept file.
        p.clean_files += 1
        p.clean_bytes += e.size
        _bump(p.by_category, category_for(e.rel_path), e.size)
        _bump(p.by_source, e.drive_name or "?", e.size)
        _bump(p.by_year, (e.modified_time or "????")[:4] or "????", e.size)

    p.dupe_files = dedup.duplicate_count
    p.dupe_reclaim = dedup.reclaimable_bytes
    return p


def render_text(p: Proposal) -> str:
    lines = [
        f"=== Proposition de clean tree -- {p.backup_dir} ===",
        f"Fichiers sauvegardés : {p.total_files}  ({p.total_bytes/GB:.2f} Go)",
        f"À garder (clean)     : {p.clean_files}  ({p.clean_bytes/GB:.2f} Go)",
        f"Doublons exacts      : {p.dupe_files} fichiers  (-{p.dupe_reclaim/GB:.2f} Go récupérables)",
        f"Junk / clutter       : {p.junk_files} fichiers  (-{p.junk_bytes/MB:.1f} Mo)",
        "",
        "Par catégorie :",
    ]
    for cat, (c, b) in sorted(p.by_category.items(), key=lambda kv: -kv[1][1]):
        lines.append(f"  {cat:<14} {c:>7} fichiers  {b/GB:>7.2f} Go")
    lines.append("\nPar source :")
    for s, (c, b) in sorted(p.by_source.items(), key=lambda kv: -kv[1][1]):
        lines.append(f"  {s:<20} {c:>7} fichiers  {b/GB:>7.2f} Go")
    lines.append("\n(Aucune suppression -- doublons et junk seront mis en quarantaine dans la COPIE.)")
    return "\n".join(lines)


def _bars(d: dict, unit_bytes: int = GB) -> str:
    if not d:
        return "<p>(vide)</p>"
    items = sorted(d.items(), key=lambda kv: -kv[1][1])
    top = items[0][1][1] or 1
    rows = []
    for k, (c, b) in items:
        w = max(1, int(100 * b / top))
        rows.append(
            f'<div class="row"><span class="lbl">{html.escape(str(k))}</span>'
            f'<span class="bar"><i style="width:{w}%"></i></span>'
            f'<span class="val">{c} · {b/unit_bytes:.2f} Go</span></div>'
        )
    return "\n".join(rows)


def render_html(p: Proposal, progress: dict | None = None) -> str:
    prog_html = ""
    if progress:
        pct = progress.get("pct", 0)
        prog_html = f"""
        <section class="card">
          <h2>Backup en cours</h2>
          <div class="prog"><i style="width:{pct:.1f}%"></i></div>
          <p>{progress.get('done',0)} / {progress.get('expected',0)} fichiers &middot;
             {progress.get('bytes_written',0)/GB:.2f} Go &middot; {pct:.1f}%</p>
        </section>"""

    kept_pct = (100 * p.clean_bytes / p.total_bytes) if p.total_bytes else 0
    return f"""<h1>Clean tree proposé</h1>
<p class="sub">{html.escape(str(p.backup_dir))}</p>
{prog_html}
<section class="grid">
  <div class="tile"><span class="n">{p.total_files}</span><span class="t">fichiers sauvegardés</span></div>
  <div class="tile"><span class="n">{p.total_bytes/GB:.1f} Go</span><span class="t">volume total</span></div>
  <div class="tile ok"><span class="n">{p.clean_files}</span><span class="t">à garder ({kept_pct:.0f}%)</span></div>
  <div class="tile warn"><span class="n">-{p.dupe_reclaim/GB:.1f} Go</span><span class="t">{p.dupe_files} doublons</span></div>
  <div class="tile warn"><span class="n">{p.junk_files}</span><span class="t">junk (-{p.junk_bytes/MB:.0f} Mo)</span></div>
</section>
<section class="card"><h2>Par catégorie</h2>{_bars(p.by_category)}</section>
<section class="card"><h2>Par source (drive)</h2>{_bars(p.by_source)}</section>
<section class="card"><h2>Par année</h2>{_bars(p.by_year)}</section>
<p class="foot">Aucune suppression. Doublons et junk vont en <code>_quarantine/</code> dans la COPIE réorganisée.</p>
{_STYLE}"""


_STYLE = """<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--card:#f6f7f9;--line:#e3e6ea;--acc:#3b82f6;--ok:#16a34a;--warn:#d97706}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#9aa4af;--card:#161b22;--line:#30363d;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922}}
:root[data-theme=dark]{--bg:#0d1117;--fg:#e6edf3;--mut:#9aa4af;--card:#161b22;--line:#30363d;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922}
:root[data-theme=light]{--bg:#fff;--fg:#111;--mut:#666;--card:#f6f7f9;--line:#e3e6ea;--acc:#3b82f6;--ok:#16a34a;--warn:#d97706}
*{box-sizing:border-box}body{margin:0}
h1{font:700 26px system-ui;margin:24px 20px 4px;color:var(--fg)}
.sub{margin:0 20px 16px;color:var(--mut);font:13px ui-monospace,monospace;word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:4px}
.tile .n{font:700 22px system-ui;color:var(--fg)}.tile .t{font:12px system-ui;color:var(--mut)}
.tile.ok .n{color:var(--ok)}.tile.warn .n{color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:16px 20px}
.card h2{font:600 15px system-ui;margin:0 0 12px;color:var(--fg)}
.row{display:grid;grid-template-columns:130px 1fr 150px;align-items:center;gap:10px;margin:6px 0;font:13px system-ui;color:var(--fg)}
.lbl{color:var(--mut)}.val{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums}
.bar{background:var(--line);border-radius:6px;height:12px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc)}
.prog{background:var(--line);border-radius:8px;height:16px;overflow:hidden;margin:8px 0}
.prog i{display:block;height:100%;background:var(--ok)}
.foot{margin:16px 20px 32px;color:var(--mut);font:13px system-ui}
code{background:var(--line);padding:2px 6px;border-radius:5px}
@media(max-width:560px){.row{grid-template-columns:90px 1fr;}.row .val{grid-column:2;text-align:left}}
</style>"""
