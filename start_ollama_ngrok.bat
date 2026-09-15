@echo off
title Ollama Permanent Live Tunnel (ngrok)
echo ========================================================
echo       Ollama Permanent Live Tunnel (ngrok)
echo ========================================================
echo.

:: Optimize Ollama performance (Keep model permanently in memory, no unload)
set OLLAMA_KEEP_ALIVE=-1
set OLLAMA_NUM_PARALLEL=2
set OLLAMA_ORIGINS=*

:: Check if Ollama is running
curl -s http://127.0.0.1:11434 >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/2] Starting Ollama in background...
    start /B "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 3 /nobreak >nul
) else (
    echo [1/2] Ollama is already running.
)

echo [2/2] Starting ngrok Permanent Tunnel...
echo.
echo ========================================================
echo PERMANENT URL (YE KABHI CHANGE NAHI HOGA):
echo https://pursuant-refill-subfloor.ngrok-free.dev
echo ========================================================
echo.

"%USERPROFILE%\Downloads\ngrok.exe" http --url=pursuant-refill-subfloor.ngrok-free.dev --host-header="localhost:11434" 11434
pause
