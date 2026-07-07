"""Local control-panel dashboard: monitor + manage the backup from a browser.

Runs a stdlib HTTP server bound to 127.0.0.1 (never exposed to the network),
serving a live page plus a small JSON API. Quick actions spawn the CLI as
subprocesses (whitelisted, non-destructive only -- purge is intentionally NOT
exposed). No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import PROJECT_ROOT, Config
from .logging_conf import get_logger
from .preflight import GB, _free_bytes
from .progress import read_snapshot

log = get_logger("dashboard")

_LOG_DIR = PROJECT_ROOT / ".dashboard" / "logs"
# action key -> subprocess.Popen (module-level so it survives across requests)
_RUNNING: dict[str, subprocess.Popen] = {}
_LOCK = threading.Lock()

# Whitelisted, non-destructive actions. Each returns argv given (cfg, account, latest_dir).
_PY = sys.executable


def _clean_account(a: str) -> str:
    """Whitelist account labels so they can never traverse paths or inject args."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(a))[:64] or "default"


def _discover_accounts(cfg: Config) -> dict[str, Path]:
    """account name -> latest backup dir containing a manifest."""
    base = cfg.backup_root
    latest: dict[str, str] = {}
    dirs: dict[str, Path] = {}
    if base.exists():
        for ts_dir in base.iterdir():
            # Skip non-backup dirs (e.g. _clean/ written by reorganize): "_" sorts
            # AFTER digits in ASCII, so it would otherwise win the "latest" pick.
            if not ts_dir.is_dir() or ts_dir.name.startswith("_"):
                continue
            for acc_dir in ts_dir.iterdir():
                if (acc_dir / "manifest.json").is_file():
                    acc = acc_dir.name
                    if acc not in latest or ts_dir.name > latest[acc]:
                        latest[acc] = ts_dir.name
                        dirs[acc] = acc_dir
    return dirs


def _is_running(backup_dir: Path) -> bool:
    """Recently active? The backup writes a time-based heartbeat every ~15s, so a
    fresh progress.json/manifest mtime is a cheap, accurate signal (no tree walk)."""
    import time
    now = time.time()
    for name in ("progress.json", "manifest.json"):
        f = backup_dir / name
        if f.is_file():
            try:
                if now - f.stat().st_mtime < 60:
                    return True
            except OSError:
                pass
    return False


def _account_state(cfg: Config, account: str, backup_dir: Path) -> dict:
    snap = read_snapshot(backup_dir)
    return {
        "account": account,
        "backup_dir": str(backup_dir),
        "done": snap.done if snap else 0,
        "expected": snap.expected if snap else 0,
        "errors": snap.errors if snap else 0,
        "bytes": snap.effective_bytes if snap else 0,          # completed + in-flight
        "bytes_done": snap.bytes_written if snap else 0,
        "bytes_inflight": snap.bytes_inflight if snap else 0,
        "expected_bytes": snap.expected_bytes if snap else 0,
        "rate_bps": snap.rate_bps if snap else 0,
        "pct": round(snap.pct, 2) if snap else 0,
        "pct_bytes": round(snap.pct_bytes, 2) if snap else 0,
        "running": _is_running(backup_dir),
    }


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pipeline(cfg: Config, account: str, backup_dir: Path | None) -> dict:
    """State of every funnel stage so the UI can guide the user step by step."""
    acc = _clean_account(account)
    has_token = (cfg.token_dir / f"token_{acc}.json").is_file()
    snap = read_snapshot(backup_dir) if backup_dir else None
    has_manifest = bool(backup_dir and (backup_dir / "manifest.json").is_file())
    running = bool(backup_dir and _is_running(backup_dir))
    complete = bool(snap and snap.expected > 0 and snap.done >= snap.expected and snap.errors == 0)
    verify_res = _read_json(backup_dir / "reports" / "verify.json") if backup_dir else None
    verify_clean = bool(verify_res and verify_res.get("clean"))
    proposal = _read_json(backup_dir / "reports" / "proposal.json") if backup_dir else None
    clean_dir = cfg.backup_root / "_clean" / acc
    reorganized = clean_dir.is_dir() and any(clean_dir.iterdir())

    def stage(key, label, status, detail, action=None, action_label=None):
        return {"key": key, "label": label, "status": status, "detail": detail,
                "action": action, "actionLabel": action_label}

    backup_status = ("done" if complete else "active" if running
                     else "partial" if has_manifest else "todo")
    backup_detail = (f"{snap.done}/{snap.expected} · {snap.pct_bytes:.1f}%" if snap else "à lancer")
    stages = [
        stage("auth", "Authentification", "done" if has_token else "todo",
              f"compte {acc}" if has_token else "consentement requis",
              None if has_token else None),
        stage("backup", "Backup", backup_status, backup_detail,
              None if complete else "backup", "Résumer" if has_manifest else "Lancer"),
        stage("verify", "Vérification",
              "done" if verify_clean else "todo" if has_manifest else "blocked",
              (f"{verify_res.get('ok')}/{verify_res.get('total')} OK" if verify_res
               else "intégrité + complétude"),
              "verify" if has_manifest else None, "Vérifier"),
        stage("propose", "Proposition",
              "done" if proposal else "todo" if has_manifest else "blocked",
              (f"{proposal.get('clean_files')} à garder" if proposal else "clean tree"),
              "propose" if has_manifest else None, "Proposer"),
        stage("reorganize", "Réorganisation",
              "done" if reorganized else "todo" if verify_clean else "blocked",
              ("copie propre créée" if reorganized
               else "requiert une vérif OK" if not verify_clean else "arbo par catégorie"),
              "reorganize_dry" if verify_clean else None, "Réorganiser"),
    ]
    return {"account": acc, "stages": stages}


