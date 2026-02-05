"""LoRa / ESP32 UART service — packet exchange with Heltec LoRa32.

Endpoints
---------
GET  /status      Connection status
GET  /messages    Recent received messages
POST /send        Send a text message  {"text": "..."}
WS   /ws          Real-time message push
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
lora_cfg = get_section(cfg, "lora")
ENABLED = lora_cfg.get("enabled", True)
DEVICE = lora_cfg.get("device", "/dev/ttyAMA0")
BAUDRATE = int(lora_cfg.get("baudrate", 115200))
PORT = int(lora_cfg.get("port", 8004))

log = setup_logging("lora", cfg)


# ---------------------------------------------------------------------------
# Lifespan & App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if ENABLED:
        threading.Thread(target=_serial_loop, daemon=True).start()
        log.info("LoRa service started on port %d (device=%s)", PORT, DEVICE)
    else:
        log.info("LoRa service disabled in config")
    yield

app = FastAPI(title="DC-Detector LoRa", lifespan=lifespan)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_connected = False
_serial_port = None
_messages: list[dict] = []
_ws_clients: list[WebSocket] = []
_MAX_MSG = 1000


# ---------------------------------------------------------------------------
# Serial reader thread
# ---------------------------------------------------------------------------

def _serial_loop() -> None:
    global _connected, _serial_port

    try:
        import serial
    except ImportError:
        log.error("pyserial not installed — LoRa service disabled")
        return

    RETRY = 10  # seconds between retries
    while True:
        log.info("Opening LoRa serial device %s @ %d baud", DEVICE, BAUDRATE)
        try:
            _serial_port = serial.Serial(DEVICE, BAUDRATE, timeout=1)
            break
        except Exception as exc:
            log.warning("Failed to open serial device %s: %s — retrying in %d s", DEVICE, exc, RETRY)
            time.sleep(RETRY)

    with _lock:
        _connected = True
    log.info("LoRa serial connection established")

    while True:
        try:
            if _serial_port.in_waiting > 0:
                line = _serial_port.readline().decode("utf-8", errors="replace").strip()
                if line:
                    ts = time.time()
                    msg = {
                        "direction": "rx",
                        "data": line,
                        "ts": ts,
                    }
                    # Parse RSSI if present (firmware appends RSSI info)
                    if "RSSI:" in line:
                        try:
                            rssi_val = int(line.split("RSSI:")[-1].strip().split()[0])
                            msg["rssi"] = rssi_val
                        except (ValueError, IndexError):
                            pass

                    with _lock:
                        _messages.append(msg)
                        if len(_messages) > _MAX_MSG:
                            del _messages[: _MAX_MSG // 2]

                    log.debug("LoRa RX: %s", line)
            else:
                time.sleep(0.05)
        except Exception as exc:
            log.warning("LoRa read error: %s", exc)
            time.sleep(1)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@app.get("/status")
async def get_status():
    with _lock:
        return JSONResponse({
            "connected": _connected,
            "device": DEVICE,
            "baudrate": BAUDRATE,
            "total_messages": len(_messages),
        })


@app.get("/messages")
async def get_messages():
    with _lock:
        return JSONResponse({"messages": _messages[-100:]})


class SendRequest(BaseModel):
    text: str


@app.post("/send")
async def send_message(req: SendRequest):
    if _serial_port is None or not _connected:
        return JSONResponse({"error": "not connected"}, status_code=503)
    try:
        payload = (req.text + "\n").encode("utf-8")
        _serial_port.write(payload)
        ts = time.time()
        with _lock:
            _messages.append({"direction": "tx", "data": req.text, "ts": ts})
        log.info("LoRa TX: %s", req.text)
        return JSONResponse({"status": "sent", "text": req.text})
    except Exception as exc:
        log.error("LoRa TX error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    last_idx = 0
    log.info("LoRa WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            with _lock:
                new_msgs = _messages[last_idx:]
                last_idx = len(_messages)
                connected = _connected
            if new_msgs:
                await ws.send_json({
                    "event": "messages",
                    "connected": connected,
                    "messages": new_msgs,
                })
            else:
                await ws.send_json({
                    "event": "heartbeat",
                    "connected": connected,
                })
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
