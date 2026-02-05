"""MAVLink 2 UART service — telemetry from flight controller.

Endpoints
---------
GET  /telemetry   Latest telemetry snapshot
GET  /status      Connection status
POST /command     Send a MAVLink command  {"msg_type": "...", "params": {...}}
WS   /ws          Real-time telemetry push
"""

import asyncio
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section  # noqa: E402
from log_config import setup_logging  # noqa: E402

cfg = load_config()
mav_cfg = get_section(cfg, "mavlink")
ENABLED = mav_cfg.get("enabled", True)
DEVICE = mav_cfg.get("device", "/dev/ttyAMA0")
BAUDRATE = int(mav_cfg.get("baudrate", 57600))
PORT = int(mav_cfg.get("port", 8003))

log = setup_logging("mavlink", cfg)


# ---------------------------------------------------------------------------
# Lifespan & App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if ENABLED:
        threading.Thread(target=_mavlink_loop, daemon=True).start()
        log.info("MAVLink service started on port %d (device=%s)", PORT, DEVICE)
    else:
        log.info("MAVLink service disabled in config")
    yield

app = FastAPI(title="DC-Detector MAVLink", lifespan=lifespan)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_connected = False
_telemetry: dict = {}
_messages_log: list[dict] = []
_ws_clients: list[WebSocket] = []
_MAX_LOG = 500


# ---------------------------------------------------------------------------
# MAVLink reader thread
# ---------------------------------------------------------------------------

def _mavlink_loop() -> None:
    global _connected, _telemetry

    try:
        from pymavlink import mavutil
    except ImportError:
        log.error("pymavlink not installed — MAVLink service disabled")
        return

    log.info("Connecting to MAVLink device %s @ %d baud", DEVICE, BAUDRATE)
    try:
        conn = mavutil.mavlink_connection(DEVICE, baud=BAUDRATE)
    except Exception as exc:
        log.error("Failed to open MAVLink device: %s", exc)
        return

    with _lock:
        _connected = True
    log.info("MAVLink connection established")

    while True:
        try:
            msg = conn.recv_match(blocking=True, timeout=2)
            if msg is None:
                continue

            msg_type = msg.get_type()
            if msg_type == "BAD_DATA":
                continue

            data = msg.to_dict()
            data.pop("mavpackettype", None)
            ts = time.time()

            with _lock:
                _telemetry[msg_type] = {"data": data, "ts": ts}
                _messages_log.append({
                    "type": msg_type,
                    "ts": ts,
                    "data": data,
                })
                if len(_messages_log) > _MAX_LOG:
                    del _messages_log[: _MAX_LOG // 2]

        except Exception as exc:
            log.warning("MAVLink read error: %s", exc)
            time.sleep(1)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@app.get("/telemetry")
async def get_telemetry():
    with _lock:
        return JSONResponse({"telemetry": _telemetry})


@app.get("/status")
async def get_status():
    with _lock:
        return JSONResponse({
            "connected": _connected,
            "device": DEVICE,
            "baudrate": BAUDRATE,
            "message_types": list(_telemetry.keys()),
        })


@app.get("/messages")
async def get_messages():
    with _lock:
        return JSONResponse({"messages": _messages_log[-100:]})


class CommandRequest(BaseModel):
    msg_type: str
    params: dict = {}


@app.post("/command")
async def send_command(req: CommandRequest):
    try:
        from pymavlink import mavutil
    except ImportError:
        return JSONResponse({"error": "pymavlink not installed"}, status_code=500)
    # Sending MAVLink commands requires a connection reference;
    # for safety, only log the request in this version.
    log.info("MAVLink command requested: %s %s", req.msg_type, req.params)
    return JSONResponse({"status": "queued", "msg_type": req.msg_type})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    log.info("MAVLink WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            with _lock:
                payload = {
                    "event": "telemetry",
                    "connected": _connected,
                    "telemetry": _telemetry,
                }
            await ws.send_json(payload)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