def _state(cfg: Config) -> dict:
    accounts = _discover_accounts(cfg)
    with _LOCK:
        running_actions = {k: (p.poll() is None) for k, p in _RUNNING.items()}
    return {
        "accounts": [_account_state(cfg, a, d) for a, d in sorted(accounts.items())],
        "disk": {
            "path": str(cfg.backup_root),
            "free_gb": round(_free_bytes(cfg.backup_root) / GB, 1),
        },
        "actions": running_actions,
        "readonly": cfg.scope_name == "readonly",
    }


def _proposal(cfg: Config, backup_dir: Path) -> dict:
    from .manifest import Manifest
    from .propose import build_proposal
    m = Manifest.load(backup_dir / "manifest.json")
    p = build_proposal(backup_dir, m)
    return {
        "total_files": p.total_files, "total_gb": round(p.total_bytes / GB, 2),
        "clean_files": p.clean_files, "clean_gb": round(p.clean_bytes / GB, 2),
        "dupe_files": p.dupe_files, "dupe_reclaim_gb": round(p.dupe_reclaim / GB, 2),
        "junk_files": p.junk_files, "junk_mb": round(p.junk_bytes / (1024**2), 1),
        "by_category": {k: [v[0], round(v[1] / GB, 2)] for k, v in p.by_category.items()},
        "by_source": {k: [v[0], round(v[1] / GB, 2)] for k, v in p.by_source.items()},
        "by_year": {k: [v[0], round(v[1] / GB, 2)] for k, v in sorted(p.by_year.items())},
    }


def _action_argv(cfg: Config, action: str, account: str, backup_dir: Path | None):
    d = str(backup_dir) if backup_dir else None
    table = {
        "backup": ["caffeinate", "-i", _PY, "-m", "gdrivefilter", "backup", "--account", account],
        "verify": [_PY, "-m", "gdrivefilter", "verify", "--dir", d] if d else None,
        "propose": [_PY, "-m", "gdrivefilter", "propose", "--account", account],
        "dedup": [_PY, "-m", "gdrivefilter", "dedup", "--dir", d] if d else None,
        "reorganize_dry": ([_PY, "-m", "gdrivefilter", "reorganize", "--dir", d,
                            "--dest", str(cfg.backup_root / "_clean" / account), "--dry-run"]
                           if d else None),
    }
    return table.get(action)


