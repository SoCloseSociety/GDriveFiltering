# Desktop app

Double-clickable launchers that start the local dashboard (http://127.0.0.1:8787)
and open it in your browser — no terminal needed. They run the project in place,
using its `.venv`, `.env` and OAuth tokens.

## macOS

```bash
./desktop/build_mac_app.sh            # builds ~/Desktop/GDriveFiltering.app
./desktop/build_mac_app.sh /Applications   # or install to Applications
```

Double-click **GDriveFiltering.app**. First launch creates the virtualenv if
missing. Because the app is built locally (not downloaded), Gatekeeper does not
block it. Logs: `~/Library/Logs/GDriveFiltering.log`.

The dashboard runs as a small background server on `127.0.0.1` only. Clicking the
app again just re-opens the page. To stop the server: quit the `python -m
gdrivefilter dashboard` process (Activity Monitor) — harmless to leave running.

## Windows

Copy the project folder to the PC, then from `desktop/windows/`:

- **GDriveFiltering.bat** — launches the dashboard (a console window stays open).
- **GDriveFiltering.vbs** — launches it with **no** console window (recommended).

Right-click `GDriveFiltering.vbs` → *Send to → Desktop (create shortcut)* to get a
desktop icon. First launch creates the virtualenv and installs dependencies
(needs Python 3.11+ from python.org, "Add to PATH" checked).

## Notes

- These launchers wrap the existing `python -m gdrivefilter dashboard` command.
- For a fully standalone app (no Python install required), bundle with PyInstaller
  per-OS — a future enhancement; the current launchers cover "run it from my
  desktop anytime".
