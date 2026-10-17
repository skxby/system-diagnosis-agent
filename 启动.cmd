@echo off
rem ============================================================
rem  EvidenceLog V1 - one-click launcher (Windows)
rem  Double-click this file: it starts the stack and opens the page.
rem  Requires Docker Desktop. Messages are kept ASCII on purpose
rem  (cmd's default code page would garble Chinese text).
rem ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   EvidenceLog V1  -  log analysis runtime (localhost only)
echo   =====================================================
echo.

where docker >nul 2>nul
if errorlevel 1 (
  echo   [X] Docker not found.
  echo       Install Docker Desktop first: https://www.docker.com/products/docker-desktop/
  echo.
  pause
  exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
  echo   [X] Docker Desktop is installed but not running.
  echo       Start Docker Desktop, wait until it says "Engine running", then run this again.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo   [!] .env was missing - created it from .env.example.
    echo       Model analysis needs MODEL_PROVIDER_API_KEY in .env.
    echo       L0 needs no model. Model-required runs show errors with a rules report.
    echo.
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\init-env.ps1"
if errorlevel 1 exit /b 1

echo   [1/3] Starting containers ^(first run builds the image: 1-3 min^)...
docker compose up -d
if errorlevel 1 (
  echo   [X] docker compose up failed - read the messages above.
  echo.
  pause
  exit /b 1
)

echo   [2/3] Waiting for http://127.0.0.1:8000/healthz/deps ...
set /a tries=0
:wait
set /a tries+=1
curl --fail -s -o nul --max-time 8 http://127.0.0.1:8000/healthz/deps
if not errorlevel 1 goto ready
if %tries% geq 90 goto timeout
timeout /t 2 /nobreak >nul
goto wait

:ready
echo   [3/3] Ready - opening http://127.0.0.1:8000/login in your browser.
start "" http://127.0.0.1:8000/login
echo.
echo   First time here?  Register an account, create a project, upload
echo   logs\Mac_2k.log  ^(2000 real log lines, it ships with the repo^),
echo   then press "start analysis" and read its status and report.
echo   Model conclusions need valid settings; L0 shows rule statistics.
echo.
echo   Optional prepared demo ^(needs host Python, README section 6^):
echo       .venv\Scripts\python.exe .dsh\demo_setup.py
echo       then log in as:  demo@evidencelog.local / evidencelog-demo-2026
echo.
echo   Stop:  double-click the stop script   or run:  docker compose down
echo   Rebuild after code changes:  docker compose up -d --build
echo.
pause
exit /b 0

:timeout
echo   [!] Dependencies did not become healthy after repeated checks.
echo       Check:  docker compose ps    and    docker compose logs web
echo.
pause
exit /b 1
