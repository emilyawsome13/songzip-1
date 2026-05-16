@echo off
setlocal

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

set "CURRENT_DIR=%~dp0"

if exist "%CURRENT_DIR%run-spotdl.ps1" (
    powershell -ExecutionPolicy Bypass -File "%CURRENT_DIR%run-spotdl.ps1" %*
    exit /b %errorlevel%
)

set "PROJECT_DIR=%CURRENT_DIR%spotify-downloader-master"

if not exist "%PROJECT_DIR%\run-spotdl.bat" (
    echo Could not find the project launcher in:
    echo %PROJECT_DIR%
    pause
    exit /b 1
)

call "%PROJECT_DIR%\run-spotdl.bat" %*
exit /b %errorlevel%
