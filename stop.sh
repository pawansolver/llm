#!/usr/bin/env bash
set -e

echo "=============================================================================="
echo "                Stopping Open WebUI (fluAi) Container"
echo "=============================================================================="
echo ""

docker compose down

echo ""
echo "Containers stopped safely. Data and configurations are preserved."
