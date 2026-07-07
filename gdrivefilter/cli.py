"""Command-line entrypoint. `python -m gdrivefilter <command>`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config
from .logging_conf import get_logger
from .manifest import Manifest
from .ollama_client import OllamaClient
from .preflight import GB, _free_bytes

log = get_logger("cli")


def _write_report(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("Rapport écrit: %s", path)


def cmd_doctor(cfg, args) -> int:
    print("=== GDriveFiltering doctor ===")
    print(f"Credentials Google : {'OK' if cfg.client_id else 'MANQUANT'} (source: {cfg.creds_source})")
    print(f"Scope Drive        : {cfg.scope_name} ({cfg.scope[0]})")
    print(f"Backup principal   : {cfg.backup_root}  (libre {_free_bytes(cfg.backup_root)/GB:.1f} Go)")
    if cfg.backup_mirror_ext:
        print(f"Miroir externe     : {cfg.backup_mirror_ext}  (libre {_free_bytes(cfg.backup_mirror_ext)/GB:.1f} Go)")
    else:
        print("Miroir externe     : (aucun -- branche un disque dur et renseigne BACKUP_MIRROR_EXT)")
    ol = OllamaClient(cfg.ollama_host, cfg.ollama_embed_model, cfg.ollama_llm_model)
    print(f"Ollama             : {'disponible' if ol.available() else 'indisponible'} ({cfg.ollama_host})")
    return 0


def _latest_backup_dir(cfg, account: str):
    base = cfg.backup_root
    cands = []
    if base.exists():
        for dd in base.iterdir():
            if dd.name.startswith("_"):  # _clean/ etc. are not backups
                continue
            if (dd / account / "manifest.json").is_file():
                cands.append(dd.name)
    if not cands:
        return None
    cands.sort()
    return base / cands[-1] / account


def cmd_status(cfg, args) -> int:
    import time as _t

    from .progress import GB, format_line, read_snapshot
    d = _latest_backup_dir(cfg, args.account)
    if d is None:
        print(f"Aucun backup trouvé pour le compte '{args.account}'.")
        return 1
    snap = read_snapshot(d)
    if snap is None:
        print("Manifest illisible.")
        return 1
    if not args.watch:
        print(f"Backup: {d}")
        print(format_line(snap))
        return 0

    prev_bytes, prev_t = None, None
    try:
        while True:
            snap = read_snapshot(d)
            now = _t.monotonic()
            rate = snap.rate_bps
            if prev_bytes is not None and now > prev_t:
                rate = (snap.bytes_written - prev_bytes) / (now - prev_t)
            print("\r" + format_line(snap, rate).ljust(112), end="", flush=True)
            if snap.expected and snap.done >= snap.expected:
                print("\nBackup complet.")
                break
            prev_bytes, prev_t = snap.bytes_written, now
            _t.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    return 0


def cmd_dashboard(cfg, args) -> int:
    from .dashboard import serve
    serve(cfg, port=args.port, open_browser=not args.no_open)
    return 0


def _make_client(cfg, account: str):
    from .auth import get_credentials
    from .drive_client import DriveClient, GoogleDriveBackend
    creds = get_credentials(cfg, account)
    return DriveClient(GoogleDriveBackend(creds), export_format=cfg.google_export_format)


def cmd_auth(cfg, args) -> int:
    _make_client(cfg, args.account)
    print(f"Auth OK pour le compte '{args.account}'.")
    return 0


def _resolve_timestamp(cfg, account: str, force_new: bool) -> str:
    """Resume the latest INCOMPLETE backup for this account, else start a new one.

    Backups live at <backup_root>/<timestamp>/<account>/. This makes a plain
    `backup` re-run continue where it stopped instead of starting from scratch.
    """
    base = cfg.backup_root
    candidates = []
    if base.exists():
        for d in base.iterdir():
            # "_"-prefixed dirs (e.g. _clean/) are not backups and sort after
            # digits in ASCII -- they must never be picked as "latest".
            if d.name.startswith("_"):
                continue
            if (d / account / "manifest.json").is_file():
                candidates.append(d.name)
    candidates.sort()
    if candidates and not force_new:
        latest = candidates[-1]
        m = Manifest.load(base / latest / account / "manifest.json")
        complete, reason = m.is_complete()
        if not complete:
            log.info("Reprise du backup existant %s (%s -- %d/%d déjà faits).",
                     latest, reason, m.count_done(), m.expected_total or m.count_done())
            return latest
        log.info("Dernier backup %s déjà complet -> création d'un nouveau backup.", latest)
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def cmd_backup(cfg, args) -> int:
    from .extract import run_backup
    client = _make_client(cfg, args.account)
    ts = (datetime.now().strftime("%Y%m%d_%H%M%S") if args.dry_run
          else _resolve_timestamp(cfg, args.account, args.new))
    res = run_backup(cfg, client, account=args.account, timestamp=ts, dry_run=args.dry_run)
    print(f"\nBackup: dl={res.downloaded} skip={res.skipped} err={res.errors} "
          f"repair={res.repaired} ({res.bytes_written/GB:.2f} Go) -> {res.backup_dir}")
    if res.error_samples:
        print("Erreurs (échantillon):")
        for s in res.error_samples:
            print("  -", s)
    if not args.dry_run:
        from .verify import verify_backup
        rep = verify_backup(res.backup_dir)
        print(("BACKUP COMPLET ET VÉRIFIÉ." if rep.clean
               else f"ATTENTION: backup INCOMPLET/non vérifié -> {rep.summary()}\n"
                    "Relance `backup` (reprise auto) jusqu'à obtenir un backup complet "
                    "avant toute réorganisation ou purge."))
    return 0


def cmd_verify(cfg, args) -> int:
    from .verify import verify_backup
    d = Path(args.dir)
    rep = verify_backup(d, check_hash=not args.no_hash)
    print(rep.summary())
    _write_report(d / "reports" / "verify.json", {
        "clean": rep.clean, "complete": rep.complete, "complete_reason": rep.complete_reason,
        "total": rep.total, "ok": rep.ok,
        "missing": len(rep.missing), "size_mismatch": len(rep.size_mismatch),
        "hash_mismatch": len(rep.hash_mismatch),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    })
    return 0 if rep.clean else 2


def cmd_dedup(cfg, args) -> int:
    from .dedup import find_exact_duplicates, find_near_duplicates
    d = Path(args.dir)
    manifest = Manifest.load(d / "manifest.json")
    report = find_exact_duplicates(manifest)
    groups = [g.__dict__ for g in report.groups]
    if args.semantic:
        ol = OllamaClient(cfg.ollama_host, cfg.ollama_embed_model, cfg.ollama_llm_model)
        near = find_near_duplicates(manifest, d, ol)
        groups += [g.__dict__ for g in near.groups]
    _write_report(d / "reports" / "dedup.json", {
        "exact_groups": len(report.groups),
        "duplicate_files": report.duplicate_count,
        "reclaimable_gb": round(report.reclaimable_bytes / GB, 3),
        "groups": groups,
    })
    print(f"Doublons exacts: {report.duplicate_count} fichiers, "
          f"{report.reclaimable_bytes/GB:.2f} Go récupérables. (aucune suppression effectuée)")
    return 0


def cmd_reorganize(cfg, args) -> int:
    from .dedup import find_exact_duplicates
    from .filters import junk_paths
    from .reorganize import reorganize
    d = Path(args.dir)
    manifest = Manifest.load(d / "manifest.json")
    dedup = find_exact_duplicates(manifest)
    junk = junk_paths(manifest.done_entries())
    rep = reorganize(d, Path(args.dest), manifest, dedup=dedup, junk=junk,
                     by_year=not args.no_year, dry_run=args.dry_run)
    _write_report(Path(args.dest) / "reports" / "reorganize.json", {
        "copied": rep.copied, "quarantined_dup": rep.quarantined_dup,
        "quarantined_junk": rep.quarantined_junk,
        "gb": round(rep.bytes_copied / GB, 3),
        "mapping_sample": rep.mapping[:50],
    })
    print(f"Réorg{' (dry-run)' if args.dry_run else ''}: {rep.copied} copiés, "
          f"{rep.quarantined_dup} doublons + {rep.quarantined_junk} junk en quarantaine -> {args.dest}")
    return 0


def cmd_propose(cfg, args) -> int:
    import dataclasses
    import json as _json

    from .progress import read_snapshot
    from .propose import build_proposal, render_html, render_text
    d = Path(args.dir) if args.dir else _latest_backup_dir(cfg, args.account)
    if d is None:
        print(f"Aucun backup trouvé pour le compte '{args.account}'.")
        return 1
    manifest = Manifest.load(d / "manifest.json")
    prop = build_proposal(d, manifest)
    print(render_text(prop))

    reports = d / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "proposal.txt").write_text(render_text(prop), encoding="utf-8")
    (reports / "proposal.json").write_text(
        _json.dumps(dataclasses.asdict(prop), default=str, indent=2, ensure_ascii=False),
        encoding="utf-8")
    snap = read_snapshot(d)
    prog = ({"pct": snap.pct, "done": snap.done, "expected": snap.expected,
             "bytes_written": snap.bytes_written} if snap else None)
    html_path = Path(args.html) if args.html else reports / "proposal.html"
    html_path.write_text(render_html(prop, prog), encoding="utf-8")
    print(f"\nRapports écrits: {reports}/proposal.(txt|json|html)")
    print(f"Dashboard HTML : {html_path}")
    return 0


def cmd_purge(cfg, args) -> int:
    from .clean import PurgeRefused, purge_duplicates
    from .dedup import find_exact_duplicates
    primary = Path(args.primary).resolve()
    manifest = Manifest.load(primary / "manifest.json")
    dedup = find_exact_duplicates(manifest)
    # Derive the external mirror's matching timestamped subdir from the primary path.
    ext = None
    if cfg.backup_mirror_ext:
        try:
            rel = primary.relative_to(cfg.backup_root.resolve())
            ext = cfg.backup_mirror_ext / rel
        except ValueError:
            ext = cfg.backup_mirror_ext
    try:
        res = purge_duplicates(cfg, primary, ext, Path(args.dest), dedup,
                               confirm=args.i_have_a_verified_backup, dry_run=not args.apply)
    except PurgeRefused as e:
        print(f"PURGE REFUSÉE -- {e}")
        return 3
    print(f"Purge {'EFFECTIVE' if args.apply else '(dry-run)'}: "
          f"{len(res.deleted)} fichiers, {res.freed_bytes/GB:.2f} Go.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdrivefilter", description="Backup + clean Google Drive locally")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Vérifie config, credentials, espace disque, Ollama")

    st = sub.add_parser("status", help="Suivi de progression du backup (live avec --watch)")
    st.add_argument("--account", default="default")
    st.add_argument("--watch", action="store_true", help="Rafraîchit en continu")
    st.add_argument("--interval", type=float, default=5.0)

    dash = sub.add_parser("dashboard", help="Dashboard web local (monitoring + quick actions)")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--no-open", action="store_true", help="Ne pas ouvrir le navigateur")

    a = sub.add_parser("auth", help="Consent OAuth pour un compte")
    a.add_argument("--account", default="default")

    b = sub.add_parser("backup", help="Extrait (backup) tous les drives -- READ ONLY")
    b.add_argument("--account", default="default")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--new", action="store_true",
                   help="Forcer un nouveau backup au lieu de reprendre le dernier incomplet")

    v = sub.add_parser("verify", help="Vérifie un dossier de backup (count/size/sha256)")
    v.add_argument("--dir", required=True)
    v.add_argument("--no-hash", action="store_true")

    dd = sub.add_parser("dedup", help="Détecte les doublons (aucune suppression)")
    dd.add_argument("--dir", required=True)
    dd.add_argument("--semantic", action="store_true", help="Ajoute la dédup sémantique Ollama")

    pr = sub.add_parser("propose", help="Analyse le backup et propose le clean tree (rapport + HTML)")
    pr.add_argument("--account", default="default")
    pr.add_argument("--dir", help="Dossier de backup précis (sinon dernier du compte)")
    pr.add_argument("--html", help="Chemin de sortie du dashboard HTML")

    r = sub.add_parser("reorganize", help="Produit une arbo propre EN COPIE")
    r.add_argument("--dir", required=True)
    r.add_argument("--dest", required=True)
    r.add_argument("--no-year", action="store_true")
    r.add_argument("--dry-run", action="store_true")

    pg = sub.add_parser("purge", help="Supprime les doublons (COPIE uniquement, ultra-gardé)")
    pg.add_argument("--primary", required=True, help="Dossier de backup principal vérifié")
    pg.add_argument("--dest", required=True, help="Arbre réorganisé (copie) où purger")
    pg.add_argument("--i-have-a-verified-backup", action="store_true")
    pg.add_argument("--apply", action="store_true", help="Sans ce flag: dry-run")
    return p


_HANDLERS = {
    "doctor": cmd_doctor, "auth": cmd_auth, "backup": cmd_backup, "verify": cmd_verify,
    "status": cmd_status, "dashboard": cmd_dashboard, "dedup": cmd_dedup,
    "propose": cmd_propose, "reorganize": cmd_reorganize, "purge": cmd_purge,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return _HANDLERS[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
