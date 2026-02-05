"""Object detection service — YOLO inference, tracking, result storage.

Endpoints
---------
GET  /tracks          Current active tracks (JSON)
GET  /detections      All detections for the current session
GET  /media/{name}    Serve a detection JPEG / GIF
GET  /stream          Annotated MJPEG stream (boxes drawn)
WS   /ws              Real-time tracks push
"""

import asyncio
import csv
import datetime
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import imageio
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section  # noqa: E402
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

log = setup_logging("detector", cfg)

app = FastAPI(title="DC-Detector Detection")

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
    global _annotated_frame, _frame_number

    os.makedirs(_session_dir, exist_ok=True)

    # Load YOLO model
    try:
        from ultralytics import YOLO
        model = YOLO(MODEL_PATH)
        log.info("Loaded YOLO model: %s", MODEL_PATH)
    except Exception as exc:
        log.error("Failed to load YOLO model: %s", exc)
        return

    for frame in _read_mjpeg(STREAM_URL):
        _frame_number += 1
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")

        # Run detection + tracking
        try:
            results = model.track(
                frame,
                conf=CONFIDENCE,
                persist=True,
                tracker=f"{TRACKER}.yaml",
                verbose=False,
            )
        except Exception as exc:
            log.error("Detection error: %s", exc)
            continue

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

                track_info = {
                    "track_id": track_id,
                    "class_name": cls_name,
                    "confidence": round(conf, 3),
                    "bbox": {"x": int(x1), "y": int(y1), "w": w, "h": h},
                    "frame_number": _frame_number,
                    "timestamp": ts,
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
    # Resize to max 200px wide for small GIF
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
# WebSocket — push active tracks
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    log.info("Detection WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            with _lock:
                payload = {
                    "event": "tracks",
                    "frame_number": _frame_number,
                    "tracks": list(_active_tracks.values()),
                }
            await ws.send_json(payload)
            await asyncio.sleep(0.25)  # ~4 updates/sec
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    os.makedirs(_session_dir, exist_ok=True)
    threading.Thread(target=_detection_loop, daemon=True).start()
    threading.Thread(target=_periodic_save, daemon=True).start()
    log.info("Detection service started on port %d  (model=%s, tracker=%s)",
             PORT, MODEL_PATH, TRACKER)


@app.on_event("shutdown")
async def on_shutdown():
    _save_results()
    log.info("Detection service shutting down, results saved")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
