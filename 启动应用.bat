@echo off
rem ============================================================
rem  Start the email butler WEB APP (graphical window).
rem
rem  Double-click this file. It does three things:
rem    1. starts the local web server on 127.0.0.1:8765
rem    2. opens a standalone app window (Edge/Chrome --app mode,
rem       no address bar, no tabs)
rem    3. keeps running until you close that window
rem
rem  The window is local-only and can send real email, so never
rem  expose port 8765 to your LAN or the internet.
rem
rem  NOTE: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI code page,
rem  so non-ASCII text here can be mis-parsed as commands.
rem  All Chinese messages are printed by Python instead.
rem ============================================================

cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"

title Email Butler App

if not exist "%PY%" goto :no_venv
if not exist ".env" goto :no_env

"%PY%" webui.py
echo.
pause
exit /b 0

:no_venv
echo [X] Virtual environment not found: %PY%
echo     Run these first:
echo         python -m venv venv
echo         venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:no_env
echo [X] .env not found
echo     Run: copy .env.example .env   then fill in your mailbox and auth code
echo.
pause
exit /b 1
