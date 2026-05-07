@echo off
setlocal enabledelayedexpansion
rem Capsule launcher (Windows).
rem Double-click to install on first run, double-click again to start.

set IMAGE=ghcr.io/capsule/capsule:latest
set CONTAINER=capsule
set PORT=8080
set DOWNLOADS=%USERPROFILE%\Documents\Capsule
set CONFIG=%USERPROFILE%\Documents\Capsule\.config

cd /d "%~dp0"

echo =============================================
echo   Capsule -- Capture the web, with proof
echo =============================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker is not installed.
  echo.
  echo Capsule needs Docker Desktop. It is free.
  echo Download:  https://www.docker.com/products/docker-desktop
  echo.
  echo After installing Docker Desktop, double-click this launcher again.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo Starting Docker Desktop...
  start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" 2>nul
  set /a tries=0
  :wait_docker
  timeout /t 2 /nobreak >nul
  docker info >nul 2>&1
  if not errorlevel 1 goto docker_ok
  set /a tries+=1
  if !tries! lss 30 goto wait_docker
  echo Docker Desktop did not start. Open it from the Start menu, then re-run.
  pause
  exit /b 1
)
:docker_ok

docker image inspect %IMAGE% >nul 2>&1
if errorlevel 1 (
  echo Setting up Capsule ^(first launch only -- about 2GB download^)...
  docker pull %IMAGE% >nul 2>&1
  if errorlevel 1 (
    if exist Dockerfile (
      echo Registry image not available; building from local source...
      docker build -t %IMAGE% .
      if errorlevel 1 (
        echo Build failed. Check Docker Desktop logs.
        pause
        exit /b 1
      )
    ) else (
      echo Could not download or build the Capsule image.
      echo Check your internet connection and try again.
      pause
      exit /b 1
    )
  )
)

if not exist "%DOWNLOADS%" mkdir "%DOWNLOADS%"
if not exist "%CONFIG%" mkdir "%CONFIG%"

set RUNNING=
for /f "delims=" %%i in ('docker ps --format "{{.Names}}" 2^>nul ^| findstr /x "%CONTAINER%"') do set RUNNING=%%i
if defined RUNNING (
  echo Capsule is already running.
  goto wait_app
)

set EXISTS=
for /f "delims=" %%i in ('docker ps -a --format "{{.Names}}" 2^>nul ^| findstr /x "%CONTAINER%"') do set EXISTS=%%i
if defined EXISTS (
  docker start %CONTAINER% >nul
  echo Capsule restarted.
) else (
  docker run -d --name %CONTAINER% --restart no ^
    -p %PORT%:8080 ^
    -v "%DOWNLOADS%:/downloads" ^
    -v "%CONFIG%:/config" ^
    -e "CAPSULE_HOST_DOWNLOADS_DIR=%DOWNLOADS%" ^
    %IMAGE% >nul
  if errorlevel 1 (
    echo Failed to start Capsule. Check Docker Desktop.
    pause
    exit /b 1
  )
  echo Capsule started.
)

:wait_app
set URL=http://localhost:%PORT%
echo Waiting for the app to come online...
set /a tries=0
:poll
curl -sf %URL%/healthz >nul 2>&1
if not errorlevel 1 goto app_ok
timeout /t 1 /nobreak >nul
set /a tries+=1
if !tries! lss 30 goto poll
echo App did not respond at %URL%. Open Docker Desktop to see container logs.
pause
exit /b 1

:app_ok
start "" %URL%
echo.
echo Capsule is open in your browser at  %URL%
echo To stop the app:        docker stop capsule
echo To start it next time:  double-click this launcher.
echo Capsule does NOT auto-start with your computer.
timeout /t 3 /nobreak >nul
exit /b 0
