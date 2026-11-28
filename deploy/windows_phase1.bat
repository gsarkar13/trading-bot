@echo off
REM Phase 1 data run: login, probe, ~1 year of history, backtest, one zip to attach.
REM Run after 15:45 IST on a weekday, or at a weekend. Takes about 15-40 minutes.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
set PYTHONUTF8=1
python -m app.tools.phase1
echo.
echo Attach the zip file named above in the chat.
pause
