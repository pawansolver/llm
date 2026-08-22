# 🚀 Open WebUI (fluAi) - Client Quickstart & Deployment Guide

This guide explains how to run the entire Open WebUI application with a single click using Docker.

---

## 📋 Prerequisites

Before running the application, ensure you have **Docker** installed on your system:
- **Windows / macOS**: Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Ensure Docker Desktop is running before starting the app.
- **Linux**: Install Docker Engine and Docker Compose plugin (`sudo apt install docker.io docker-compose-v2`).

---

## ⚡ Quick Start (1-Click Run)

### 🪟 On Windows:
1. Double-click **`run.bat`** (or run `.\run.bat` in Command Prompt/PowerShell).
2. The script will automatically build and launch the Docker containers.
3. Your web browser will open automatically at **`http://localhost:8080`**.

To stop the app, double-click **`stop.bat`**.

---

### 🍎 On macOS / 🐧 On Linux:
1. Open terminal inside the project directory and run:
   ```bash
   chmod +x run.sh stop.sh
   ./run.sh
   ```
2. Open your browser at **`http://localhost:8080`**.

To stop the app, run:
```bash
./stop.sh
```

---

## ⚙️ Configuration (Optional)

You can customize settings by editing the **`.env`** file in the root folder:

| Variable | Description | Default |
|---|---|---|
| `PORT` | The port on which the web app runs | `8080` |
| `OPENAI_API_KEY` | Your OpenAI API key (for GPT models, Whisper STT, OpenAI TTS) | *(empty)* |
| `OLLAMA_BASE_URL` | Ollama connection endpoint for local models | `http://ollama:11434` |
| `WEBUI_SECRET_KEY` | JWT encryption secret key | *(auto-generated)* |

---

## 📱 Connecting the Android Mobile App

If you have installed the **`fluAi`** Android app on your phone:
1. Make sure your phone and the computer running Docker are on the **same Wi-Fi network**.
2. Find your computer's local IP address (`ipconfig` on Windows or `ifconfig` on Linux/Mac, e.g. `192.168.1.50`).
3. The mobile app connects directly to `http://<YOUR_COMPUTER_IP>:8080`.
4. Open the Voice Mode / Siri overlay in the mobile app and start interacting!

---

## 💾 Data Persistence & Backups

All chats, users, model configurations, and knowledge bases are permanently saved inside the **`open-webui-data`** Docker volume. 
Stopping or rebuilding the container **will NOT delete** your data.

To see logs or troubleshoot:
```bash
docker compose logs -f open-webui
```
