@echo off
rem Command Center.  No arguments: open the desktop app (no console window).
rem Other commands: run.cmd install | uninstall | serve | login <profile> <url> | setpass <connection>
cd /d "%~dp0"
if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" -m commandcenter app
) else (
  ".venv\Scripts\python.exe" -m commandcenter %*
)
