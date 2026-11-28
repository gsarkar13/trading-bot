@echo off
REM Get the latest code and upgrade the database. Run when told a new version is merged.
cd /d "%~dp0\.."
git pull || exit /b 1
call .venv\Scripts\activate.bat
pip install -e .
python -m app.tools.init_db
