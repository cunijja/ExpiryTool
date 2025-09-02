@echo off
setlocal

REM Jump to this script's folder
cd /d "%~dp0"

REM Create export folders if missing (no error if they exist)
mkdir exports 2>nul
mkdir "exports\picklists" 2>nul
mkdir "exports\low_stock" 2>nul
mkdir "exports\expiries" 2>nul

REM Pick a Python launcher
where python >nul 2>&1
if %errorlevel%==0 (
  set "PY=python"
) else (
  set "PY=py -3"
)

REM UTF-8 console
chcp 65001 >nul

%PY% expirytool.py
pause
