@echo off
REM EMERGENCY FLATTEN when the engine is hung or closed: cancels every order and sells
REM every position with limit orders. Use the dashboard or Telegram /flatten first.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
set PYTHONUTF8=1
set /p CONFIRM=Type FLATTEN and press Enter to close everything: 
python -m app.tools.flatten --confirm "%CONFIRM%"
pause
