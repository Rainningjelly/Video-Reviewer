@echo off
setlocal
set "APP_LAUNCHER=%~dp0qc_app\Start QC App.bat"

if not exist "%APP_LAUNCHER%" (
    echo Could not find the Video QC app launcher:
    echo "%APP_LAUNCHER%"
    pause
    exit /b 1
)

call "%APP_LAUNCHER%"
exit /b %errorlevel%