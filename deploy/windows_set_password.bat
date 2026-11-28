@echo off
REM Set or change the dashboard password (only a hash is stored, in .env).
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m app.tools.set_password
pause
