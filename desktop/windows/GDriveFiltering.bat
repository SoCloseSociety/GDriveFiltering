@echo off
REM Launch GDriveFiltering as a native window (pywebview / built-in Edge WebView2).
REM Use GDriveFiltering.vbs for a shortcut that shows no console window.
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
REM Native-window dependency (falls back to the browser dashboard if it fails).
.venv\Scripts\python -c "import webview" 2>nul || .venv\Scripts\python -m pip install -q -r requirements-desktop.txt

.venv\Scripts\python -m gdrivefilter app --port 8787
endlocal
