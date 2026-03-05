"""Video capture service — MJPEG streaming, recording, playback.

Endpoints
---------
GET  /stream          MJPEG video stream
GET  /frame           Single JPEG frame
POST /recording/start Start recording
POST /recording/stop  Stop recording
GET  /recordings      List recorded files
GET  /recordings/{name}  Download a recording
POST /playback        Start playback from a recorded file  {"filename": "..."}
POST /playback/stop   Stop playback, return to live source
WS   /ws              Frame metadata & recording status push
"""

import asyncio
import datetime
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

import platform

import cv2
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section, project_root  # noqa: E402
from log_config import setup_logging  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
cfg = load_config()
cap_cfg = get_section(cfg, "capture")
SRC = cap_cfg.get("source", "auto")
VIDEO_FILE = cap_cfg.get("video_file", "")
DEVICE_INDEX = int(cap_cfg.get("device_index", 0))
WIDTH = int(cap_cfg.get("width", 640))
HEIGHT = int(cap_cfg.get("height", 640))
FPS = int(cap_cfg.get("fps", 30))
PORT = int(cap_cfg.get("port", 8001))
REC_ENABLED = cap_cfg.get("recording", {}).get("enabled", True)
REC_DIR = cap_cfg.get("recording", {}).get("directory", "./data/recordings")
REC_CODEC = cap_cfg.get("recording", {}).get("codec", "MJPG")
JPEG_QUALITY = int(cap_cfg.get("jpeg_quality", 80))
AWB_MODE = cap_cfg.get("awb_mode", "auto").lower()
AWB_SETTLE = float(cap_cfg.get("awb_settle_time", 2.0))
COLOUR_GAINS = cap_cfg.get("colour_gains", None)  # [red, blue] or None

log = setup_logging("capture", cfg)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_latest_frame: np.ndarray | None = None
_recording = False
_video_writer: cv2.VideoWriter | None = None
_recording_path: str | None = None
_cap: "cv2.VideoCapture | Picamera2Capture | None" = None
_playback_mode = False
_ws_clients: list[WebSocket] = []
_actual_fps: float = FPS  # actual camera FPS for recording
_current_awb_mode: str = AWB_MODE
_current_colour_gains: list | None = [float(g) for g in COLOUR_GAINS] if COLOUR_GAINS else None


def _update_actual_fps(cap: cv2.VideoCapture | None) -> None:
    """Read actual FPS from the camera and store it for VideoWriter."""
    global _actual_fps
    if cap is None:
        return
    reported = cap.get(cv2.CAP_PROP_FPS)
    if reported and reported > 0:
        _actual_fps = reported
        log.info("Actual camera FPS for recording: %.1f", _actual_fps)
    else:
        _actual_fps = FPS


# ---------------------------------------------------------------------------
# Picamera2 wrapper (rpicam — Raspberry Pi 5 / Bookworm)
# ---------------------------------------------------------------------------

