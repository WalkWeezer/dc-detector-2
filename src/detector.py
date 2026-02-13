"""Object detection service — YOLO inference, tracking, result storage.

Endpoints
---------
GET  /tracks          Current active tracks (JSON)
GET  /detections      All detections for the current session
GET  /media/{name}    Serve a detection JPEG / GIF
GET  /stream          Annotated MJPEG stream (boxes drawn)
GET  /config          Current runtime detection config
POST /config          Update runtime detection config
GET  /models          List available YOLO models
POST /model           Switch active model
GET  /metrics         Real-time performance metrics
GET  /sessions        List all detection sessions
DELETE /sessions/{id} Delete a detection session
WS   /ws              Real-time tracks + metrics push
"""

import asyncio
import csv
import datetime
import glob as _glob
import io
import json
import os
import shutil
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import imageio
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section, project_root  # noqa: E402
from log_config import setup_logging  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
cfg = load_config()
det_cfg = get_section(cfg, "detection")
MODEL_PATH = det_cfg.get("model_path", "./models/yolov8n.pt")
CONFIDENCE = float(det_cfg.get("confidence", 0.5))
STREAM_URL = det_cfg.get("stream_url", "http://localhost:8001/stream")
PORT = int(det_cfg.get("port", 8002))
RESULTS_DIR = det_cfg.get("results_dir", "./data/detections")
GIF_DURATION = int(det_cfg.get("gif_duration", 5))
RESULTS_FMT = det_cfg.get("results_format", "json")
TRACKER = det_cfg.get("tracker", "bytetrack")

cap_cfg = get_section(cfg, "capture")
SRC_FPS = int(cap_cfg.get("fps", 30))

MODELS_DIR = os.path.join(project_root(), "models")

log = setup_logging("detector", cfg)


# ---------------------------------------------------------------------------
# Lifespan & App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    os.makedirs(_session_dir, exist_ok=True)
    threading.Thread(target=_detection_loop, daemon=True).start()
    threading.Thread(target=_periodic_save, daemon=True).start()
    log.info("Detection service started on port %d  (model=%s, tracker=%s)",
             PORT, MODEL_PATH, TRACKER)
    yield
    _save_results()
    log.info("Detection service shutting down, results saved")

