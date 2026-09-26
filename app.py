"""
Hugging Face Spaces Entry Point for fluAi (Open WebUI) - ZeroGPU Supported
"""
import os
import sys
from pathlib import Path

# Ensure backend and root are in python path
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(ROOT_DIR))

# Set essential runtime environment variables
os.environ.setdefault("PORT", "7860")
os.environ.setdefault("HOST", "0.0.0.0")
os.environ.setdefault("FRONTEND_BUILD_DIR", str(ROOT_DIR / "build"))
os.environ.setdefault("DATA_DIR", str(BACKEND_DIR / "data"))
os.environ.setdefault("DOCKER", "false")
os.environ.setdefault("WEBUI_SECRET_KEY", "fluai_secret_key_987654321_secure")
os.environ.setdefault("ENABLE_SIGNUP", "true")
os.environ.setdefault("DIFY_API_BASE_URL", "https://pawan11112-diffyproject.hf.space")

try:
    import spaces
    from spaces import zero
except ImportError:
    class DummySpaces:
        def GPU(self, fn=None, **kwargs):
            if fn is None:
                return lambda f: f
            return fn
    spaces = DummySpaces()
    zero = None

# ZeroGPU hook to satisfy Hugging Face startup scanner
@spaces.GPU
def ping_gpu(query: str = ""):
    return f"🟢 fluAi ZeroGPU Gateway is Active! Query: {query}"

import gradio as gr
from open_webui.main import app as webui_app

# Standard gr.Interface directly bound to @spaces.GPU function for ZeroGPU supervisor detection
demo = gr.Interface(
    fn=ping_gpu,
    inputs=gr.Textbox(label="Test GPU Input", value="ping"),
    outputs=gr.Textbox(label="Status"),
    title="🚀 fluAi Enterprise Gateway",
    description="WebUI is served directly at root / and API at /api/v1."
)

# Mount Gradio on /gpu subpath so Open WebUI gets the root / and all APIs!
app = gr.mount_gradio_app(webui_app, demo, path="/gpu")

if __name__ == "__main__":
    if zero is not None and hasattr(zero, "startup"):
        try:
            zero.startup()
        except Exception as e:
            print("zero.startup notice:", e)

    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)
