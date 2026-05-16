@echo off
setlocal

set "ENV_FILE=%~dp0.spotdl.env"

echo This saves Spotify app credentials for the local launcher.
echo.
echo If you do not already have them, create an app in the Spotify Developer Dashboard.
echo Then paste the Client ID and Client Secret below.
echo.

set /p SPOTDL_CLIENT_ID=Client ID^> 
if not defined SPOTDL_CLIENT_ID goto :cancelled

set /p SPOTDL_CLIENT_SECRET=Client Secret^> 
if not defined SPOTDL_CLIENT_SECRET goto :cancelled

(
    echo # Local Spotify app credentials for this repo
    echo SPOTDL_CLIENT_ID=%SPOTDL_CLIENT_ID%
    echo SPOTDL_CLIENT_SECRET=%SPOTDL_CLIENT_SECRET%
) > "%ENV_FILE%"

echo.
echo Saved credentials to:
echo %ENV_FILE%
echo.
echo Run this after setup:
echo %~dp0run-spotdl.bat
pause
exit /b 0

:cancelled
echo.
echo Cancelled. Nothing was saved.
pause
exit /b 1
