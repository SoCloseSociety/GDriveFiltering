"""Native desktop window (pywebview) wrapping the local dashboard.

Opens the dashboard in a real OS window instead of a browser tab. pywebview is
an optional, desktop-only dependency (see requirements-desktop.txt); the CLI
falls back to the browser dashboard if it isn't installed.
"""
from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer

from .config import Config
from .dashboard import dashboard_up, make_handler
from .logging_conf import get_logger

log = get_logger("app")


def run(cfg: Config, port: int = 8787, width: int = 1200, height: int = 820) -> None:
    import webview  # lazy: optional dependency, only needed for the native window

    url = f"http://127.0.0.1:{port}/"
    httpd = None
    if not dashboard_up(port):  # only start a server if OURS isn't already up
        # No dashboard running yet -> start one in-process (bound to loopback).
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        for _ in range(40):
            if dashboard_up(port):
                break
            time.sleep(0.25)
        log.info("Dashboard démarré sur %s", url)
    else:
        log.info("Dashboard déjà actif -> fenêtre pointée sur %s", url)

    webview.create_window("GDriveFiltering", url, width=width, height=height,
                          min_size=(900, 600))
    try:
        webview.start()  # blocks until the window is closed
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