class Picamera2Capture:
    """Wraps Picamera2 with cv2.VideoCapture-compatible read()/release()/isOpened()."""

    # Picamera2 AwbMode name → libcamera enum value
    _AWB_MODES = {
        "auto": 0, "incandescent": 1, "tungsten": 2,
        "fluorescent": 3, "indoor": 4, "daylight": 5,
        "cloudy": 6, "custom": 7,
    }

    def __init__(self, width: int, height: int, fps: int):
        from picamera2 import Picamera2
        self._cam = Picamera2()

        # Build controls dict
        controls: dict = {"FrameRate": fps}
        if AWB_MODE == "off" and COLOUR_GAINS:
            # Manual white balance
            controls["AwbEnable"] = False
            controls["ColourGains"] = tuple(float(g) for g in COLOUR_GAINS)
            log.info("AWB disabled, manual ColourGains=(%.2f, %.2f)",
                     controls["ColourGains"][0], controls["ColourGains"][1])
        else:
            controls["AwbEnable"] = True
            awb_val = self._AWB_MODES.get(AWB_MODE, 0)
            controls["AwbMode"] = awb_val
            log.info("AWB enabled, mode=%s (%d)", AWB_MODE, awb_val)

        config = self._cam.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"},
            controls=controls,
        )
        self._cam.configure(config)
        self._cam.start()
        # Let auto-exposure & AWB settle
        time.sleep(AWB_SETTLE)
        self._opened = True
        props = self._cam.camera_properties
        log.info("Picamera2 opened: sensor=%s, requested %dx%d@%dfps (AWB settle=%.1fs)",
                 props.get("Model", "?"), width, height, fps, AWB_SETTLE)

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._opened:
            return False, None
        try:
            # Picamera2 "RGB888" format is actually BGR in memory
            # (DRM pixel format naming is MSB-first, but memory layout
            #  on little-endian ARM is BGR) — matches OpenCV directly.
            frame = self._cam.capture_array()
            return True, frame
        except Exception as exc:
            log.warning("Picamera2 read error: %s", exc)
            return False, None

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FPS:
            return float(FPS)
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(WIDTH)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(HEIGHT)
        return 0.0

    def set(self, prop_id: int, value: float) -> bool:
        # Resolution/FPS are set at configure time, ignore runtime changes
        return True

    def set_awb(self, mode: str, colour_gains: list | None = None) -> dict:
        """Change AWB at runtime. Returns the applied settings."""
        mode = mode.lower()
        if mode == "off" and colour_gains:
            gains = tuple(float(g) for g in colour_gains)
            self._cam.set_controls({"AwbEnable": False, "ColourGains": gains})
            log.info("Runtime AWB off, ColourGains=(%.2f, %.2f)", gains[0], gains[1])
            return {"awb_mode": "off", "colour_gains": list(gains)}
        else:
            awb_val = self._AWB_MODES.get(mode, 0)
            self._cam.set_controls({"AwbEnable": True, "AwbMode": awb_val})
            log.info("Runtime AWB mode=%s (%d)", mode, awb_val)
            return {"awb_mode": mode, "colour_gains": None}

    def release(self) -> None:
        if self._opened:
            self._opened = False
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass


