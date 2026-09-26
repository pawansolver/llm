"""
Hugging Face Spaces Entry Point for fluAi (Open WebUI)
"""
import os
import sys
from pathlib import Path

# Ensure backend and root are in python path
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(ROOT_DIR))

# Ensure frontend build dir is accurately located
frontend_dir = ROOT_DIR / "build"
if not frontend_dir.exists():
    frontend_dir = Path.cwd() / "build"

# Set essential runtime environment variables
os.environ.setdefault("PORT", "7860")
os.environ.setdefault("HOST", "0.0.0.0")
os.environ["FRONTEND_BUILD_DIR"] = str(frontend_dir.resolve())
os.environ.setdefault("DATA_DIR", str(BACKEND_DIR / "data"))
os.environ.setdefault("DOCKER", "false")
os.environ.setdefault("WEBUI_SECRET_KEY", "fluai_secret_key_987654321_secure")
os.environ.setdefault("ENABLE_SIGNUP", "true")
os.environ.setdefault("DIFY_API_BASE_URL", "https://pawan11112-diffyproject.hf.space")

print(f"[fluAi] Frontend build dir: {os.environ['FRONTEND_BUILD_DIR']} (exists: {frontend_dir.exists()})")

import gradio as gr
from open_webui.main import app as webui_app

# Minimal Gradio dashboard on subpath /status
with gr.Blocks(title="fluAi Gateway") as demo:
    gr.Markdown("# 🚀 fluAi Gateway")
    gr.Markdown("**Status:** 🟢 Online & Running on Hugging Face Spaces")
    gr.Markdown("WebUI is served directly at root `/` and API at `/api/v1`.")
    gr.Markdown("Diffy FastMCP Server: [https://pawan11112-diffyproject.hf.space](https://pawan11112-diffyproject.hf.space)")

# Mount Gradio on /status subpath so Open WebUI gets the root / and all APIs!
app = gr.mount_gradio_app(webui_app, demo, path="/status")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)
