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
            if not ts_dir.is_dir():
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
        "bytes": snap.bytes_written if snap else 0,
        "pct": round(snap.pct, 2) if snap else 0,
        "running": _is_running(backup_dir),
    }


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
<title>GDriveFiltering -- Dashboard</title>
<style>
:root{--bg:#f7f8fa;--fg:#0f1720;--mut:#5c6672;--card:#fff;--line:#e5e8ec;--acc:#3b82f6;--ok:#16a34a;--warn:#d97706;--crit:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f14;--fg:#e6edf3;--mut:#8b98a5;--card:#141a21;--line:#242c35;--acc:#5aa2ff;--ok:#3fb950;--warn:#d29922;--crit:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:700}
.badge{font-size:11px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.badge.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
.grow{flex:1}
main{padding:20px 22px;display:flex;flex-direction:column;gap:18px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h2{margin:0 0 12px;font-size:14px;font-weight:600}
.acc-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.dot.run{background:var(--ok);box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 70%,transparent);animation:p 1.6s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
.prog{background:var(--line);border-radius:9px;height:18px;overflow:hidden;margin:12px 0 6px}
.prog i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),color-mix(in srgb,var(--acc) 60%,var(--ok)));transition:width .4s}
.stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:8px}
.stat div{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px}
.stat .n{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .t{font-size:11px;color:var(--mut)}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
button{font:inherit;font-size:13px;padding:8px 13px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;transition:.15s}
button:hover{border-color:var(--acc);color:var(--acc)}
button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
button.primary:hover{filter:brightness(1.08);color:#fff}
.rows .r{display:grid;grid-template-columns:150px 1fr 120px;gap:10px;align-items:center;margin:5px 0}
.rows .lbl{color:var(--mut);font-size:13px}.rows .val{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums;font-size:13px}
.bar{background:var(--line);border-radius:5px;height:11px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
pre{background:#0b0f14;color:#cfe;border-radius:10px;padding:12px;max-height:240px;overflow:auto;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.muted{color:var(--mut);font-size:12px}
canvas{width:100%;height:48px;display:block}
@media(max-width:560px){.rows .r{grid-template-columns:90px 1fr}.rows .val{grid-column:2;text-align:left}}
</style></head><body>
<header>
  <h1>🗂️ GDriveFiltering</h1>
  <span class="badge ok" id="ro">lecture seule</span>
  <span class="badge" id="disk">disque --</span>
  <span class="grow"></span>
  <span class="muted" id="clock"></span>
</header>
<main>
  <div id="accounts"></div>
  <div class="card">
    <h2>Débit (Mo/s)</h2><canvas id="spark" height="48"></canvas>
    <div class="muted" id="rate">--</div>
  </div>
  <div class="card" id="proposalCard" style="display:none">
    <h2>Clean tree proposé</h2>
    <div class="stat" id="propStat"></div>
    <h2 style="margin-top:16px">Par catégorie</h2><div class="rows" id="byCat"></div>
    <h2 style="margin-top:16px">Par source</h2><div class="rows" id="bySrc"></div>
  </div>
  <div class="card">
    <h2>Console <span class="muted" id="logfor"></span></h2>
    <pre id="log">Sélectionne une action pour voir ses logs.</pre>
  </div>
  <p class="muted">Actions non destructives uniquement. La suppression (<code>purge</code>) n'est pas exposée ici -- elle exige un miroir vérifié + confirmation en ligne de commande.</p>
</main>
<script>
const $=s=>document.querySelector(s), api=(p,o)=>fetch(p,o).then(r=>r.json());
let prev=null, prevT=null, hist=[], logCtx=null, logOff=0;
function bar(map,el){const it=Object.entries(map||{}).sort((a,b)=>b[1][1]-a[1][1]);const top=(it[0]?.[1][1])||1;
 el.innerHTML=it.map(([k,[c,g]])=>`<div class="r"><span class="lbl">${k}</span><span class="bar"><i style="width:${Math.max(2,100*g/top)}%"></i></span><span class="val">${c} · ${g} Go</span></div>`).join('')||'<span class="muted">--</span>';}
function accCard(a){
 const eta=(a.running&&window.__rate>0)?fmtEta((a.expected-a.done)*(a.bytes/Math.max(1,a.done))/window.__rate):'--';
 return `<div class="card"><div class="acc-head"><span class="dot ${a.running?'run':''}"></span>
  <h2 style="margin:0">${a.account}</h2><span class="muted">${a.running?'en cours':'à l\'arrêt'}</span></div>
  <div class="prog"><i style="width:${a.pct}%"></i></div>
  <div class="stat">
   <div><div class="n">${a.pct}%</div><div class="t">${a.done} / ${a.expected} fichiers</div></div>
   <div><div class="n">${(a.bytes/1073741824).toFixed(2)} Go</div><div class="t">téléchargé</div></div>
   <div><div class="n" style="color:${a.errors?'var(--crit)':'inherit'}">${a.errors}</div><div class="t">erreurs</div></div>
   <div><div class="n">${eta}</div><div class="t">ETA</div></div>
  </div>
  <div class="actions">
   <button class="primary" onclick="act('backup','${a.account}')">▶ Résumer backup</button>
   <button onclick="act('verify','${a.account}')">✓ Vérifier</button>
   <button onclick="act('propose','${a.account}');loadProp('${a.account}')">📋 Proposer clean tree</button>
   <button onclick="act('dedup','${a.account}')">⧉ Dédup (rapport)</button>
   <button onclick="act('reorganize_dry','${a.account}')">🗂 Réorg (dry-run)</button>
   <button onclick="act('open_folder','${a.account}')">📂 Ouvrir dossier</button>
  </div></div>`;}
function fmtEta(s){if(!isFinite(s)||s<=0)return'?';s=Math.round(s);const h=(s/3600)|0,m=((s%3600)/60)|0;return h?`${h}h${String(m).padStart(2,'0')}`:`${m}m`;}
async function act(action,account){const r=await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,account})});
 logCtx={action,account};logOff=0;$('#logfor').textContent='· '+action+' ('+account+')';$('#log').textContent=r.message||'';}
