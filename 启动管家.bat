@echo off
rem ============================================================
rem  Start the email butler.
rem
rem  Double-click this file. It starts the MCP server by itself,
rem  so you only need to talk to the bot.
rem  Ollama must be running already; the bot will tell you if not.
rem
rem  NOTE: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI code page,
rem  so non-ASCII text here can be mis-parsed as commands.
rem  All Chinese messages are printed by Python instead.
rem ============================================================

cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"

title Email Butler

if not exist "%PY%" goto :no_venv
if not exist ".env" goto :no_env

"%PY%" email_butler.py
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
