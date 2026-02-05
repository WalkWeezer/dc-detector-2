"""Main web server — serves the UI and proxies service status.

Endpoints
---------
GET  /              Web UI (index.html)
GET  /api/services  Status of all sub-services
"""

import os
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section, project_root  # noqa: E402
from log_config import setup_logging  # noqa: E402

cfg = load_config()
web_cfg = get_section(cfg, "web")
PORT = int(web_cfg.get("port", 8080))

cap_port = int(get_section(cfg, "capture").get("port", 8001))
det_port = int(get_section(cfg, "detection").get("port", 8002))
mav_port = int(get_section(cfg, "mavlink").get("port", 8003))
lora_port = int(get_section(cfg, "lora").get("port", 8004))

log = setup_logging("web", cfg)
app = FastAPI(title="DC-Detector Web")

WEB_DIR = os.path.join(project_root(), "web")


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/services")
async def services():
    return JSONResponse({
        "capture": {"port": cap_port, "base": f"http://localhost:{cap_port}"},
        "detection": {"port": det_port, "base": f"http://localhost:{det_port}"},
        "mavlink": {"port": mav_port, "base": f"http://localhost:{mav_port}"},
        "lora": {"port": lora_port, "base": f"http://localhost:{lora_port}"},
    })


@app.on_event("startup")
async def on_startup():
    log.info("Web UI server started on port %d", PORT)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