def _launch(cfg: Config, action: str, account: str) -> dict:
    account = _clean_account(account)
    if action == "open_folder":
        d = _discover_accounts(cfg).get(account)
        if d:
            subprocess.Popen(["open", str(d)])
        return {"ok": True, "message": "Dossier ouvert dans le Finder."}

    backup_dir = _discover_accounts(cfg).get(account)
    # Never start a second backup while one is already running (external or here):
    # two processes writing the same manifest/dir would corrupt state.
    if action == "backup" and backup_dir and _is_running(backup_dir):
        return {"ok": False, "message": "Un backup est déjà en cours pour ce compte."}

    argv = _action_argv(cfg, action, account, backup_dir)
    if not argv:
        return {"ok": False, "message": f"Action inconnue ou backup introuvable: {action}"}

    key = f"{action}:{account}"
    with _LOCK:
        cur = _RUNNING.get(key)
        if cur and cur.poll() is None:
            return {"ok": False, "message": "Déjà en cours."}
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(_LOG_DIR / f"{key.replace(':', '_')}.log", "wb")
        env = dict(os.environ)
        proc = subprocess.Popen(argv, cwd=str(PROJECT_ROOT), stdout=logf,
                                stderr=subprocess.STDOUT, env=env)
        logf.close()  # the child holds its own dup of the fd
        _RUNNING[key] = proc
    return {"ok": True, "message": f"Lancé: {action} ({account})."}


def _read_log(action: str, account: str, offset: int) -> dict:
    account = _clean_account(account)
    key = f"{action}:{account}".replace(":", "_")
    f = _LOG_DIR / f"{key}.log"
    if not f.is_file():
        return {"offset": 0, "text": ""}
    try:
        with open(f, "rb") as fh:
            fh.seek(max(0, offset))
            chunk = fh.read(1_000_000)  # cap per poll
        return {"offset": max(0, offset) + len(chunk),
                "text": chunk.decode("utf-8", errors="replace")}
    except OSError:
        return {"offset": offset, "text": ""}


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path in ("/", "/index.html"):
                    return self._send(200, _PAGE, "text/html")
                if u.path == "/api/state":
                    return self._send(200, json.dumps(_state(cfg)))
                if u.path == "/api/pipeline":
                    acc = _clean_account(q.get("account", ["default"])[0])
                    d = _discover_accounts(cfg).get(acc)
                    return self._send(200, json.dumps(_pipeline(cfg, acc, d)))
                if u.path == "/api/proposal":
                    acc = q.get("account", ["default"])[0]
                    d = _discover_accounts(cfg).get(acc)
                    if not d:
                        return self._send(404, json.dumps({"error": "no backup"}))
                    return self._send(200, json.dumps(_proposal(cfg, d)))
                if u.path == "/api/log":
                    acc = q.get("account", ["default"])[0]
                    act = q.get("action", ["backup"])[0]
                    try:
                        off = int(q.get("offset", ["0"])[0])
                    except ValueError:
                        off = 0
                    return self._send(200, json.dumps(_read_log(act, acc, off)))
                return self._send(404, json.dumps({"error": "not found"}))
            except Exception as e:  # noqa: BLE001 - one bad request must not kill the server
                return self._send(500, json.dumps({"error": str(e)}))

        def do_POST(self):
            try:
                if urlparse(self.path).path != "/api/action":
                    return self._send(404, json.dumps({"error": "not found"}))
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"ok": False, "message": "JSON invalide"}))
                action = str(body.get("action", ""))
                account = str(body.get("account", "default"))
                allowed = {"backup", "verify", "propose", "dedup", "reorganize_dry", "open_folder"}
                if action not in allowed:
                    return self._send(400, json.dumps({"ok": False, "message": "action refusée"}))
                return self._send(200, json.dumps(_launch(cfg, action, account)))
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"ok": False, "message": str(e)}))

    return Handler


