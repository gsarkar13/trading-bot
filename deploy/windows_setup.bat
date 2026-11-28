@echo off
REM One-time setup on a Windows PC or laptop. Run from the repository folder:
REM     deploy\windows_setup.bat        (or double-click it in File Explorer)
REM
REM Needs Python 3.12 exactly. The official Fyers SDK pins aiohttp 3.9.3, which has
REM ready-made Windows packages only for Python 3.12 (newer versions would need the
REM Microsoft C++ compiler). Python versions install side by side, so having 3.13 or
REM 3.14 as well is fine.
cd /d "%~dp0\.."
where py >nul 2>nul || goto nopython
py -3.12 -c "import sys" >nul 2>nul || goto nopython

REM If an old .venv was made with a different Python version, rebuild it.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" || (
    echo Existing .venv uses a different Python version; rebuilding it with 3.12...
    rmdir /s /q .venv
  )
)
if not exist .venv (py -3.12 -m venv .venv || exit /b 1)
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
exit /b 0

:nopython
echo.
echo Python 3.12 is needed (the Fyers SDK only installs on 3.12 on Windows).
echo Install it alongside any other version you have:
echo   winget install Python.Python.3.12
echo or download "Windows installer (64-bit)" for 3.12 from
echo   https://www.python.org/downloads/windows/
echo Then run deploy\windows_setup.bat again.
exit /b 1
