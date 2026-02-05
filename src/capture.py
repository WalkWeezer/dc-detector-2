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

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
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

log = setup_logging("capture", cfg)

app = FastAPI(title="DC-Detector Capture")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_latest_frame: np.ndarray | None = None
_recording = False
_video_writer: cv2.VideoWriter | None = None
_recording_path: str | None = None
_cap: cv2.VideoCapture | None = None
_playback_mode = False
_ws_clients: list[WebSocket] = []


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def _open_source(source: str, video_file: str, device_index: int) -> cv2.VideoCapture:
    """Open the video source based on config."""
    if source == "file":
        if not video_file or not os.path.isfile(video_file):
            log.error("Video file not found: %s", video_file)
            raise FileNotFoundError(video_file)
        cap = cv2.VideoCapture(video_file)
        log.info("Opened video file: %s", video_file)
        return cap

    if source in ("csi", "auto"):
        # Try CSI / libcamera (Raspberry Pi)
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

    raise RuntimeError("No camera source available")


def _apply_props(cap: cv2.VideoCapture) -> None:
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)


# ---------------------------------------------------------------------------
# Capture loop (runs in a background thread)
# ---------------------------------------------------------------------------

def _capture_loop() -> None:
    global _latest_frame, _cap, _recording, _video_writer, _playback_mode

    cap_local = _open_source(SRC, VIDEO_FILE, DEVICE_INDEX)
    _apply_props(cap_local)
    with _lock:
        _cap = cap_local

    # Start recording by default if enabled and source is live
    if REC_ENABLED and SRC != "file":
        _start_recording_internal()

    frame_interval = 1.0 / max(FPS, 1)
    while True:
        # Re-read _cap under lock — playback/stop endpoints may swap it
        with _lock:
            if _cap is not cap_local:
                cap_local = _cap

        ok, frame = cap_local.read()
        if not ok:
            if _playback_mode or SRC == "file":
                # Loop file
                cap_local.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            log.warning("Frame grab failed, retrying in 1 s")
            time.sleep(1)
            continue

        # Resize to configured resolution
        if frame.shape[1] != WIDTH or frame.shape[0] != HEIGHT:
            frame = cv2.resize(frame, (WIDTH, HEIGHT))

        with _lock:
            _latest_frame = frame
            if _recording and _video_writer is not None:
                _video_writer.write(frame)

        time.sleep(frame_interval)


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------

def _start_recording_internal() -> str:
    global _recording, _video_writer, _recording_path
    os.makedirs(REC_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = ".avi"
    _recording_path = os.path.join(REC_DIR, f"rec_{ts}{ext}")
    fourcc = cv2.VideoWriter_fourcc(*REC_CODEC)
    _video_writer = cv2.VideoWriter(_recording_path, fourcc, FPS, (WIDTH, HEIGHT))
    _recording = True
    log.info("Recording started: %s", _recording_path)
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
    while True:
        with _lock:
            frame = _latest_frame
        if frame is None:
            await asyncio.sleep(0.05)
            continue
        ok, jpeg = cv2.imencode(".jpg", frame)
        if not ok:
            await asyncio.sleep(0.05)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        )
        await asyncio.sleep(1.0 / max(FPS, 1))


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

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
    ok, jpeg = cv2.imencode(".jpg", frame)
    if not ok:
        return JSONResponse({"error": "encode failed"}, status_code=500)
    return StreamingResponse(
        iter([jpeg.tobytes()]),
        media_type="image/jpeg",
    )


@app.post("/recording/start")
async def recording_start():
    with _lock:
        if _recording:
            return JSONResponse({"status": "already recording", "path": _recording_path})
        path = _start_recording_internal()
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


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    os.makedirs(REC_DIR, exist_ok=True)
    t = threading.Thread(target=_capture_loop, daemon=True)
    t.start()
    log.info("Capture service started on port %d  (source=%s, %dx%d@%dfps)",
             PORT, SRC, WIDTH, HEIGHT, FPS)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
