@echo off
rem ============================================================
rem  EvidenceLog V1 - stop the stack (Windows)
rem  Data volumes are kept: uploads/DB survive, so nothing is lost.
rem ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   Stopping EvidenceLog containers (data volumes are kept)...
docker compose down --remove-orphans
echo.
echo   Done. Start again with 启动.cmd
echo.
pause
