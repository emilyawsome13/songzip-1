@echo off
setlocal

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

powershell -ExecutionPolicy Bypass -File "%~dp0run-artist-queue.ps1" %*
exit /b %errorlevel%
