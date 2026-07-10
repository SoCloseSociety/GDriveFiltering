"""Large-file download survives a dropped connection (the real 'Remote end
closed connection' errors seen on big MP4s), via full-file retry + reconnect."""
import pytest

from gdrivefilter.drive_client import DriveClient
from gdrivefilter.extract import run_backup
from gdrivefilter.manifest import Manifest
from tests.fakes import FakeBackend


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Make retry backoff instant so these tests stay fast (and don't slow the suite).
    monkeypatch.setattr("gdrivefilter.extract.time.sleep", lambda *_: None)


class FlakyBackend(FakeBackend):
    def __init__(self, *a, fail_until=2, fail_id="v1", **k):
        super().__init__(*a, **k)
        self.fail_until, self.fail_id = fail_until, fail_id
        self.attempts = 0
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def download_to(self, file_id, fileobj):
        if file_id == self.fail_id:
            self.attempts += 1
            if self.attempts <= self.fail_until:
                fileobj.write(b"PARTIAL-GARBAGE")  # partial bytes before the drop
                raise ConnectionError("Remote end closed connection without response")
        return super().download_to(file_id, fileobj)


def test_large_file_recovers_from_connection_drop(cfg):
    files = [{"id": "v1", "name": "C0047.MP4", "mimeType": "video/mp4", "parents": [],
              "size": "9", "modifiedTime": "2023-01-01T00:00:00Z",
              "owners": [{"emailAddress": "me@x.com"}]}]
    be = FlakyBackend(files_by_drive={"": files}, shared_drives=[],
                      content={"v1": b"VIDEODATA"}, exports={}, fail_until=2)
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="RS", workers=2)

    assert res.downloaded == 1 and res.errors == 0     # recovered, not errored
    assert be.reset_calls >= 2                          # connection rebuilt each drop
    primary = cfg.backup_root / "RS/default"
    # 'wb' truncation each attempt -> final file is clean, no partial garbage.
    assert (primary / "My Drive/C0047.MP4").read_bytes() == b"VIDEODATA"
    assert Manifest.load(primary / "manifest.json").count_done() == 1


def test_incomplete_read_is_retried(cfg):
    """http.client.IncompleteRead (server drops mid-stream) must be transient:
    retried with a fresh connection, not recorded as a permanent error."""
    import http.client

    class IncompleteReadBackend(FlakyBackend):
        def download_to(self, file_id, fileobj):
            if file_id == self.fail_id:
                self.attempts += 1
                if self.attempts <= self.fail_until:
                    fileobj.write(b"PART")
                    raise http.client.IncompleteRead(b"PART", expected=5)
            return FakeBackend.download_to(self, file_id, fileobj)

    files = [{"id": "v1", "name": "pod.mp4", "mimeType": "video/mp4", "parents": [],
              "size": "9", "modifiedTime": "2023-01-01T00:00:00Z",
              "owners": [{"emailAddress": "me@x.c"}]}]
    be = IncompleteReadBackend(files_by_drive={"": files}, shared_drives=[],
                               content={"v1": b"VIDEODATA"}, exports={}, fail_until=2)
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="IR", workers=2)
    assert res.downloaded == 1 and res.errors == 0
    assert (cfg.backup_root / "IR/default/My Drive/pod.mp4").read_bytes() == b"VIDEODATA"


def test_permanent_403_becomes_restricted_not_blocking(cfg):
    """View-only / flagged files (permanent 403) must not block completeness:
    recorded as 'restricted', reported, retried on future resumes."""
    class Fake403(Exception):
        status_code = 403
        reason = "cannotDownloadAbusiveFile"

    class RestrictedBackend(FlakyBackend):
        def download_to(self, file_id, fileobj):
            if file_id == self.fail_id:
                raise Fake403("forbidden")
            return FakeBackend.download_to(self, file_id, fileobj)

    files = [
        {"id": "ok1", "name": "fine.bin", "mimeType": "application/octet-stream",
         "parents": [], "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
        {"id": "v1", "name": "viewonly.pdf", "mimeType": "application/pdf",
         "parents": [], "size": "9", "modifiedTime": "2023-01-01T00:00:00Z",
         "owners": [{"emailAddress": "m@x.c"}]},
    ]
    be = RestrictedBackend(files_by_drive={"": files}, shared_drives=[],
                           content={"ok1": b"AAA", "v1": b"NEVER"}, exports={})
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="R403", workers=2)
    assert res.downloaded == 1 and res.restricted == 1 and res.errors == 0

    m = Manifest.load(cfg.backup_root / "R403/default/manifest.json")
    complete, reason = m.is_complete()
    assert complete and "restricted" in reason          # accounted, not blocking
    from gdrivefilter.verify import verify_backup
    rep = verify_backup(cfg.backup_root / "R403/default")
    assert rep.clean and rep.restricted == 1            # surfaced in the report


def test_circuit_breaker_aborts_on_systemic_outage(cfg, monkeypatch):
    """Network/DNS outage (everything failing) must abort early, not burn the
    whole queue into tens of thousands of error entries."""
    monkeypatch.setattr("gdrivefilter.extract.BREAKER_LIMIT", 10, raising=False)

    class DeadNetworkBackend(FakeBackend):
        def download_to(self, file_id, fileobj):
            raise ValueError("Unable to find the server at www.googleapis.com")

    n = 200
    files = [{"id": f"f{i}", "name": f"f{i}.bin", "mimeType": "application/octet-stream",
              "parents": [], "size": "3", "modifiedTime": "2023-01-01T00:00:00Z",
              "owners": [{"emailAddress": "m@x.c"}]} for i in range(n)]
    be = DeadNetworkBackend(files_by_drive={"": files}, shared_drives=[],
                            content={}, exports={})
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="CB", workers=4)
    assert res.aborted            # breaker fired with a reason
    assert res.errors < n         # queue NOT fully burned into errors
    # And the run stays resumable: manifest saved, no corruption.
    m = Manifest.load(cfg.backup_root / "CB/default/manifest.json")
    assert not m.is_complete()[0]


def test_permanent_failure_is_reported_not_infinite(cfg):
    files = [{"id": "v1", "name": "bad.MP4", "mimeType": "video/mp4", "parents": [],
              "size": "9", "modifiedTime": "2023-01-01T00:00:00Z",
              "owners": [{"emailAddress": "me@x.com"}]}]
    be = FlakyBackend(files_by_drive={"": files}, shared_drives=[],
                      content={"v1": b"VIDEODATA"}, exports={}, fail_until=999)
    res = run_backup(cfg, DriveClient(be), account="default", timestamp="RSX", workers=2)
    assert res.downloaded == 0 and res.errors == 1      # gives up cleanly after N tries
