@echo off
rem Starts Command Center. Extra commands: run.cmd login <profile> <url>  |  run.cmd setpass <connection>
cd /d "%~dp0"
".venv\Scripts\python.exe" -m commandcenter %*