def serve(cfg: Config, port: int = 8787, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    url = f"http://127.0.0.1:{port}/"
    log.info("Dashboard: %s  (Ctrl-C pour arrêter)", url)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Dashboard arrêté.")
    finally:
        httpd.server_close()


_PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GDriveFiltering — Control room</title>
<style>
:root{
 --bg:#0a0d15;--panel:#111725;--panel2:#0d1320;--line:#212a3d;--txt:#eaeefb;--mut:#8b95ad;
 --accent:#7c5cff;--accent2:#22d3ee;--done:#34d399;--active:#fbbf24;--err:#fb7185;--blocked:#4a5468;
 --grad:linear-gradient(90deg,#7c5cff,#22d3ee);
}
:root[data-theme=light]{
 --bg:#f4f6fb;--panel:#ffffff;--panel2:#eef1f8;--line:#e0e5f0;--txt:#141a29;--mut:#5a647a;
 --accent:#6d4bff;--accent2:#0ea5c4;--done:#0f9d68;--active:#c07c0a;--err:#d64560;--blocked:#aeb6c7;
 --grad:linear-gradient(90deg,#6d4bff,#0ea5c4);
}
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){
 --bg:#f4f6fb;--panel:#ffffff;--panel2:#eef1f8;--line:#e0e5f0;--txt:#141a29;--mut:#5a647a;
 --accent:#6d4bff;--accent2:#0ea5c4;--done:#0f9d68;--active:#c07c0a;--err:#d64560;--blocked:#aeb6c7;
 --grad:linear-gradient(90deg,#6d4bff,#0ea5c4);}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.eyebrow{font:600 11px system-ui;letter-spacing:.12em;text-transform:uppercase;color:var(--mut)}
header{display:flex;align-items:center;gap:14px;padding:16px 26px;border-bottom:1px solid var(--line);flex-wrap:wrap;background:var(--panel2)}
header .logo{font-size:18px;font-weight:800;letter-spacing:-.01em}
.pill{font:600 11px system-ui;padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.ok{color:var(--done);border-color:color-mix(in srgb,var(--done) 45%,transparent)}
.grow{flex:1}
main{max-width:1080px;margin:0 auto;padding:22px 26px;display:flex;flex-direction:column;gap:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px}
/* hero */
.hero{display:grid;grid-template-columns:1.4fr 1fr;gap:22px}
@media(max-width:720px){.hero{grid-template-columns:1fr}}
.hero .big{font:800 46px/1 system-ui;letter-spacing:-.02em;margin:6px 0 2px}
.hero .sub{color:var(--mut);font-size:13px}
.track{height:14px;border-radius:8px;background:var(--panel2);overflow:hidden;margin:16px 0 6px;border:1px solid var(--line)}
.track i{display:block;height:100%;background:var(--grad);transition:width .5s ease;box-shadow:0 0 18px color-mix(in srgb,var(--accent) 55%,transparent)}
.kv{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:14px}
.kv div{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
.kv .n{font:700 18px system-ui}.kv .t{font-size:11px;color:var(--mut)}
.kv .n.err{color:var(--err)}
canvas{width:100%;height:70px;display:block;margin-top:6px}
/* funnel */
.funnel{display:flex;gap:0;align-items:stretch;overflow-x:auto;padding-bottom:4px}
.step{flex:1;min-width:150px;position:relative;padding:6px 14px 2px}
.step:not(:last-child):after{content:"";position:absolute;top:24px;right:-2px;width:calc(100% - 34px);height:2px;background:var(--line);left:44px}
.step.filled:not(:last-child):after{background:var(--grad)}
.node{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:15px;border:2px solid var(--line);background:var(--panel2);position:relative;z-index:1}
.step.done .node{border-color:var(--done);color:var(--done)}
.step.active .node,.step.partial .node{border-color:var(--active);color:var(--active)}
.step.active .node{animation:pulse 1.7s infinite}
.step.blocked .node{color:var(--blocked)}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--active) 55%,transparent)}70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
.step h3{font:600 13.5px system-ui;margin:12px 0 2px}
.step.blocked h3,.step.blocked .d{color:var(--mut)}
.step .d{font-size:12px;color:var(--mut);min-height:32px}
.step .st{font:600 10.5px system-ui;letter-spacing:.06em;text-transform:uppercase}
.step.done .st{color:var(--done)}.step.active .st,.step.partial .st{color:var(--active)}.step.todo .st{color:var(--accent2)}.step.blocked .st{color:var(--blocked)}
button{font:600 12.5px system-ui;padding:7px 13px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);cursor:pointer;transition:.15s;margin-top:8px}
button:hover{border-color:var(--accent);color:var(--accent)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button.primary{background:var(--grad);color:#fff;border:none}
button.primary:hover{filter:brightness(1.08);color:#fff}
/* bars */
.rows .r{display:grid;grid-template-columns:130px 1fr 130px;gap:10px;align-items:center;margin:6px 0;font-size:13px}
.rows .lbl{color:var(--mut)}.rows .val{text-align:right;color:var(--mut)}
.bar{background:var(--panel2);border-radius:6px;height:11px;overflow:hidden;border:1px solid var(--line)}.bar i{display:block;height:100%;background:var(--grad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tiles div{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}
.tiles .n{font:700 20px system-ui}.tiles .t{font-size:11px;color:var(--mut)}.tiles .n.warn{color:var(--active)}.tiles .n.ok{color:var(--done)}
pre{background:#05070c;color:#b7f0d8;border:1px solid var(--line);border-radius:12px;padding:14px;max-height:240px;overflow:auto;font:12px ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
.muted{color:var(--mut);font-size:12px}
h2.sec{font:700 13px system-ui;letter-spacing:.02em;margin:0 0 14px}
.foot{color:var(--mut);font-size:12px;text-align:center;padding:8px 0 28px}
code{background:var(--panel2);border:1px solid var(--line);padding:1px 6px;border-radius:6px}
</style></head><body>
<header>
  <span class="logo">🛰️ GDriveFiltering</span>
  <span class="pill ok" id="ro">read-only</span>
  <span class="pill mono" id="disk">disk —</span>
  <span class="grow"></span>
  <span class="muted mono" id="clock"></span>
</header>
<main>
  <section class="card hero" id="hero"><div class="muted">Chargement…</div></section>

  <section class="card">
    <div class="eyebrow">Parcours</div>
    <h2 class="sec">Funnel — de l'authentification au clean tree</h2>
    <div class="funnel" id="funnel"></div>
  </section>

  <section class="card" id="proposalCard" style="display:none">
    <div class="eyebrow">Analyse</div>
    <h2 class="sec">Clean tree proposé</h2>
    <div class="tiles" id="propTiles"></div>
    <h2 class="sec" style="margin-top:18px">Par catégorie</h2><div class="rows" id="byCat"></div>
    <h2 class="sec" style="margin-top:18px">Par source</h2><div class="rows" id="bySrc"></div>
  </section>

  <section class="card">
    <div class="eyebrow">Journal</div>
    <h2 class="sec">Console <span class="muted mono" id="logfor"></span></h2>
    <pre id="log">Lance une étape du funnel pour suivre ses logs ici.</pre>
  </section>
  <div class="foot">Actions non destructives. La suppression (<code>purge</code>) reste réservée au CLI gardé (miroir vérifié requis).</div>
</main>
<script>
const $=s=>document.querySelector(s), api=(p,o)=>fetch(p,o).then(r=>r.json());
const GB=1073741824; let hist=[], logCtx=null, logOff=0, ACCT="perso";
const gb=b=>(b/GB).toFixed(b<10*GB?2:1);
function eta(s){if(!isFinite(s)||s<=0)return'—';s=Math.round(s);const d=(s/86400)|0,h=((s%86400)/3600)|0,m=((s%3600)/60)|0;return d?`${d}j ${h}h`:h?`${h}h${String(m).padStart(2,'0')}`:`${m}m`;}
function heroView(a){
 if(!a)return '<div class="muted">Aucun backup pour l\'instant. Lance une authentification puis un backup.</div>';
 ACCT=a.account;
 const rem=Math.max(0,a.expected_bytes-a.bytes), etaS=a.rate_bps>0?rem/a.rate_bps:0;
 const pctB=a.pct_bytes||a.pct;
 return `<div>
   <div class="eyebrow">${a.running?'● en cours':'○ à l\'arrêt'} · ${a.account}</div>
   <div class="big mono">${pctB.toFixed(1)}<span style="font-size:22px">%</span></div>
   <div class="sub mono">${a.bytes?gb(a.bytes):0} / ${a.expected_bytes?gb(a.expected_bytes):'?'} Go &nbsp;·&nbsp; ${a.done.toLocaleString()} / ${a.expected.toLocaleString()} fichiers</div>
   <div class="track"><i style="width:${Math.min(100,pctB)}%"></i></div>
   <div class="kv">
     <div><div class="n mono">${(a.rate_bps/1e6).toFixed(1)} Mo/s</div><div class="t">débit</div></div>
     <div><div class="n mono">${eta(etaS)}</div><div class="t">temps restant estimé</div></div>
     <div><div class="n mono ${a.errors?'err':''}">${a.errors}</div><div class="t">erreurs</div></div>
     <div><div class="n mono">${a.running?'live':'idle'}</div><div class="t">état</div></div>
   </div>
 </div>
 <div><div class="eyebrow" style="margin-bottom:6px">Débit (Mo/s)</div><canvas id="spark" height="140"></canvas></div>`;
}
function drawSpark(){const c=$('#spark');if(!c)return;const x=c.getContext('2d'),W=c.width=c.clientWidth*2,H=c.height=280;x.clearRect(0,0,W,H);if(hist.length<2)return;const mx=Math.max(...hist,0.1);const st=getComputedStyle(document.documentElement);const g=x.createLinearGradient(0,0,W,0);g.addColorStop(0,st.getPropertyValue('--accent'));g.addColorStop(1,st.getPropertyValue('--accent2'));x.beginPath();hist.forEach((v,i)=>{const px=i/(hist.length-1)*W,py=H-(v/mx)*H*0.86-8;i?x.lineTo(px,py):x.moveTo(px,py);});x.strokeStyle=g;x.lineWidth=3.5;x.lineJoin='round';x.stroke();x.lineTo(W,H);x.lineTo(0,H);x.closePath();x.globalAlpha=.13;x.fillStyle=g;x.fill();x.globalAlpha=1;}
const ICON={done:'✓',active:'●',partial:'‖',todo:'○',blocked:'🔒'};
function funnelView(p){
 const order={done:3,active:2,partial:2,todo:1,blocked:0};
 return p.stages.map((s,i)=>{
  const filled=s.status==='done';
  const btn=(s.action&&s.status!=='blocked')?`<button class="${s.status==='todo'||s.status==='partial'?'primary':''}" onclick="act('${s.action}','${p.account}')">${s.actionLabel||'Lancer'}</button>`:'';
  return `<div class="step ${s.status} ${filled?'filled':''}">
    <div class="node">${ICON[s.status]||'○'}</div>
    <div class="st">${s.status==='partial'?'en pause':s.status}</div>
    <h3>${s.label}</h3><div class="d">${s.detail||''}</div>${btn}</div>`;
 }).join('');
}
function barRows(map,el){const it=Object.entries(map||{}).sort((a,b)=>b[1][1]-a[1][1]);const top=(it[0]?.[1][1])||1;
 el.innerHTML=it.map(([k,[c,g]])=>`<div class="r"><span class="lbl">${k}</span><span class="bar"><i style="width:${Math.max(2,100*g/top)}%"></i></span><span class="val mono">${c} · ${g} Go</span></div>`).join('')||'<span class="muted">—</span>';}
async function act(action,account){const r=await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,account})});
 logCtx={action,account};logOff=0;$('#logfor').textContent='· '+action;$('#log').textContent=(r.message||'')+'\n';
 if(action==='propose')setTimeout(()=>loadProp(account),1500);
 setTimeout(pollPipeline,600);}
async function loadProp(account){try{const p=await api('/api/proposal?account='+encodeURIComponent(account));$('#proposalCard').style.display='';
 $('#propTiles').innerHTML=[['fichiers',p.total_files.toLocaleString()],['à garder',p.clean_files.toLocaleString()+' ('+p.clean_gb+' Go)'],['doublons','-'+p.dupe_reclaim_gb+' Go',1],['junk',p.junk_files+' ('+p.junk_mb+' Mo)',1]]
  .map(([t,n,w])=>`<div><div class="n ${w?'warn':'ok'}">${n}</div><div class="t">${t}</div></div>`).join('');
 barRows(p.by_category,$('#byCat'));barRows(p.by_source,$('#bySrc'));}catch(e){}}
let prevB=null,prevT=null;
async function tick(){try{const s=await api('/api/state');const a=s.accounts.find(x=>x.running)||s.accounts[0];
 $('#hero').innerHTML=heroView(a);$('#disk').textContent='disk '+s.disk.free_gb+' Go';$('#ro').style.display=s.readonly?'':'none';
 if(a){let r=a.rate_bps;if(!r&&prevB!=null){const now=performance.now();if(now>prevT)r=(a.bytes-prevB)/((now-prevT)/1000);prevB=a.bytes;prevT=now;}else{prevB=a.bytes;prevT=performance.now();}
  if(a.running){hist.push((r||0)/1e6);if(hist.length>80)hist.shift();}drawSpark();}
 $('#clock').textContent=new Date().toLocaleTimeString();}catch(e){}}
async function pollPipeline(){try{const p=await api('/api/pipeline?account='+encodeURIComponent(ACCT));$('#funnel').innerHTML=funnelView(p);}catch(e){}}
async function pollLog(){if(logCtx){try{const r=await api(`/api/log?action=${logCtx.action}&account=${logCtx.account}&offset=${logOff}`);if(r.text){logOff=r.offset;const el=$('#log');el.textContent+=r.text;el.scrollTop=el.scrollHeight;}}catch(e){}}}
tick();pollPipeline();setInterval(tick,2000);setInterval(pollPipeline,3000);setInterval(pollLog,1500);
</script></body></html>
"""
