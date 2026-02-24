"""LoRa / ESP32 UART service — packet exchange with Heltec LoRa32.

Endpoints
---------
GET  /status      Connection status
GET  /messages    Recent received messages
POST /send        Send a text message  {"text": "..."}
WS   /ws          Real-time message push

Auto-forwarding (AIR mode):
  TEL: telemetry   — from mavlink_service every N seconds
  DET: detections  — new tracks from detector service
  STS: system status — recording, fps, tracks, model

Command handling (from ground via LoRa):
  CMD:REC_START  → POST capture /recording/start
  CMD:REC_STOP   → POST capture /recording/stop
"""

import asyncio
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
TEL_ENABLED = lora_cfg.get("telemetry_forward", True)
TEL_INTERVAL = float(lora_cfg.get("telemetry_interval", 2.0))
MAV_PORT = int(cfg.get("mavlink", {}).get("port", 8003))
DET_PORT = int(cfg.get("detection", {}).get("port", 8002))
CAP_PORT = int(cfg.get("capture", {}).get("port", 8001))

# Forward intervals
DET_INTERVAL = float(lora_cfg.get("detection_interval", 3.0))
STS_INTERVAL = float(lora_cfg.get("status_interval", 5.0))

# ESP32 WiFi auto-connect (Linux only)
_esp_wifi_cfg = lora_cfg.get("esp_wifi", {})
ESP_WIFI_ENABLED = _esp_wifi_cfg.get("enabled", False)
ESP_WIFI_HOSTNAME = _esp_wifi_cfg.get("hostname", "dc-detect")

log = setup_logging("lora", cfg)


# ---------------------------------------------------------------------------
# Lifespan & App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    if ENABLED:
        threading.Thread(target=_serial_loop, daemon=True).start()
        if TEL_ENABLED:
            threading.Thread(target=_telemetry_forward_loop, daemon=True).start()
            threading.Thread(target=_detection_forward_loop, daemon=True).start()
            threading.Thread(target=_status_forward_loop, daemon=True).start()
        if ESP_WIFI_ENABLED:
            threading.Thread(target=_esp_wifi_connect_loop, daemon=True).start()
        log.info("LoRa service started on port %d (device=%s)", PORT, DEVICE)
    else:
        log.info("LoRa service disabled in config")
    yield

