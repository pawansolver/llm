@echo off
title Stopping Open WebUI Container

echo ==============================================================================
echo                 Stopping Open WebUI (fluAi) Container
echo ==============================================================================
echo.

docker compose down

echo.
echo Containers have been stopped safely.
echo All your data, chats, and models remain securely saved.
echo.
pause
