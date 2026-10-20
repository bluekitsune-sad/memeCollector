@echo off
setlocal
cd /d "%~dp0"
title MemeCollector launcher

echo.
echo  MemeCollector - starting services
echo.

REM ---- Backend :8000 ------------------------------------------------------
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "MemeCollector backend :8000" cmd /k python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
    echo   Backend  : starting
) else (
    echo   Backend  : already running
)
echo            http://127.0.0.1:8000

REM ---- Frontend :3100 -----------------------------------------------------
if not exist "frontend\.next\BUILD_ID" (
    echo   Frontend : no build found - running npm run build...
    pushd frontend
    call npm.cmd run build
    if errorlevel 1 (
        echo   Frontend : BUILD FAILED - fix it and run start.bat again.
        popd
        exit /b 1
    )
    popd
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 3100 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "MemeCollector frontend :3100" /D "%~dp0frontend" cmd /k npm.cmd run start -- --port 3100
    echo   Frontend : starting
) else (
    echo   Frontend : already running
)
echo            http://127.0.0.1:3100

echo.
echo  Each service runs in its own window - leave them open.
echo  Close a window to stop that service; run start.bat again to bring it back.
echo.
pause
