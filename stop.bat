@echo off
title Stopping Open WebUI Container

echo ==============================================================================
echo                 Stopping Open WebUI (fluAi) Container
echo ==============================================================================
echo.

set "PATH=%PATH%;%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;C:\Program Files\Docker\Docker\resources\bin"

docker compose down

echo.
echo Containers have been stopped safely.
echo All your data, chats, and models remain securely saved.
echo.
pause
