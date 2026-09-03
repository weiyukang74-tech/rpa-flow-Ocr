@echo off
setlocal
set "STUDIO_DIR=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%STUDIO_DIR%start-studio.ps1"
set "STUDIO_EXIT=%ERRORLEVEL%"
if not "%STUDIO_EXIT%"=="0" (
    echo.
    echo Flow Configurator failed to start. Exit code: %STUDIO_EXIT%
    pause
)
endlocal & exit /b %STUDIO_EXIT%