app = FastAPI(title="DC-Detector LoRa", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_connected = False
_serial_port = None
_messages: list[dict] = []
_ws_clients: list[WebSocket] = []
_MAX_MSG = 1000

# ESP32 WiFi connection state
_esp_wifi_connected = False
_esp_wifi_ip = ""
_esp_wifi_ssid = ""

# AP info received from ESP32 via serial (AP:ssid,password,ip)
_esp_ap_ssid = ""
_esp_ap_password = ""


# ---------------------------------------------------------------------------
# Serial reader thread
# ---------------------------------------------------------------------------

def _serial_loop() -> None:
    global _connected, _serial_port, _esp_ap_ssid, _esp_ap_password

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

                    # Tag message types
                    if "TEL:" in line:
                        msg["type"] = "telemetry_rx"
                    elif line.startswith("CMD:"):
                        msg["type"] = "command"
                        # Process command from ground station
                        cmd = line[4:].strip()
                        _handle_command(cmd)
                    elif line.startswith("AP:"):
                        msg["type"] = "ap_info"
                        parts = line[3:].split(",")
                        if len(parts) >= 2:
                            _esp_ap_ssid = parts[0]
                            _esp_ap_password = parts[1]
                            log.debug("ESP32 AP info: SSID=%s", _esp_ap_ssid)

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
# Command handler (ground station → Pi services)
# ---------------------------------------------------------------------------

IMG_CHUNK_BYTES = 100  # binary bytes per LoRa chunk (→ 200 hex chars + header ≈ 215 bytes < 255 LoRa limit)


def _send_track_image(track_id: int, httpx) -> None:
    """Fetch detection JPEG, resize to thumbnail, send as IMG: chunks via serial."""
    try:
        # Find jpeg_url for this track
        resp = httpx.get(f"http://localhost:{DET_PORT}/detections", timeout=3.0)
        if resp.status_code != 200:
            log.warning("GET_IMG: cannot fetch detections")
            return
        jpeg_url = ""
        for d in resp.json().get("detections", []):
            if d.get("track_id") == track_id and d.get("jpeg_url"):
                jpeg_url = d["jpeg_url"]
        if not jpeg_url:
            log.warning("GET_IMG: no JPEG for track %d", track_id)
            return

        # Download original JPEG
        img_resp = httpx.get(f"http://localhost:{DET_PORT}{jpeg_url}", timeout=5.0)
        if img_resp.status_code != 200:
            log.warning("GET_IMG: failed to fetch %s", jpeg_url)
            return

        # Resize to tiny thumbnail and compress heavily
        import cv2
        import numpy as np
        arr = np.frombuffer(img_resp.content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            log.warning("GET_IMG: failed to decode image")
            return
        img = cv2.resize(img, (120, 90))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 15])
        if not ok:
            return
        data = buf.tobytes()
        log.info("GET_IMG track %d: %d bytes (120x90 q15)", track_id, len(data))

        # Send as pre-chunked IMG: lines via serial
        # Delay must be >= LoRa TX time (~100-300ms at SF7/BW125) to avoid UART overflow
        total_chunks = (len(data) + IMG_CHUNK_BYTES - 1) // IMG_CHUNK_BYTES
        log.info("GET_IMG: sending %d chunks (%d bytes), ETA ~%ds",
                 total_chunks, len(data), total_chunks * 300 // 1000)
        for c in range(total_chunks):
            start = c * IMG_CHUNK_BYTES
            end = min(start + IMG_CHUNK_BYTES, len(data))
            hex_data = data[start:end].hex().upper()
            line = f"IMG:{c}:{total_chunks}:{hex_data}\n"
            _serial_port.write(line.encode("utf-8"))
            time.sleep(0.3)  # wait for ESP32 to relay chunk via LoRa
        log.info("GET_IMG: sent %d chunks to ESP32", total_chunks)

    except Exception as exc:
        log.error("GET_IMG error: %s", exc)


def _handle_command(cmd: str) -> None:
    """Process command received from ground station via LoRa."""
    try:
        import httpx
    except ImportError:
        log.error("httpx not installed — cannot process commands")
        return

    log.info("Processing ground CMD: %s", cmd)
    try:
        if cmd == "REC_START":
            resp = httpx.post(f"http://localhost:{CAP_PORT}/recording/start", timeout=3.0)
            log.info("CMD REC_START → capture: %s", resp.json())
        elif cmd == "REC_STOP":
            resp = httpx.post(f"http://localhost:{CAP_PORT}/recording/stop", timeout=3.0)
            log.info("CMD REC_STOP → capture: %s", resp.json())
        elif cmd.startswith("SET_CONF:"):
            val = float(cmd.split(":", 1)[1])
            resp = httpx.post(f"http://localhost:{DET_PORT}/config",
                              json={"confidence": val}, timeout=3.0)
            log.info("CMD SET_CONF → detector: %s", resp.json())
        elif cmd.startswith("SET_IMGSZ:"):
            val = int(cmd.split(":", 1)[1])
            resp = httpx.post(f"http://localhost:{DET_PORT}/config",
                              json={"imgsz": val}, timeout=3.0)
            log.info("CMD SET_IMGSZ → detector: %s", resp.json())
        elif cmd.startswith("SET_MODEL:"):
            name = cmd.split(":", 1)[1]
            resp = httpx.post(f"http://localhost:{DET_PORT}/model",
                              json={"name": name}, timeout=5.0)
            log.info("CMD SET_MODEL → detector: %s", resp.json())
        elif cmd.startswith("GET_IMG:"):
            _send_track_image(int(cmd.split(":", 1)[1]), httpx)
        else:
            log.warning("Unknown ground command: %s", cmd)
    except Exception as exc:
        log.error("Command execution error: %s", exc)


# ---------------------------------------------------------------------------
# Telemetry forward thread (TEL: lines from MAVLink → ESP32)
# ---------------------------------------------------------------------------

def _telemetry_forward_loop() -> None:
    """Periodically fetch structured telemetry from mavlink_service and push to ESP32."""
    try:
        import httpx
    except ImportError:
        log.error("httpx not installed — telemetry forwarding disabled")
        return

    log.info("Telemetry forward thread started (interval=%.1fs, mavlink port=%d)", TEL_INTERVAL, MAV_PORT)
    url = f"http://localhost:{MAV_PORT}/telemetry/lora"

    while True:
        time.sleep(TEL_INTERVAL)
        if _serial_port is None or not _connected:
            continue
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                tel_string = resp.json().get("tel_string", "")
                if tel_string and tel_string.startswith("TEL:"):
                    payload = (tel_string + "\n").encode("utf-8")
                    _serial_port.write(payload)
                    with _lock:
                        _messages.append({
                            "direction": "tx",
                            "data": tel_string,
                            "ts": time.time(),
                            "type": "telemetry",
                        })
                    log.debug("TEL forwarded to ESP32: %s", tel_string)
        except Exception as exc:
            log.debug("Telemetry forward error: %s", exc)


# ---------------------------------------------------------------------------
# Detection forward thread (DET: new tracks → ESP32 → LoRa → ground)
# ---------------------------------------------------------------------------

def _detection_forward_loop() -> None:
    """Poll detector for active tracks and send DET: via serial to ESP32.

    Only /tracks (current-frame active tracks) is used — this ensures the LoRa
    module sees exactly the same tracks as the Pi web dashboard.  All active
    tracks are re-sent every cycle so timestamps stay fresh on the ESP32/GND.
    """
    try:
        import httpx
    except ImportError:
        log.error("httpx not installed — detection forwarding disabled")
        return

    log.info("Detection forward thread started (interval=%.1fs)", DET_INTERVAL)
    tracks_url = f"http://localhost:{DET_PORT}/tracks"
    tel_url = f"http://localhost:{MAV_PORT}/telemetry/structured"
    cached_gps = {"lat": 0.0, "lon": 0.0, "alt": 0.0}

    while True:
        time.sleep(DET_INTERVAL)
        if _serial_port is None or not _connected:
            continue
        try:
            # Update cached GPS from mavlink
            try:
                gps_resp = httpx.get(tel_url, timeout=2.0)
                if gps_resp.status_code == 200:
                    gps = gps_resp.json().get("telemetry", {}).get("gps", {})
                    cached_gps["lat"] = gps.get("lat", 0.0)
                    cached_gps["lon"] = gps.get("lon", 0.0)
                    cached_gps["alt"] = gps.get("alt_msl", 0.0)
            except Exception:
                pass

            # Send DET: for ALL currently active tracks (same as Pi web dashboard)
            resp = httpx.get(tracks_url, timeout=2.0)
            if resp.status_code != 200:
                continue
            for t in resp.json().get("tracks", []):
                tid = t.get("track_id", -1)
                if tid < 0:
                    continue
                cls = t.get("class_name", "unknown")
                conf = t.get("confidence", 0.0)
                det_line = f"DET:{cls},{conf:.2f},{tid},{cached_gps['lat']:.6f},{cached_gps['lon']:.6f},{cached_gps['alt']:.1f}"
                _serial_port.write((det_line + "\n").encode("utf-8"))
                with _lock:
                    _messages.append({
                        "direction": "tx",
                        "data": det_line,
                        "ts": time.time(),
                        "type": "detection",
                    })
                log.debug("DET forwarded: %s", det_line)

        except Exception as exc:
            log.debug("Detection forward error: %s", exc)


# ---------------------------------------------------------------------------
# Status forward thread (STS: system status → ESP32 → LoRa → ground)
# ---------------------------------------------------------------------------

def _status_forward_loop() -> None:
    """Periodically send STS: system status over serial to ESP32."""
    try:
        import httpx
    except ImportError:
        log.error("httpx not installed — status forwarding disabled")
        return

    log.info("Status forward thread started (interval=%.1fs)", STS_INTERVAL)
    metrics_url = f"http://localhost:{DET_PORT}/metrics"
    config_url = f"http://localhost:{DET_PORT}/config"
    cap_status_url = f"http://localhost:{CAP_PORT}/status"

    while True:
        time.sleep(STS_INTERVAL)
        if _serial_port is None or not _connected:
            continue
        try:
            recording = False
            fps = 0.0
            tracks = 0
            model = "N/A"
            conf = 0.5
            imgsz = 640
            inf_ms = 0.0
            cpu_temp = -1.0

            # Get Raspberry Pi CPU temperature
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    cpu_temp = int(f.read().strip()) / 1000.0
            except Exception:
                pass

            # Get capture recording status
            try:
                resp = httpx.get(cap_status_url, timeout=2.0)
                if resp.status_code == 200:
                    recording = resp.json().get("recording", False)
            except Exception:
                pass

            # Get detector metrics (fps, active tracks, inference time)
            try:
                resp = httpx.get(metrics_url, timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    fps = data.get("fps", 0.0)
                    tracks = data.get("active_tracks", 0)
                    inf_ms = data.get("last_inference_ms", 0.0)
            except Exception:
                pass

            # Get current model name + config
            try:
                resp = httpx.get(config_url, timeout=2.0)
                if resp.status_code == 200:
                    cfg_data = resp.json()
                    mp = cfg_data.get("model_path", "")
                    if mp:
                        model = os.path.basename(mp)
                    conf = cfg_data.get("confidence", 0.5)
                    imgsz = cfg_data.get("imgsz", 640)
            except Exception:
                pass

            rec_int = 1 if recording else 0
            # STS:rec,fps,tracks,model,conf,imgsz,inf_ms,cpu_temp
            sts_line = f"STS:{rec_int},{fps:.1f},{tracks},{model},{conf:.2f},{imgsz},{inf_ms:.0f},{cpu_temp:.1f}"
            payload = (sts_line + "\n").encode("utf-8")
            _serial_port.write(payload)
            with _lock:
                _messages.append({
                    "direction": "tx",
                    "data": sts_line,
                    "ts": time.time(),
                    "type": "status",
                })
            log.debug("STS forwarded: %s", sts_line)

        except Exception as exc:
            log.debug("Status forward error: %s", exc)


# ---------------------------------------------------------------------------
# ESP32 WiFi auto-connect thread (Pi → ESP32 AP, Linux only)
# ---------------------------------------------------------------------------

def _get_wlan0_ssid(subprocess) -> str:
    """Return the SSID that wlan0 is currently connected to, or empty string."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.strip().split("\n"):
            if "GENERAL.CONNECTION" in line:
                ssid = line.split(":", 1)[-1].strip()
                if ssid and ssid != "--":
                    return ssid
    except Exception:
        pass
    return ""


def _esp_wifi_connect_loop() -> None:
    """Auto-connect Pi to ESP32 WiFi AP using SSID/password received via serial."""
    global _esp_wifi_connected, _esp_wifi_ip, _esp_wifi_ssid

    import platform
    if platform.system() != "Linux":
        log.info("ESP WiFi auto-connect: skipped (not Linux)")
        return

    import subprocess

    # Check if nmcli is available
    try:
        result = subprocess.run(["nmcli", "--version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            log.warning("nmcli not available — ESP WiFi auto-connect disabled")
            return
    except FileNotFoundError:
        log.warning("nmcli not found — ESP WiFi auto-connect disabled")
        return

    WEB_PORT = int(cfg.get("web", {}).get("port", 8080))
    avahi_proc = None
    last_wifi_send = 0.0
    ap_wait_logged = False

    log.info("ESP WiFi auto-connect thread started (hostname=%s), waiting for AP info from serial...",
             ESP_WIFI_HOSTNAME)

    while True:
        # --- Already connected: monitor + periodically re-send info ---
        if _esp_wifi_connected:
            # Re-send WIFI info to ESP32 every 30s (in case ESP32 rebooted)
            if time.time() - last_wifi_send > 30 and _serial_port and _connected:
                wifi_line = f"WIFI:{_esp_wifi_ip},{ESP_WIFI_HOSTNAME},{WEB_PORT}"
                try:
                    _serial_port.write((wifi_line + "\n").encode("utf-8"))
                    last_wifi_send = time.time()
                except Exception:
                    pass

            # Check if still connected to the correct SSID
            try:
                current_ssid = _get_wlan0_ssid(subprocess)
                if current_ssid != _esp_wifi_ssid:
                    _esp_wifi_connected = False
                    _esp_wifi_ip = ""
                    log.warning("WiFi switched from ESP32 AP (%s) to %s — will reconnect",
                                _esp_wifi_ssid, current_ssid or "disconnected")
                    if avahi_proc:
                        avahi_proc.kill()
                        avahi_proc = None
            except Exception:
                pass
            time.sleep(10)
            continue

        # --- Wait for AP info from ESP32 via serial ---
        if not _esp_ap_ssid:
            if not ap_wait_logged:
                log.info("Waiting for AP credentials from ESP32 via serial (AP: message)...")
                ap_wait_logged = True
            time.sleep(5)
            continue
        ap_wait_logged = False

        target = _esp_ap_ssid
        password = _esp_ap_password

        # --- Disconnect current WiFi if connected to a different network ---
        current_ssid = _get_wlan0_ssid(subprocess)
        if current_ssid and current_ssid != target:
            log.info("wlan0 connected to '%s' — disconnecting to switch to ESP32 AP '%s'",
                     current_ssid, target)
            try:
                subprocess.run(["nmcli", "device", "disconnect", "wlan0"],
                               capture_output=True, timeout=10)
                time.sleep(2)
            except Exception as exc:
                log.warning("Failed to disconnect wlan0: %s", exc)

        # --- Connect to the exact SSID received from our ESP32 ---
        try:
            log.info("Connecting to ESP32 AP: %s", target)

            # Remove stale connection profiles for this SSID
            try:
                subprocess.run(
                    ["nmcli", "connection", "delete", target],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

            # Connect with explicit interface
            result = subprocess.run(
                ["nmcli", "device", "wifi", "connect", target,
                 "password", password, "ifname", "wlan0"],
                capture_output=True, text=True, timeout=30,
            )

            if result.returncode != 0:
                log.warning("Failed to connect to %s: %s", target, result.stderr.strip())
                time.sleep(10)
                continue

            # Set high autoconnect priority so NM doesn't switch to other networks
            try:
                subprocess.run(
                    ["nmcli", "connection", "modify", target,
                     "connection.autoconnect", "yes",
                     "connection.autoconnect-priority", "100"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

            # Wait for DHCP
            time.sleep(3)

            # Get assigned IP
            ip = ""
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "IP4.ADDRESS", "connection", "show", target],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().split("\n"):
                    if ":" in line:
                        addr = line.split(":", 1)[-1].strip()
                        if "/" in addr:
                            addr = addr.split("/")[0]
                        if addr:
                            ip = addr
                            break
            except Exception:
                pass

            if not ip:
                log.warning("Connected to %s but no IP assigned", target)
                time.sleep(5)
                continue

            _esp_wifi_connected = True
            _esp_wifi_ip = ip
            _esp_wifi_ssid = target
            log.info("Connected to ESP32 AP %s — IP: %s", target, ip)

            # Send WIFI info to ESP32 via serial
            wifi_line = f"WIFI:{ip},{ESP_WIFI_HOSTNAME},{WEB_PORT}"
            if _serial_port and _connected:
                try:
                    _serial_port.write((wifi_line + "\n").encode("utf-8"))
                    last_wifi_send = time.time()
                    log.info("Sent WiFi info to ESP32: %s", wifi_line)
                except Exception as exc:
                    log.warning("Failed to send WiFi info: %s", exc)

            # Publish mDNS hostname via avahi
            if avahi_proc:
                avahi_proc.kill()
                avahi_proc = None
            try:
                avahi_proc = subprocess.Popen(
                    ["avahi-publish", "-a", f"{ESP_WIFI_HOSTNAME}.local", ip],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                log.info("mDNS: publishing %s.local -> %s", ESP_WIFI_HOSTNAME, ip)
            except FileNotFoundError:
                log.warning("avahi-publish not found — access Pi via IP: %s:%d", ip, WEB_PORT)

        except Exception as exc:
            log.debug("ESP WiFi connect error: %s", exc)

        time.sleep(10)


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
            "telemetry_forward": TEL_ENABLED,
            "esp_wifi": {
                "enabled": ESP_WIFI_ENABLED,
                "connected": _esp_wifi_connected,
                "ssid": _esp_wifi_ssid,
                "ip": _esp_wifi_ip,
                "hostname": ESP_WIFI_HOSTNAME,
            },
        })


@app.get("/esp_wifi")
async def get_esp_wifi():
    return JSONResponse({
        "enabled": ESP_WIFI_ENABLED,
        "connected": _esp_wifi_connected,
        "ssid": _esp_wifi_ssid,
        "ip": _esp_wifi_ip,
        "hostname": ESP_WIFI_HOSTNAME,
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
