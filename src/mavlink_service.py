"""MAVLink 2 UART service — telemetry from flight controller.

Endpoints
---------
GET  /telemetry            Latest raw telemetry snapshot
GET  /telemetry/structured Parsed telemetry (GPS, battery, attitude, flight)
GET  /telemetry/lora       Compact TEL: string for LoRa forwarding
GET  /status               Connection status
GET  /messages             Recent MAVLink messages
POST /command              Send a MAVLink command  {"msg_type": "...", "params": {...}}
WS   /ws                   Real-time telemetry push (raw + structured)
"""

import asyncio
import math
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

os.environ.setdefault("MAVLINK20", "1")  # enable MAVLink 2 protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section  # noqa: E402
from log_config import setup_logging  # noqa: E402

cfg = load_config()
mav_cfg = get_section(cfg, "mavlink")
ENABLED = mav_cfg.get("enabled", True)
DEVICE = mav_cfg.get("device", "/dev/ttyAMA1")
BAUDRATE = int(mav_cfg.get("baudrate", 57600))
PORT = int(mav_cfg.get("port", 8003))

log = setup_logging("mavlink", cfg)


# ---------------------------------------------------------------------------
# ArduPilot Copter flight mode mapping
# ---------------------------------------------------------------------------
_COPTER_MODES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO",
    4: "GUIDED", 5: "LOITER", 6: "RTL", 7: "CIRCLE",
    9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
    15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE",
    18: "THROW", 19: "AVOID_ADSB", 20: "GUIDED_NOGPS",
    21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW",
}


def _resolve_flight_mode(mav_type: int, custom_mode: int) -> str:
    """Resolve custom_mode to a human-readable string (ArduCopter assumed)."""
    if mav_type in (2, 13, 14, 15, 29):
        return _COPTER_MODES.get(custom_mode, f"MODE_{custom_mode}")
    return f"MODE_{custom_mode}"


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
_structured_telemetry: dict = {
    "gps": {"lat": 0.0, "lon": 0.0, "alt_msl": 0.0, "alt_rel": 0.0,
            "fix_type": 0, "satellites": 0, "hdop": 0.0, "vdop": 0.0},
    "attitude": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
    "vfr": {"airspeed": 0.0, "groundspeed": 0.0, "heading": 0,
            "throttle": 0, "climb": 0.0},
    "battery": {"voltage": 0.0, "current": 0.0, "remaining": -1},
    "heartbeat": {"mode": "N/A", "mode_num": 0, "armed": False,
                  "system_status": 0},
    "ts": 0.0,
}
_messages_log: list[dict] = []
_ws_clients: list[WebSocket] = []
_MAX_LOG = 500


# ---------------------------------------------------------------------------
# MAVLink reader thread
# ---------------------------------------------------------------------------

def _extract_structured(msg_type: str, data: dict, ts: float) -> None:
    """Parse well-known MAVLink message types into _structured_telemetry."""
    s = _structured_telemetry

    if msg_type == "GLOBAL_POSITION_INT":
        s["gps"]["lat"] = data.get("lat", 0) / 1e7
        s["gps"]["lon"] = data.get("lon", 0) / 1e7
        s["gps"]["alt_msl"] = data.get("alt", 0) / 1000.0
        s["gps"]["alt_rel"] = data.get("relative_alt", 0) / 1000.0
        s["ts"] = ts

    elif msg_type == "GPS_RAW_INT":
        s["gps"]["fix_type"] = data.get("fix_type", 0)
        s["gps"]["satellites"] = data.get("satellites_visible", 0)
        s["gps"]["hdop"] = data.get("eph", 0) / 100.0
        s["gps"]["vdop"] = data.get("epv", 0) / 100.0

    elif msg_type == "SYS_STATUS":
        s["battery"]["voltage"] = data.get("voltage_battery", 0) / 1000.0
        s["battery"]["current"] = data.get("current_battery", 0) / 100.0
        s["battery"]["remaining"] = data.get("battery_remaining", -1)

    elif msg_type == "VFR_HUD":
        s["vfr"]["airspeed"] = round(data.get("airspeed", 0.0), 1)
        s["vfr"]["groundspeed"] = round(data.get("groundspeed", 0.0), 1)
        s["vfr"]["heading"] = data.get("heading", 0)
        s["vfr"]["throttle"] = data.get("throttle", 0)
        s["vfr"]["climb"] = round(data.get("climb", 0.0), 1)

    elif msg_type == "HEARTBEAT":
        custom_mode = data.get("custom_mode", 0)
        base_mode = data.get("base_mode", 0)
        s["heartbeat"]["armed"] = bool(base_mode & 128)
        s["heartbeat"]["mode_num"] = custom_mode
        s["heartbeat"]["system_status"] = data.get("system_status", 0)
        s["heartbeat"]["mode"] = _resolve_flight_mode(
            data.get("type", 0), custom_mode
        )

    elif msg_type == "ATTITUDE":
        s["attitude"]["roll"] = round(math.degrees(data.get("roll", 0.0)), 1)
        s["attitude"]["pitch"] = round(math.degrees(data.get("pitch", 0.0)), 1)
        s["attitude"]["yaw"] = round(math.degrees(data.get("yaw", 0.0)), 1)


def _mavlink_loop() -> None:
    global _connected

    try:
        from pymavlink import mavutil
    except ImportError:
        log.error("pymavlink not installed — MAVLink service disabled")
        return

    RETRY = 10
    conn = None
    while conn is None:
        log.info("Connecting to MAVLink device %s @ %d baud", DEVICE, BAUDRATE)
        try:
            conn = mavutil.mavlink_connection(DEVICE, baud=BAUDRATE, dialect="ardupilotmega")
            conn.mav.srcSystem = 255
            conn.mav.srcComponent = 0
        except Exception as exc:
            log.warning("Failed to open MAVLink device %s: %s — retrying in %d s", DEVICE, exc, RETRY)
            time.sleep(RETRY)

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
                _extract_structured(msg_type, data, ts)
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


def get_telemetry_string() -> str:
    """Build a compact TEL: string for LoRa transmission (< 200 bytes)."""
    with _lock:
        g = _structured_telemetry["gps"]
        v = _structured_telemetry["vfr"]
        b = _structured_telemetry["battery"]
        h = _structured_telemetry["heartbeat"]
    return (
        f"TEL:{g['lat']:.6f},{g['lon']:.6f},{g['alt_rel']:.1f},"
        f"{v['groundspeed']:.1f},{v['heading']},"
        f"{b['voltage']:.1f},{b['remaining']},"
        f"{g['fix_type']},{g['satellites']},"
        f"{h['mode']},{1 if h['armed'] else 0}"
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@app.get("/telemetry")
async def get_telemetry():
    with _lock:
        return JSONResponse({"telemetry": _telemetry})


@app.get("/telemetry/structured")
async def get_structured_telemetry():
    with _lock:
        return JSONResponse({"telemetry": _structured_telemetry})


@app.get("/telemetry/lora")
async def get_telemetry_lora():
    return JSONResponse({"tel_string": get_telemetry_string()})


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
                    "structured": _structured_telemetry,
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
