@echo off
REM Optional: open windows_trade.bat automatically at 08:50 on weekdays.
REM It still asks for your vault passphrase (never stored), so you must be at the PC.
REM Remove later with:  schtasks /Delete /TN AlgoTradeDay /F
cd /d "%~dp0\.."
schtasks /Create /TN AlgoTradeDay /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:50 /TR "\"%CD%\deploy\windows_trade.bat\"" /F
pause
