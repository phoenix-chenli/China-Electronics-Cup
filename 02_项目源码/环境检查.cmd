@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows.ps1" check %*
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
