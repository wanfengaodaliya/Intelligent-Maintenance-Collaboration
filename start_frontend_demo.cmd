@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_frontend_demo.ps1" %*
set "DEMO_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%DEMO_EXIT_CODE%"=="0" echo Demo launcher exited with code %DEMO_EXIT_CODE%.
pause
exit /b %DEMO_EXIT_CODE%
