@echo off
setlocal enabledelayedexpansion
title Open WebUI Launcher

echo ==============================================================================
echo                 Starting Open WebUI (fluAi) Container
echo ==============================================================================
echo.

:: Check if Docker is installed and running
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker is not running or not installed!
    echo.
    echo Please make sure Docker Desktop is installed and started, then run this file again.
    echo Download Docker Desktop: https://www.docker.com/products/docker-desktop/
    echo.
    pause
    exit /b 1
)

:: Create .env if not exists
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo [INFO] Created .env file from template.
    )
)

echo [1/2] Building and launching containers...
docker compose up -d --build

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to start Docker containers.
    echo Please check the error message above.
    pause
    exit /b %errorlevel%
)

echo.
echo [2/2] Containers are up and running!
echo ==============================================================================
echo  Open WebUI is ready to use:
echo.
echo  * Desktop Browser : http://localhost:8080
echo  * Mobile App URL  : http://localhost:8080 (or your LAN IP:8080)
echo ==============================================================================
echo.
echo Opening browser...
start http://localhost:8080

echo.
echo To stop the application, run 'stop.bat'.
echo.
pause
