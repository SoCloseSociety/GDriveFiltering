"""In-memory fake Drive backend for tests -- no network, no Google libs."""
from __future__ import annotations

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"


class FakeBackend:
    """Implements the DriveBackend protocol over in-memory dicts.

    files_by_drive: {drive_id ("" for My Drive): [file dicts]}
    content: {file_id: bytes} for binary files
    exports: {file_id: bytes} for native exports
    """

    def __init__(self, files_by_drive: dict, shared_drives: list[dict],
                 content: dict, exports: dict | None = None,
                 shared_with_me: list[dict] | None = None):
        self.files_by_drive = files_by_drive
        self.shared_drives = shared_drives
        self.content = content
        self.exports = exports or {}
        self.shared_with_me = shared_with_me or []
        self.download_calls: list[str] = []
        self.export_calls: list[str] = []

    def drives_list(self, **params) -> dict:
        return {"drives": self.shared_drives}

    def files_list(self, **params) -> dict:
        q = params.get("q", "")
        if "sharedWithMe" in q:
            return {"files": list(self.shared_with_me)}
        if params.get("corpora") == "drive":
            key = params.get("driveId", "")
        else:
            key = ""
        return {"files": list(self.files_by_drive.get(key, []))}

    def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return self.content[file_id]

    def export(self, file_id: str, mime: str) -> bytes:
        self.export_calls.append(file_id)
        return self.exports[file_id]

    def reset(self) -> None:
        pass

    def download_to(self, file_id: str, fileobj) -> None:
        self.download_calls.append(file_id)
        fileobj.write(self.content[file_id])

    def export_to(self, file_id: str, mime: str, fileobj) -> None:
        self.export_calls.append(file_id)
        fileobj.write(self.exports[file_id])


def sample_tree() -> "FakeBackend":
    """A small tree: My Drive with a folder + duplicate + a native doc; one shared drive."""
    my_drive = [
        {"id": "fold1", "name": "Photos", "mimeType": FOLDER, "parents": [],
         "modifiedTime": "2023-01-01T00:00:00Z"},
        {"id": "img1", "name": "a.jpg", "mimeType": "image/jpeg", "parents": ["fold1"],
         "size": "5", "modifiedTime": "2023-05-01T00:00:00Z",
         "owners": [{"emailAddress": "me@example.com"}]},
        {"id": "img2", "name": "b.jpg", "mimeType": "image/jpeg", "parents": [],
         "size": "5", "modifiedTime": "2024-05-01T00:00:00Z",
         "owners": [{"emailAddress": "me@example.com"}]},
        {"id": "doc1", "name": "notes", "mimeType": DOC, "parents": [],
         "modifiedTime": "2022-05-01T00:00:00Z",
         "owners": [{"emailAddress": "me@example.com"}]},
        # Two DIFFERENT files with the SAME name in the SAME folder (Drive allows it).
        {"id": "d1", "name": "dup.txt", "mimeType": "text/plain", "parents": [],
         "size": "4", "modifiedTime": "2023-02-01T00:00:00Z",
         "owners": [{"emailAddress": "me@example.com"}]},
        {"id": "d2", "name": "dup.txt", "mimeType": "text/plain", "parents": [],
         "size": "4", "modifiedTime": "2023-02-01T00:00:00Z",
         "owners": [{"emailAddress": "me@example.com"}]},
    ]
    shared = [
        {"id": "shdoc", "name": "report.pdf", "mimeType": "application/pdf", "parents": [],
         "size": "3", "modifiedTime": "2021-05-01T00:00:00Z",
         "owners": [{"emailAddress": "team@example.com"}]},
    ]
    shared_with_me = [
        {"id": "swm1", "name": "shared_note.txt", "mimeType": "text/plain", "parents": [],
         "size": "6", "modifiedTime": "2020-05-01T00:00:00Z",
         "owners": [{"emailAddress": "friend@example.com"}]},
    ]
    content = {
        "img1": b"HELLO",   # identical bytes to img2 -> exact duplicate
        "img2": b"HELLO",
        "shdoc": b"PDF",
        "d1": b"AAAA",      # same name as d2, different bytes -> must NOT overwrite
        "d2": b"BBBB",
        "swm1": b"SHARED",
    }
    exports = {"doc1": b"DOCX-BYTES"}
    return FakeBackend(
        files_by_drive={"": my_drive, "team1": shared},
        shared_drives=[{"id": "team1", "name": "TeamDrive"}],
        content=content, exports=exports, shared_with_me=shared_with_me,
    )
