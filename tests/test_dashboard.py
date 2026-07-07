import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from gdrivefilter.dashboard import _discover_accounts, _launch, make_handler
from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from tests.fakes import sample_tree


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(cfg):
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_state_and_proposal_endpoints(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="D1")
    assert "default" in _discover_accounts(cfg)

    httpd, base = _serve(cfg)
    try:
        state = _get(base + "/api/state")
        accts = {a["account"]: a for a in state["accounts"]}
        assert "default" in accts
        assert accts["default"]["expected"] == 7
        assert state["readonly"] is True

        prop = _get(base + "/api/proposal?account=default")
        assert prop["total_files"] == 7
        assert "by_category" in prop
    finally:
        httpd.shutdown()


def test_disallowed_action_is_refused(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="D2")
    httpd, base = _serve(cfg)
    try:
        req = urllib.request.Request(
            base + "/api/action", method="POST",
            data=json.dumps({"action": "purge", "account": "default"}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "purge should be refused"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            body = json.loads(e.read())
            assert body["ok"] is False
    finally:
        httpd.shutdown()


def test_backup_action_refused_while_running(cfg):
    # A just-written manifest means a backup is (heuristically) active -> no
    # second backup may start (would corrupt the shared manifest/dir).
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="D3")
    r = _launch(cfg, "backup", "default")
    assert r["ok"] is False and "cours" in r["message"].lower()


def test_invalid_json_post_is_400(cfg):
    run_backup(cfg, DriveClient(sample_tree()), account="default", timestamp="D4")
    httpd, base = _serve(cfg)
    try:
        req = urllib.request.Request(
            base + "/api/action", method="POST", data=b"} not json {",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "malformed JSON should be rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()
