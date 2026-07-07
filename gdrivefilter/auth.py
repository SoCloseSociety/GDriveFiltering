"""OAuth for Google Drive. Robust dual-stack loopback consent, per-account token.

Why not InstalledAppFlow.run_local_server: on macOS `localhost` can resolve to
IPv6 (::1) while a plain server binds IPv4 (127.0.0.1) -> the browser redirect
never reaches us ("localhost ne fonctionne pas"). We also hit MismatchingStateError
when a stray/duplicate request reached the one-shot server. This module:
  - binds a DUAL-STACK server (IPv4 + IPv6) so localhost works either way,
  - ignores stray requests (favicon, etc.) and waits for the real ?code=,
  - exchanges the code directly (no strict state re-check),
  - times out instead of hanging forever.

Google libraries are imported lazily so the rest of the package (and most of the
test suite) works without them installed.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .logging_conf import get_logger

log = get_logger("auth")

_SUCCESS_HTML = (
    "<html><body style='font-family:sans-serif'>"
    "<h2>Authentification reussie.</h2>"
    "<p>Tu peux fermer cet onglet et revenir au terminal.</p></body></html>"
)


class _DualStackServer(HTTPServer):
    """HTTP server accepting both IPv4 and IPv6 loopback (127.0.0.1 and ::1)."""
    address_family = socket.AF_INET6
    allow_reuse_address = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (OSError, AttributeError):
            pass
        super().server_bind()


def _make_server(port: int, result: dict) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            params = parse_qs(urlparse(self.path).query)
            if "code" in params or "error" in params:
                result["code"] = params.get("code", [None])[0]
                result["state"] = params.get("state", [None])[0]
                result["error"] = params.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML.encode("utf-8"))
            else:
                # stray request (favicon, probe): keep waiting for the real one
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # silence default stderr logging
            pass

    try:
        return _DualStackServer(("::", port), Handler)
    except OSError:
        # No IPv6 available -> fall back to IPv4-only.
        return HTTPServer(("127.0.0.1", port), Handler)


def capture_loopback_code(port: int, on_ready=None, timeout: float = 300.0) -> dict:
    """Serve on the loopback until a request carrying ?code= (or ?error=) arrives.

    Returns {"code", "state", "error"}. Testable without Google: hit
    http://127.0.0.1:<port>/?code=... or http://localhost:<port>/?code=...
    """
    result: dict = {}
    httpd = _make_server(port, result)
    httpd.timeout = 1.0
    if on_ready:
        on_ready()
    deadline = time.monotonic() + timeout
    try:
        while "code" not in result and "error" not in result:
            if time.monotonic() > deadline:
                result["error"] = "timeout"
                break
            httpd.handle_request()  # blocks up to httpd.timeout, ignores stray reqs
    finally:
        httpd.server_close()
    return result


def _client_config(cfg: Config) -> dict:
    return {
        "installed": {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{cfg.oauth_port}/"],
        }
    }


def _token_path(cfg: Config, account: str) -> Path:
    safe = account.replace("/", "_") or "default"
    return cfg.token_dir / f"token_{safe}.json"


def get_credentials(cfg: Config, account: str = "default"):
    """Return valid google credentials for `account`, running consent if needed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import Flow
    except ImportError as e:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Dépendances Google manquantes. Lance: pip install -r requirements.txt"
        ) from e

    if not cfg.client_id or not cfg.client_secret:
        raise RuntimeError(
            "Aucun GOOGLE_CLIENT_ID/SECRET trouvé (ni local, ni dans les projets "
            "voisins). Renseigne-les dans .env."
        )

    tok = _token_path(cfg, account)
    creds = None
    if tok.is_file():
        creds = Credentials.from_authorized_user_info(
            json.loads(tok.read_text(encoding="utf-8")), cfg.scope
        )
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(tok, creds)
            return creds
        except Exception as e:  # noqa: BLE001
            log.warning("Refresh token échoué (%s), reconsent nécessaire.", e)

    redirect_uri = f"http://localhost:{cfg.oauth_port}/"
    flow = Flow.from_client_config(_client_config(cfg), scopes=cfg.scope)
    flow.redirect_uri = redirect_uri
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )

    log.info("Consent OAuth pour le compte '%s'.", account)
    log.info("Si le navigateur ne s'ouvre pas, ouvre ce lien:\n%s", auth_url)
    log.info(">>> redirect_uri utilisé: %s (loopback dual-stack IPv4+IPv6).", redirect_uri)

    def _open():
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 - browser is best-effort
            pass

    result = capture_loopback_code(cfg.oauth_port, on_ready=_open)
    if result.get("error") == "timeout":
        raise RuntimeError(
            "Délai d'authentification dépassé. Relance et complète le consentement "
            "dans le navigateur (ouvre le lien affiché ci-dessus si besoin)."
        )
    if result.get("error"):
        raise RuntimeError(f"Google a renvoyé une erreur OAuth: {result['error']}")
    if not result.get("code"):
        raise RuntimeError("Aucun code d'autorisation reçu sur le loopback.")

    flow.fetch_token(code=result["code"])  # no strict state re-check -> no CSRF mismatch
    creds = flow.credentials
    _save_token(tok, creds)
    return creds


def _save_token(path: Path, creds) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
