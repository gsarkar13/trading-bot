@echo off
REM One-time setup on the Windows laptop. Run from the repository folder:
REM     deploy\windows_setup.bat
cd /d "%~dp0\.."
where py >nul 2>nul || (echo Install Python 3.12 from https://www.python.org/downloads/ first, tick "Add to PATH". & exit /b 1)
if not exist .venv (py -3.12 -m venv .venv || (echo Python 3.12 not found. Install it from python.org. & exit /b 1))
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .
if not exist .env copy .env.example .env
if not exist config\host.yaml copy config\host.example.yaml config\host.yaml
python -m app.tools.init_db
echo.
echo Setup done. Now open .env in Notepad and fill in ALGO_FYERS_APP_ID and ALGO_FYERS_APP_SECRET.