def _try_picamera2(width: int, height: int, fps: int) -> Picamera2Capture | None:
    """Try to open camera via Picamera2. Returns None if unavailable."""
    log.info("Trying Picamera2 backend (%dx%d@%dfps)...", width, height, fps)
    try:
        cap = Picamera2Capture(width, height, fps)
        if cap.isOpened():
            return cap
    except ImportError as exc:
        log.warning("Picamera2 not available: %s  "
                     "(install: sudo apt install python3-picamera2, "
                     "venv must use --system-site-packages)", exc)
    except Exception as exc:
        log.warning("Picamera2 init failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def _open_source(source: str, video_file: str, device_index: int) -> cv2.VideoCapture | Picamera2Capture | None:
    """Open the video source based on config.  Returns None if nothing works."""
    log.info("Opening camera source=%s, platform=%s", source, platform.system())
    if source == "file":
        if not video_file or not os.path.isfile(video_file):
            log.error("Video file not found: %s", video_file)
            return None
        cap = cv2.VideoCapture(video_file)
        if cap.isOpened():
            log.info("Opened video file: %s", video_file)
            return cap
        return None

    # rpicam — Picamera2 only (Raspberry Pi CSI camera on Bookworm)
    if source == "rpicam":
        cap = _try_picamera2(WIDTH, HEIGHT, FPS)
        if cap is not None:
            return cap
        log.error("rpicam source requested but Picamera2 failed to open")
        return None

    # auto — try rpicam first on Linux, then V4L2, then USB
    if source == "auto" and platform.system() == "Linux":
        cap = _try_picamera2(WIDTH, HEIGHT, FPS)
        if cap is not None:
            return cap

    if source in ("csi", "auto"):
        try:
            cap = cv2.VideoCapture(device_index, cv2.CAP_V4L2)
            if cap.isOpened():
                log.info("Opened CSI/V4L2 camera index %d", device_index)
                return cap
        except Exception:
            pass

    if source in ("usb", "auto"):
        cap = cv2.VideoCapture(device_index)
        if cap.isOpened():
            log.info("Opened USB camera index %d", device_index)
            return cap

    return None


def _apply_props(cap: cv2.VideoCapture | Picamera2Capture) -> None:
    if isinstance(cap, Picamera2Capture):
        # Already configured at init time
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    log.info("Camera actual resolution: %dx%d @ %.1f fps (requested %dx%d @ %d)",
             actual_w, actual_h, actual_fps, WIDTH, HEIGHT, FPS)


def _no_signal_frame() -> np.ndarray:
    """Generate a 'No Signal' placeholder frame."""
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    text = "NO SIGNAL"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = WIDTH / 400
    thickness = max(1, int(scale * 2))
    (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
    x = (WIDTH - tw) // 2
    y = (HEIGHT + th) // 2
    cv2.putText(frame, text, (x, y), font, scale, (0, 0, 255), thickness)
    return frame


# ---------------------------------------------------------------------------
# Capture loop (runs in a background thread)
# ---------------------------------------------------------------------------

def _capture_loop() -> None:
    global _latest_frame, _cap, _recording, _video_writer, _playback_mode

    RETRY_INTERVAL = 5  # seconds between reconnection attempts

    # --- Initial connection (with retries) ---
    cap_local: cv2.VideoCapture | Picamera2Capture | None = None
    while cap_local is None:
        cap_local = _open_source(SRC, VIDEO_FILE, DEVICE_INDEX)
        if cap_local is None:
            log.warning("No camera source available — showing 'No Signal', retrying in %d s", RETRY_INTERVAL)
            with _lock:
                _latest_frame = _no_signal_frame()
            time.sleep(RETRY_INTERVAL)

    _apply_props(cap_local)
    with _lock:
        _cap = cap_local

    # Detect actual camera FPS for recording
    _update_actual_fps(cap_local)

    # Start recording by default if enabled and source is live
    if REC_ENABLED and SRC != "file":
        _start_recording_internal()

    frame_interval = 1.0 / max(FPS, 1)
    while True:
        t_start = time.monotonic()

        # Re-read _cap under lock — playback/stop endpoints may swap it
        with _lock:
            if _cap is not cap_local:
                cap_local = _cap
                _update_actual_fps(cap_local)

        if cap_local is None:
            with _lock:
                _latest_frame = _no_signal_frame()
            time.sleep(RETRY_INTERVAL)
            # Try to reconnect
            cap_local = _open_source(SRC, VIDEO_FILE, DEVICE_INDEX)
            if cap_local is not None:
                _apply_props(cap_local)
                _update_actual_fps(cap_local)
                with _lock:
                    _cap = cap_local
                log.info("Camera reconnected")
            continue

        ok, frame = cap_local.read()
        if not ok:
            if _playback_mode or SRC == "file":
                # Loop file
                cap_local.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            log.warning("Frame grab failed, retrying in %d s", RETRY_INTERVAL)
            with _lock:
                _latest_frame = _no_signal_frame()
            time.sleep(RETRY_INTERVAL)
            continue

        # Resize only if camera returns a larger frame than configured,
        # preserving the original aspect ratio (no stretching/squashing).
        fh, fw = frame.shape[:2]
        if fw != WIDTH or fh != HEIGHT:
            src_ratio = fw / fh
            dst_ratio = WIDTH / HEIGHT
            if abs(src_ratio - dst_ratio) < 0.01:
                # Same aspect ratio — simple resize
                frame = cv2.resize(frame, (WIDTH, HEIGHT))
            else:
                # Different aspect ratio — fit inside WIDTH x HEIGHT, keep AR
                scale = min(WIDTH / fw, HEIGHT / fh)
                new_w = int(fw * scale)
                new_h = int(fh * scale)
                frame = cv2.resize(frame, (new_w, new_h))

        with _lock:
            _latest_frame = frame
            rec_active = _recording
            writer = _video_writer

        # Write outside lock so disk I/O doesn't block the MJPEG generator
        if rec_active and writer is not None:
            writer.write(frame)

        # Sleep only for the remaining time to hit target FPS.
        # cap.read() already blocks for live cameras, so this avoids
        # doubling the frame interval.
        elapsed = time.monotonic() - t_start
        sleep_time = frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------

def _start_recording_internal(rec_id: str | None = None) -> str:
    global _recording, _video_writer, _recording_path
    os.makedirs(REC_DIR, exist_ok=True)
    ts = rec_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = ".avi"
    _recording_path = os.path.join(REC_DIR, f"rec_{ts}{ext}")
    fourcc = cv2.VideoWriter_fourcc(*REC_CODEC)
    # Use actual frame size (not config) so recording matches the stream
    # NOTE: caller holds _lock (endpoint) or is the capture thread — no extra lock needed
    f = _latest_frame
    rec_w = f.shape[1] if f is not None else WIDTH
    rec_h = f.shape[0] if f is not None else HEIGHT
    _video_writer = cv2.VideoWriter(_recording_path, fourcc, _actual_fps, (rec_w, rec_h))
    _recording = True
    log.info("Recording started: %s (%dx%d @ %.1f fps)", _recording_path, rec_w, rec_h, _actual_fps)
    return _recording_path


def _stop_recording_internal() -> str | None:
    global _recording, _video_writer, _recording_path
    path = _recording_path
    _recording = False
    if _video_writer is not None:
        _video_writer.release()
        _video_writer = None
    log.info("Recording stopped: %s", path)
    return path


# ---------------------------------------------------------------------------
# MJPEG generator
# ---------------------------------------------------------------------------

async def _mjpeg_generator():
    _encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    _interval = 1.0 / max(FPS, 1)
    while True:
        with _lock:
            frame = _latest_frame
        if frame is None:
            await asyncio.sleep(0.05)
            continue
        t0 = time.monotonic()
        ok, jpeg = cv2.imencode(".jpg", frame, _encode_params)
        if not ok:
            await asyncio.sleep(0.01)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        )
        # Sleep for remaining interval (min 1 ms to keep event loop responsive
        # for detector httpx, WebSocket, and other async tasks)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.001, _interval - elapsed))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    os.makedirs(REC_DIR, exist_ok=True)
    t = threading.Thread(target=_capture_loop, daemon=True)
    t.start()
    log.info("Capture service started on port %d  (source=%s, %dx%d@%dfps)",
             PORT, SRC, WIDTH, HEIGHT, FPS)
    yield

app = FastAPI(title="DC-Detector Capture", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/status")
async def get_status():
    with _lock:
        return JSONResponse({
            "recording": _recording,
            "recording_path": _recording_path,
            "playback": _playback_mode,
            "frame_available": _latest_frame is not None,
        })


@app.get("/awb")
async def get_awb():
    return JSONResponse({
        "awb_mode": _current_awb_mode,
        "colour_gains": _current_colour_gains,
        "modes": ["auto", "daylight", "cloudy", "tungsten",
                  "fluorescent", "incandescent", "indoor", "off"],
    })


@app.post("/awb")
async def set_awb(request: Request):
    global _current_awb_mode, _current_colour_gains
    body = await request.json()
    mode = body.get("awb_mode", _current_awb_mode)
    gains = body.get("colour_gains", _current_colour_gains)

    with _lock:
        cap_local = _cap
    if isinstance(cap_local, Picamera2Capture):
        result = cap_local.set_awb(mode, gains)
        _current_awb_mode = result["awb_mode"]
        _current_colour_gains = result["colour_gains"]
    else:
        # Non-Picamera2 — just store for reference
        _current_awb_mode = mode
        _current_colour_gains = gains
        log.info("AWB settings saved (non-Picamera2 source)")

    return JSONResponse({
        "awb_mode": _current_awb_mode,
        "colour_gains": _current_colour_gains,
    })


@app.get("/stream")
async def stream():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/frame")
async def single_frame():
    with _lock:
        frame = _latest_frame
    if frame is None:
        return JSONResponse({"error": "no frame available"}, status_code=503)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return JSONResponse({"error": "encode failed"}, status_code=500)
    return Response(content=jpeg.tobytes(), media_type="image/jpeg")


@app.post("/recording/start")
async def recording_start(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    rec_id = body.get("id") if isinstance(body, dict) else None
    with _lock:
        if _recording and rec_id:
            # Restart with the shared ID so timestamps match the detector recording
            _stop_recording_internal()
        elif _recording:
            return JSONResponse({"status": "already recording", "path": _recording_path})
        path = _start_recording_internal(rec_id=rec_id)
    await _broadcast({"event": "recording_started", "path": path})
    return JSONResponse({"status": "started", "path": path})


@app.post("/recording/stop")
async def recording_stop():
    with _lock:
        if not _recording:
            return JSONResponse({"status": "not recording"})
        path = _stop_recording_internal()
    await _broadcast({"event": "recording_stopped", "path": path})
    return JSONResponse({"status": "stopped", "path": path})


@app.get("/recordings")
async def list_recordings():
    os.makedirs(REC_DIR, exist_ok=True)
    files = []
    for f in sorted(os.listdir(REC_DIR)):
        fp = os.path.join(REC_DIR, f)
        if os.path.isfile(fp):
            files.append({
                "filename": f,
                "size_bytes": os.path.getsize(fp),
                "modified": os.path.getmtime(fp),
            })
    return JSONResponse({"recordings": files})


@app.get("/recordings/{name}")
async def download_recording(name: str):
    fp = os.path.join(REC_DIR, name)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fp, filename=name)


@app.delete("/recordings/{name}")
async def delete_recording(name: str):
    fp = os.path.join(REC_DIR, name)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    # Don't delete if it's the active recording
    with _lock:
        if _recording and _recording_path and os.path.basename(_recording_path) == name:
            return JSONResponse({"error": "Cannot delete active recording"}, status_code=400)
    try:
        os.remove(fp)
        log.info("Deleted recording: %s", fp)
        return JSONResponse({"status": "deleted", "filename": name})
    except Exception as exc:
        log.error("Failed to delete recording %s: %s", name, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


class PlaybackRequest(BaseModel):
    filename: str


@app.post("/playback")
async def start_playback(req: PlaybackRequest):
    global _cap, _playback_mode
    fp = os.path.join(REC_DIR, req.filename)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "file not found"}, status_code=404)
    with _lock:
        if _recording:
            _stop_recording_internal()
        if _cap is not None:
            _cap.release()
        _cap = cv2.VideoCapture(fp)
        _apply_props(_cap)
        _playback_mode = True
    log.info("Playback started: %s", fp)
    return JSONResponse({"status": "playback", "file": req.filename})


@app.post("/playback/stop")
async def stop_playback():
    global _cap, _playback_mode
    with _lock:
        _playback_mode = False
        if _cap is not None:
            _cap.release()
        _cap = _open_source(SRC, VIDEO_FILE, DEVICE_INDEX)
        if _cap is not None:
            _apply_props(_cap)
    log.info("Playback stopped, live source restored")
    return JSONResponse({"status": "live"})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def _broadcast(msg: dict) -> None:
    import json
    data = json.dumps(msg)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            _ws_clients.remove(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    log.info("WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            # Push status every second
            with _lock:
                status = {
                    "event": "status",
                    "recording": _recording,
                    "recording_path": _recording_path,
                    "playback": _playback_mode,
                    "frame_available": _latest_frame is not None,
                }
            await ws.send_json(status)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
