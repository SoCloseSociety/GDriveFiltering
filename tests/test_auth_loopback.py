"""Prove the loopback consent server works over IPv4 AND IPv6 (localhost fix),
ignores stray requests, and times out cleanly -- all without contacting Google."""
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from gdrivefilter.auth import capture_loopback_code


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_capture(port, timeout=10.0):
    holder = {}
    ready = threading.Event()
    holder["thread"] = threading.Thread(
        target=lambda: holder.update(result=capture_loopback_code(
            port, on_ready=ready.set, timeout=timeout)),
        daemon=True,
    )
    holder["thread"].start()
    assert ready.wait(5.0), "le serveur loopback ne s'est pas lancé"
    time.sleep(0.05)
    return holder


def _get(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_captures_code_on_ipv4_and_ipv6(host):
    port = _free_port()
    h = _run_capture(port)
    status = _get(f"http://{host}:{port}/?code=abc123&state=xyz")
    assert status == 200
    h["thread"].join(5.0)
    assert h["result"]["code"] == "abc123"
    assert h["result"]["state"] == "xyz"


def test_stray_request_is_ignored_then_real_code_captured():
    port = _free_port()
    h = _run_capture(port)
    # A stray request (no code, e.g. favicon) must NOT end the flow.
    assert _get(f"http://127.0.0.1:{port}/favicon.ico") == 404
    assert not h["thread"].join(0.2) and h["thread"].is_alive()
    # The real redirect then completes it.
    assert _get(f"http://127.0.0.1:{port}/?code=real42") == 200
    h["thread"].join(5.0)
    assert h["result"]["code"] == "real42"


def test_oauth_error_is_surfaced():
    port = _free_port()
    h = _run_capture(port)
    _get(f"http://127.0.0.1:{port}/?error=access_denied")
    h["thread"].join(5.0)
    assert h["result"]["error"] == "access_denied"


def test_timeout_returns_error_without_hanging():
    port = _free_port()
    result = capture_loopback_code(port, timeout=1.0)
    assert result["error"] == "timeout"
