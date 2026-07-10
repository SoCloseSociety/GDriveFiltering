@echo off
REM Launch the GDriveFiltering dashboard and open it in the browser.
REM Put this file (with the whole project) anywhere and make a Desktop shortcut,
REM or use GDriveFiltering.vbs to launch it without a console window.
setlocal
cd /d "%~dp0\.."

REM Already running? just open it.
powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8787/)|Out-Null;exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 ( start "" http://127.0.0.1:8787/ & exit /b 0 )

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
start "" /min .venv\Scripts\python -m gdrivefilter dashboard --port 8787 --no-open

REM wait for the server then open the browser
for /l %%i in (1,1,30) do (
  powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8787/)|Out-Null;exit 0}catch{exit 1}" >nul 2>&1
  if not errorlevel 1 goto :open
  timeout /t 1 >nul
)
:open
start "" http://127.0.0.1:8787/
endlocal
