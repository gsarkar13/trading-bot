@echo off
REM One-time setup on a Windows PC or laptop. Run from the repository folder:
REM     deploy\windows_setup.bat        (or double-click it in File Explorer)
cd /d "%~dp0\.."
where py >nul 2>nul || (echo Python launcher not found. Install Python 3.12 or newer from https://www.python.org/downloads/ & exit /b 1)
REM Any Python 3.12 or newer works (3.12, 3.13, ...).
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || (echo Python 3.12 or newer is needed. Install it from python.org. & exit /b 1)
if not exist .venv (py -3 -m venv .venv || exit /b 1)
call .venv\Scripts\activate.bat
python --version
python -m pip install --upgrade pip
pip install -e . || exit /b 1
if not exist .env copy .env.example .env
if not exist config\host.yaml copy config\host.example.yaml config\host.yaml
python -m app.tools.init_db || exit /b 1
echo.
echo Setup done.
echo  1. Open .env in Notepad and fill in ALGO_FYERS_APP_ID and ALGO_FYERS_APP_SECRET.
echo  2. Open config\host.yaml and give this machine its own instance_id (e.g. PC1 or LAP1).
