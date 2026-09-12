@echo off
title Ollama Live Cloudflare Tunnel
echo ========================================================
echo         Ollama Live Tunnel for Open WebUI & Mobile APK
echo ========================================================
echo.

:: Check if Ollama is running
curl -s http://127.0.0.1:11434 >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/2] Starting Ollama in background...
    start /B "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 3 /nobreak >nul
) else (
    echo [1/2] Ollama is already running.
)

echo [2/2] Starting Cloudflare Live Tunnel...
echo.
echo ========================================================
echo COPY THE URL BELOW (ending with .trycloudflare.com)
echo and paste it into Render Environment as OLLAMA_BASE_URL
echo ========================================================
echo.

"%USERPROFILE%\Downloads\cloudflared.exe" tunnel --url http://127.0.0.1:11434 --http-host-header 127.0.0.1:11434
pause