app = FastAPI(title="DC-Detector Detection", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
_session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_session_dir = os.path.join(RESULTS_DIR, f"session_{_session_id}")

_lock = threading.Lock()
_active_tracks: dict[int, dict] = {}
_all_detections: list[dict] = []
_annotated_frame: np.ndarray | None = None
_frame_number = 0
_ws_clients: list[WebSocket] = []

# Per-track buffers for GIF creation: {track_id: {"frames": [...], "start": float, "done": bool}}
_gif_buffers: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# Runtime-mutable config & metrics
# ---------------------------------------------------------------------------
_runtime_conf = CONFIDENCE
_runtime_save_conf = CONFIDENCE  # min confidence to persist detection in DB
_runtime_imgsz = 640
_runtime_skip = 0            # skip N frames between inferences
_pending_model: str | None = None  # set by POST /model to trigger hot-swap

_track_first_seen: dict[int, str] = {}  # track_id → ISO timestamp

# Performance metrics
_metrics_lock = threading.Lock()
_fps_times: deque[float] = deque(maxlen=120)
_frame_ms: deque[float] = deque(maxlen=120)
_last_inference_ms: float = 0.0
_current_model_path: str = MODEL_PATH

# ---------------------------------------------------------------------------
# MJPEG stream reader
# ---------------------------------------------------------------------------

def _read_mjpeg(url: str):
    """Yield frames from an MJPEG HTTP stream."""
    import httpx

    log.info("Connecting to capture stream: %s", url)
    while True:
        try:
            with httpx.stream("GET", url, timeout=None) as resp:
                buf = b""
                for chunk in resp.iter_bytes(4096):
                    buf += chunk
                    while True:
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2 if a >= 0 else 0)
                        if a < 0 or b < 0:
                            break
                        jpg = buf[a : b + 2]
                        buf = buf[b + 2 :]
                        frame = cv2.imdecode(
                            np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                        if frame is not None:
                            yield frame
        except Exception as exc:
            log.warning("Stream read error: %s — retrying in 2 s", exc)
            time.sleep(2)


# ---------------------------------------------------------------------------
# Detection loop
# ---------------------------------------------------------------------------

def _detection_loop() -> None:
    global _annotated_frame, _frame_number, _current_model_path
    global _pending_model, _last_inference_ms

    os.makedirs(_session_dir, exist_ok=True)

    # Load YOLO model
    try:
        from ultralytics import YOLO
        model = YOLO(MODEL_PATH)
        _current_model_path = MODEL_PATH
        log.info("Loaded YOLO model: %s", MODEL_PATH)
    except Exception as exc:
        log.error("Failed to load YOLO model: %s", exc)
        return

    skip_counter = 0

    for frame in _read_mjpeg(STREAM_URL):
        _frame_number += 1

        # ── Hot-swap model if requested ──
        if _pending_model is not None:
            new_path = _pending_model
            _pending_model = None
            try:
                from ultralytics import YOLO as _YOLO
                model = _YOLO(new_path)
                _current_model_path = new_path
                log.info("Hot-swapped model to: %s", new_path)
            except Exception as exc:
                log.error("Model swap failed: %s", exc)

        # ── Skip frames to reduce load ──
        if _runtime_skip > 0:
            skip_counter += 1
            if skip_counter <= _runtime_skip:
                with _lock:
                    _annotated_frame = frame.copy()
                continue
            skip_counter = 0

        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        t0 = time.perf_counter()

        # Run detection + tracking
        try:
            results = model.track(
                frame,
                conf=_runtime_conf,
                imgsz=_runtime_imgsz,
                persist=True,
                tracker=f"{TRACKER}.yaml",
                verbose=False,
            )
        except Exception as exc:
            log.error("Detection error: %s", exc)
            continue

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Update metrics
        now = time.time()
        with _metrics_lock:
            _fps_times.append(now)
            _frame_ms.append(elapsed_ms)
            _last_inference_ms = elapsed_ms

        current_tracks: dict[int, dict] = {}
        if results and results[0].boxes is not None:
            boxes = results[0].boxes
            for i in range(len(boxes)):
                box = boxes[i]
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                w, h = int(x2 - x1), int(y2 - y1)
                conf = float(box.conf[0])
                cls_id = int(box.cls[0])
                cls_name = model.names.get(cls_id, str(cls_id))
                track_id = int(box.id[0]) if box.id is not None else -1

                # Track first seen
                if track_id >= 0 and track_id not in _track_first_seen:
                    _track_first_seen[track_id] = ts

                track_info = {
                    "track_id": track_id,
                    "class_name": cls_name,
                    "confidence": round(conf, 3),
                    "bbox": {"x": int(x1), "y": int(y1), "w": w, "h": h},
                    "frame_number": _frame_number,
                    "timestamp": ts,
                    "first_seen": _track_first_seen.get(track_id, ts),
                }

                # Save JPEG crop on first appearance
                jpeg_rel = ""
                gif_rel = ""
                if track_id >= 0:
                    jpeg_name = f"track_{track_id}.jpg"
                    gif_name = f"track_{track_id}.gif"
                    jpeg_path = os.path.join(_session_dir, jpeg_name)
                    gif_path = os.path.join(_session_dir, gif_name)
                    jpeg_rel = f"session_{_session_id}/{jpeg_name}"
                    gif_rel = f"session_{_session_id}/{gif_name}"

                    if not os.path.exists(jpeg_path):
                        crop = _safe_crop(frame, x1, y1, x2, y2)
                        cv2.imwrite(jpeg_path, crop)
                        log.info("Saved JPEG crop: %s", jpeg_path)

                    # Buffer frames for GIF
                    _buffer_gif_frame(track_id, frame, x1, y1, x2, y2, gif_path)

                track_info["jpeg_url"] = f"/media/{jpeg_rel}" if jpeg_rel else ""
                track_info["gif_url"] = f"/media/{gif_rel}" if gif_rel else ""
                current_tracks[track_id] = track_info

        # Draw annotations
        annotated = results[0].plot() if results else frame.copy()

        with _lock:
            _active_tracks.clear()
            _active_tracks.update(current_tracks)
            _annotated_frame = annotated

            for t in current_tracks.values():
                if t["confidence"] >= _runtime_save_conf:
                    _all_detections.append(t)

        # Throttle to avoid overload
        time.sleep(0.001)

    log.warning("Detection loop ended (stream closed)")


def _safe_crop(frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return frame
    return crop


def _buffer_gif_frame(
    track_id: int, frame: np.ndarray,
    x1: int, y1: int, x2: int, y2: int,
    gif_path: str,
) -> None:
    """Collect cropped frames for GIF_DURATION seconds, then write GIF."""
    now = time.time()
    if track_id not in _gif_buffers:
        _gif_buffers[track_id] = {"frames": [], "start": now, "done": False, "path": gif_path}

    buf = _gif_buffers[track_id]
    if buf["done"]:
        return

    crop = _safe_crop(frame, x1, y1, x2, y2)
    # Convert BGR → RGB for imageio
    crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)

    # Fix size to the first frame's dimensions so all GIF frames match
    if buf["frames"]:
        target_h, target_w = buf["frames"][0].shape[:2]
        crop_rgb = cv2.resize(crop_rgb, (target_w, target_h))
    else:
        # First frame — cap width at 200 px
        h, w = crop_rgb.shape[:2]
        if w > 200:
            scale = 200 / w
            crop_rgb = cv2.resize(crop_rgb, (200, int(h * scale)))
    buf["frames"].append(crop_rgb)

    if now - buf["start"] >= GIF_DURATION and len(buf["frames"]) >= 5:
        # Write GIF
        try:
            # Sample ~20 frames max for reasonable file size
            frames = buf["frames"]
            step = max(1, len(frames) // 20)
            sampled = frames[::step]
            imageio.mimsave(buf["path"], sampled, duration=0.25, loop=0)
            log.info("Saved GIF: %s (%d frames)", buf["path"], len(sampled))
        except Exception as exc:
            log.error("GIF save failed: %s", exc)
        buf["done"] = True
        buf["frames"] = []  # free memory


# ---------------------------------------------------------------------------
# Save session results
# ---------------------------------------------------------------------------

def _save_results() -> None:
    with _lock:
        detections = list(_all_detections)

    if not detections:
        return

    if RESULTS_FMT == "csv":
        path = os.path.join(_session_dir, "results.csv")
        keys = ["timestamp", "frame_number", "track_id", "class_name",
                "confidence", "bbox", "jpeg_url", "gif_url"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            for d in detections:
                row = {k: d.get(k, "") for k in keys}
                row["bbox"] = json.dumps(d.get("bbox", {}))
                writer.writerow(row)
    else:
        path = os.path.join(_session_dir, "results.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "session_id": _session_id,
                "total": len(detections),
                "detections": detections,
            }, f, ensure_ascii=False, indent=1)

    log.info("Saved session results: %s (%d detections)", path, len(detections))


def _periodic_save() -> None:
    """Save results every 30 seconds."""
    while True:
        time.sleep(30)
        _save_results()


# ---------------------------------------------------------------------------
# Helpers: metrics
# ---------------------------------------------------------------------------

def _calc_metrics() -> dict:
    with _metrics_lock:
        fps_list = list(_fps_times)
        ms_list = list(_frame_ms)
        last_ms = _last_inference_ms

    fps = 0.0
    if len(fps_list) >= 2:
        span = fps_list[-1] - fps_list[0]
        if span > 0:
            fps = (len(fps_list) - 1) / span

    avg_ms = sum(ms_list) / len(ms_list) if ms_list else 0.0

    return {
        "fps": round(fps, 1),
        "avg_frame_ms": round(avg_ms, 1),
        "last_inference_ms": round(last_ms, 1),
        "frame_number": _frame_number,
        "active_tracks": len(_active_tracks),
        "total_detections": len(_all_detections),
        "session_id": _session_id,
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/tracks")
async def get_tracks():
    with _lock:
        return JSONResponse({"tracks": list(_active_tracks.values())})


@app.get("/detections")
async def get_detections():
    with _lock:
        return JSONResponse({
            "session_id": _session_id,
            "total": len(_all_detections),
            "detections": _all_detections[-200:],  # last 200
        })


@app.get("/media/{path:path}")
async def serve_media(path: str):
    fp = os.path.join(RESULTS_DIR, path)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fp)


@app.get("/config")
async def get_config():
    return JSONResponse({
        "confidence": round(_runtime_conf, 3),
        "save_confidence": round(_runtime_save_conf, 3),
        "imgsz": _runtime_imgsz,
        "skip_frames": _runtime_skip,
        "model_path": _current_model_path,
        "tracker": TRACKER,
    })


@app.post("/config")
async def set_config(request: Request):
    global _runtime_conf, _runtime_save_conf, _runtime_imgsz, _runtime_skip
    body = await request.json()
    if "confidence" in body:
        _runtime_conf = max(0.05, min(1.0, float(body["confidence"])))
    if "save_confidence" in body:
        _runtime_save_conf = max(0.05, min(1.0, float(body["save_confidence"])))
    if "imgsz" in body:
        v = int(body["imgsz"])
        if v in (160, 320, 480, 640, 960, 1280):
            _runtime_imgsz = v
    if "skip_frames" in body:
        _runtime_skip = max(0, min(30, int(body["skip_frames"])))
    log.info("Config updated: conf=%.2f save_conf=%.2f imgsz=%d skip=%d",
             _runtime_conf, _runtime_save_conf, _runtime_imgsz, _runtime_skip)
    return JSONResponse({
        "confidence": round(_runtime_conf, 3),
        "save_confidence": round(_runtime_save_conf, 3),
        "imgsz": _runtime_imgsz,
        "skip_frames": _runtime_skip,
    })


@app.get("/models")
async def list_models():
    models = []
    if os.path.isdir(MODELS_DIR):
        for ext in ("*.pt", "*.onnx", "*.engine"):
            for fp in _glob.glob(os.path.join(MODELS_DIR, ext)):
                name = os.path.basename(fp)
                size_mb = os.path.getsize(fp) / (1024 * 1024)
                models.append({"name": name, "path": fp, "size_mb": round(size_mb, 1)})
    return JSONResponse({
        "current": os.path.basename(_current_model_path),
        "models": models,
    })


@app.post("/model")
async def switch_model(request: Request):
    global _pending_model
    body = await request.json()
    name = body.get("name", "")
    path = os.path.join(MODELS_DIR, name)
    if not os.path.isfile(path):
        return JSONResponse({"error": f"Model not found: {name}"}, status_code=404)
    _pending_model = path
    log.info("Model switch requested: %s", path)
    return JSONResponse({"status": "switching", "model": name})


@app.get("/metrics")
async def get_metrics():
    return JSONResponse(_calc_metrics())


@app.get("/sessions")
async def list_sessions():
    """List all detection sessions with metadata."""
    sessions = []
    if os.path.isdir(RESULTS_DIR):
        for entry in sorted(os.listdir(RESULTS_DIR), reverse=True):
            sdir = os.path.join(RESULTS_DIR, entry)
            if not os.path.isdir(sdir) or not entry.startswith("session_"):
                continue
            sid = entry.replace("session_", "")
            # Count files
            files = os.listdir(sdir)
            jpg_count = sum(1 for f in files if f.endswith(".jpg"))
            gif_count = sum(1 for f in files if f.endswith(".gif"))
            has_results = any(f.startswith("results.") for f in files)
            # Get total size
            total_size = sum(
                os.path.getsize(os.path.join(sdir, f))
                for f in files if os.path.isfile(os.path.join(sdir, f))
            )
            # Parse detection count from results file
            det_count = 0
            classes = set()
            results_path = os.path.join(sdir, "results.json")
            if os.path.isfile(results_path):
                try:
                    with open(results_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    det_count = data.get("total", 0)
                    for d in data.get("detections", []):
                        cn = d.get("class_name", "")
                        if cn:
                            classes.add(cn)
                except Exception:
                    pass
            sessions.append({
                "session_id": sid,
                "dir_name": entry,
                "detections": det_count,
                "tracks": jpg_count,
                "gifs": gif_count,
                "classes": sorted(classes),
                "size_bytes": total_size,
                "has_results": has_results,
                "created": os.path.getctime(sdir),
                "active": sid == _session_id,
            })
    return JSONResponse({"sessions": sessions})


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a detection session directory."""
    sdir = os.path.join(RESULTS_DIR, f"session_{session_id}")
    if not os.path.isdir(sdir):
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if session_id == _session_id:
        return JSONResponse({"error": "Cannot delete active session"}, status_code=400)
    try:
        shutil.rmtree(sdir)
        log.info("Deleted session: %s", sdir)
        return JSONResponse({"status": "deleted", "session_id": session_id})
    except Exception as exc:
        log.error("Failed to delete session %s: %s", session_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def _annotated_mjpeg():
    while True:
        with _lock:
            frame = _annotated_frame
        if frame is None:
            await asyncio.sleep(0.1)
            continue
        ok, jpeg = cv2.imencode(".jpg", frame)
        if not ok:
            await asyncio.sleep(0.05)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        )
        await asyncio.sleep(1.0 / max(SRC_FPS, 1))


@app.get("/stream")
async def annotated_stream():
    return StreamingResponse(
        _annotated_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# WebSocket — push active tracks + metrics
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    log.info("Detection WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            metrics = _calc_metrics()
            with _lock:
                payload = {
                    "event": "tracks",
                    "frame_number": _frame_number,
                    "tracks": list(_active_tracks.values()),
                    "metrics": metrics,
                }
            await ws.send_json(payload)
            await asyncio.sleep(0.25)  # ~4 updates/sec
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
