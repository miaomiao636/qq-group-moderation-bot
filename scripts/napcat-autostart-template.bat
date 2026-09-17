@echo off
REM NapCat autostart at Windows logon (minimized, single-instance guard).
REM
REM Setup:
REM   1) Set NAPCAT_DIR below to your NapCat folder.
REM   2) Set BOT_QQ to your bot account number.
REM   3) Copy this file into the Startup folder:
REM      Win+R -> shell:startup -> paste here.
REM
REM Notes:
REM   - NapCat CANNOT run as a Windows service (it needs an interactive login
REM     session for QR / quick login). Autostart at user logon is the supported way.
REM   - Your NapCat version may use a different launcher than launcher-user.bat
REM     (e.g. NapCatWinBootMain.exe). Adjust the "start" line accordingly.
set "NAPCAT_DIR=D:\QQ"
set "BOT_QQ=<your-bot-qq>"

tasklist /FI "IMAGENAME eq QQ.exe" 2>NUL | find /I "QQ.exe" >NUL
if %ERRORLEVEL%==0 exit /b 0
cd /d %NAPCAT_DIR%
start "" /min cmd /c "%NAPCAT_DIR%\launcher-user.bat %BOT_QQ%"
