@echo off
REM Each trading day, start this around 08:55 IST and leave the window open until ~15:40.
REM It logs in to Fyers if needed, then records market data and writes the daily summary.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
set PYTHONUTF8=1
python -m app.tools.daily
echo.
echo Finished. Attach the newest file in reports\recorder\ in the chat.
pause
