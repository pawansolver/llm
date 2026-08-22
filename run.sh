#!/usr/bin/env bash
set -e

echo "=============================================================================="
echo "                Starting Open WebUI (fluAi) Container"
echo "=============================================================================="
echo ""

# Check if Docker is installed and running
if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker is not running or not installed!"
    echo "Please ensure Docker / Docker Desktop is running and try again."
    exit 1
fi

# Create .env from template if missing
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[INFO] Created .env file from template."
fi

echo "[1/2] Building and launching containers..."
docker compose up -d --build

echo ""
echo "[2/2] Containers are up and running!"
echo "=============================================================================="
echo " Open WebUI is ready:"
echo ""
echo " * Desktop Browser : http://localhost:8080"
echo " * Mobile App URL  : http://localhost:8080 (or your LAN IP:8080)"
echo "=============================================================================="
echo ""

# Open browser if supported
if which xdg-open >/dev/null 2>&1; then
    xdg-open http://localhost:8080 >/dev/null 2>&1 || true
elif which open >/dev/null 2>&1; then
    open http://localhost:8080 >/dev/null 2>&1 || true
fi

echo "To stop the application, run './stop.sh'."