async function loadProp(account){try{const p=await api('/api/proposal?account='+encodeURIComponent(account));
 $('#proposalCard').style.display='';$('#propStat').innerHTML=
  [['fichiers',p.total_files],['à garder',p.clean_files+' ('+p.clean_gb+' Go)'],['doublons','-'+p.dupe_reclaim_gb+' Go'],['junk',p.junk_files+' ('+p.junk_mb+' Mo)']]
  .map(([t,n])=>`<div><div class="n">${n}</div><div class="t">${t}</div></div>`).join('');
 bar(p.by_category,$('#byCat'));bar(p.by_source,$('#bySrc'));}catch(e){}}
function drawSpark(){const c=$('#spark'),x=c.getContext('2d'),W=c.width=c.clientWidth*2,H=c.height=96;x.clearRect(0,0,W,H);
 if(hist.length<2)return;const mx=Math.max(...hist,1);x.beginPath();hist.forEach((v,i)=>{const px=i/(hist.length-1)*W,py=H-(v/mx)*H*0.9-4;i?x.lineTo(px,py):x.moveTo(px,py);});
 x.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--acc');x.lineWidth=3;x.stroke();
 x.lineTo(W,H);x.lineTo(0,H);x.closePath();x.globalAlpha=.12;x.fillStyle=x.strokeStyle;x.fill();x.globalAlpha=1;}
async function tick(){try{const s=await api('/api/state');
 $('#accounts').innerHTML=s.accounts.map(accCard).join('')||'<div class="card muted">Aucun backup pour l\'instant.</div>';
 $('#disk').textContent='disque '+s.disk.free_gb+' Go libres';$('#ro').style.display=s.readonly?'':'none';
 const a=s.accounts.find(x=>x.running)||s.accounts[0];const now=performance.now();
 if(a&&prev!=null&&now>prevT){const r=(a.bytes-prev)/((now-prevT)/1000);window.__rate=r;hist.push(r/1e6);if(hist.length>60)hist.shift();
  $('#rate').textContent=(r/1e6).toFixed(2)+' Mo/s';drawSpark();}
 if(a){prev=a.bytes;prevT=now;}
 $('#clock').textContent=new Date().toLocaleTimeString();}catch(e){}}
async function pollLog(){if(logCtx){try{const r=await api(`/api/log?action=${logCtx.action}&account=${logCtx.account}&offset=${logOff}`);
 if(r.text){logOff=r.offset;const el=$('#log');el.textContent+=r.text;el.scrollTop=el.scrollHeight;}}catch(e){}}}
tick();setInterval(tick,2000);setInterval(pollLog,1500);
</script></body></html>"""
