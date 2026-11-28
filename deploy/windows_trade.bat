@echo off
REM Phase 2 trading day: start around 08:55 IST, leave the window open until ~15:40.
REM Logs in if needed, starts the watchdog (Telegram) and runs the engine, recorder and
REM dashboard (http://127.0.0.1:8600). Paper trading only: real money stays off.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
set PYTHONUTF8=1
python -m app.tools.trade_day
echo.
echo Trading day finished. The journal is on the dashboard (Review tab).
pause
