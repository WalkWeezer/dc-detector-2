#!/usr/bin/env python3
"""Download YOLO models for DC-Detector, optimized for Raspberry Pi 5.

Usage:
    python download_models.py                # download all + export to NCNN
    python download_models.py --no-export    # download .pt only, skip NCNN export
    python download_models.py --list         # list available models

Models:
  Standard (COCO 80 classes — cars, trucks, persons, etc.):
    - yolov8n.pt     (6 MB)  — nano, fastest
    - yolo11n.pt     (5 MB)  — latest generation, best speed/accuracy

  Specialized:
    - fire_smoke.pt           — fire & smoke detection (2 classes)

Pi 5 performance (NCNN format, 640px input):
    yolov8n  → ~94 ms/frame  (~10 FPS)
    yolo11n  → ~68 ms/frame  (~15 FPS)
  At 320px input: ~2x faster → 20-30 FPS
"""

import argparse
import os
import shutil
import sys
import urllib.request

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# ---------------------------------------------------------------------------
# Standard ultralytics models (auto-download via ultralytics library)
# ---------------------------------------------------------------------------
STANDARD_MODELS = [
    "yolov8n.pt",    # COCO nano — 80 classes (car, truck, bus, person, etc.)
    "yolo11n.pt",    # YOLO11 nano — latest, better speed/accuracy ratio
]

# ---------------------------------------------------------------------------
# Specialized models from GitHub (pre-trained .pt files)
# ---------------------------------------------------------------------------
SPECIAL_MODELS = [
    {
        "name": "fire_smoke.pt",
        "desc": "Fire & smoke detection (YOLOv8n fine-tuned, 2 classes)",
        "url": "https://github.com/luminous0219/fire-and-smoke-detection-yolov8/raw/main/runs/detect/train/weights/best.pt",
        "fallback_url": "https://github.com/Abonia1/YOLOv8-Fire-and-Smoke-Detection/raw/main/runs/detect/train/weights/best.pt",
    },
]


def download_file(url, dest):
    """Download a file from URL to dest path."""
    print(f"    Downloading from {url}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DC-Detector/0.2"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
        return True
    except Exception as exc:
        print(f"    Download failed: {exc}")
        return False


def download_standard():
    """Download standard ultralytics models."""
    try:
        from ultralytics import YOLO
    except ImportError:
        print("  ERROR: ultralytics not installed. Run: pip install ultralytics")
        return

    os.makedirs(MODELS_DIR, exist_ok=True)

    for name in STANDARD_MODELS:
        target = os.path.join(MODELS_DIR, name)
        if os.path.isfile(target):
            size_mb = os.path.getsize(target) / (1024 * 1024)
            print(f"  [OK] {name} ({size_mb:.1f} MB)")
            continue

        print(f"  Downloading {name}...")
        try:
            # Ultralytics auto-downloads to cwd or cache
            model = YOLO(name)
            # Find the downloaded file
            src = name
            if os.path.isfile(src):
                shutil.move(src, target)
            elif hasattr(model, "ckpt_path") and os.path.isfile(model.ckpt_path):
                shutil.copy2(model.ckpt_path, target)
            else:
                # Try ultralytics default cache
                from pathlib import Path
                cache_dir = Path.home() / ".cache" / "ultralytics"
                for p in cache_dir.rglob(name):
                    shutil.copy2(str(p), target)
                    break

            if os.path.isfile(target):
                size_mb = os.path.getsize(target) / (1024 * 1024)
                print(f"  [OK] {name} ({size_mb:.1f} MB)")
            else:
                print(f"  [WARN] {name} loaded but .pt not saved to models/")
        except Exception as exc:
            print(f"  [ERROR] {name}: {exc}")


def download_special():
    """Download specialized pre-trained models from GitHub."""
    os.makedirs(MODELS_DIR, exist_ok=True)

    for m in SPECIAL_MODELS:
        target = os.path.join(MODELS_DIR, m["name"])
        if os.path.isfile(target):
            size_mb = os.path.getsize(target) / (1024 * 1024)
            print(f"  [OK] {m['name']} ({size_mb:.1f} MB) — {m['desc']}")
            continue

        print(f"  {m['name']} — {m['desc']}")
        ok = download_file(m["url"], target)
        if not ok and m.get("fallback_url"):
            print("    Trying fallback...")
            ok = download_file(m["fallback_url"], target)

        if ok and os.path.isfile(target):
            size_mb = os.path.getsize(target) / (1024 * 1024)
            print(f"  [OK] {m['name']} ({size_mb:.1f} MB)")
        else:
            print(f"  [FAIL] {m['name']} — see manual download instructions below")


def export_ncnn():
    """Export all .pt models to NCNN format for Pi 5 GPU acceleration."""
    try:
        from ultralytics import YOLO
    except ImportError:
        print("  ERROR: ultralytics not installed")
        return

    import glob
    pt_files = glob.glob(os.path.join(MODELS_DIR, "*.pt"))
    if not pt_files:
        print("  No .pt files found to export")
        return

    for pt_path in pt_files:
        name = os.path.basename(pt_path)
        ncnn_dir = pt_path.replace(".pt", "_ncnn_model")

        if os.path.isdir(ncnn_dir):
            print(f"  [OK] {name} → NCNN already exported")
            continue

        print(f"  Exporting {name} → NCNN...")
        try:
            model = YOLO(pt_path)
            model.export(format="ncnn")
            print(f"  [OK] {name} → NCNN exported")
        except Exception as exc:
            print(f"  [ERROR] {name}: {exc}")
            print("    Install deps: pip install ncnn")


def list_models():
    """List all models in the models directory."""
    if not os.path.isdir(MODELS_DIR):
        print("Models directory does not exist.")
        return

    entries = []
    for f in sorted(os.listdir(MODELS_DIR)):
        fp = os.path.join(MODELS_DIR, f)
        if os.path.isfile(fp):
            size_mb = os.path.getsize(fp) / (1024 * 1024)
            entries.append((f, size_mb))
        elif os.path.isdir(fp):
            total = sum(
                os.path.getsize(os.path.join(dp, fn))
                for dp, _, fns in os.walk(fp) for fn in fns
            )
            entries.append((f + "/", total / (1024 * 1024)))

    if not entries:
        print("  No models found.")
        return

    print(f"\n  {'Model':<45s} {'Size':>8s}")
    print("  " + "-" * 55)
    for name, size_mb in entries:
        print(f"  {name:<45s} {size_mb:>7.1f} MB")


def print_manual_instructions():
    print()
    print("=" * 60)
    print("  MANUAL DOWNLOAD — specialized models")
    print("=" * 60)
    print()
    print("  Fire/smoke detection:")
    print("    https://github.com/luminous0219/fire-and-smoke-detection-yolov8")
    print("    → runs/detect/train/weights/best.pt")
    print("    Save as: models/fire_smoke.pt")
    print()
    print("  Power line / ЛЭП detection:")
    print("    Train on Roboflow datasets:")
    print("    https://universe.roboflow.com/search?q=power+line")
    print("    Command: yolo train data=dataset.yaml model=yolov8n.pt epochs=100")
    print("    Save as: models/power_lines.pt")
    print()
    print("  Road detection (segmentation):")
    print("    https://universe.roboflow.com/search?q=road+segmentation")
    print("    Command: yolo train task=segment data=dataset.yaml model=yolov8n-seg.pt")
    print("    Save as: models/roads_seg.pt")
    print()


def main():
    parser = argparse.ArgumentParser(description="Download YOLO models for DC-Detector")
    parser.add_argument("--no-export", action="store_true", help="Skip NCNN export")
    parser.add_argument("--list", action="store_true", help="List models")
    args = parser.parse_args()

    if args.list:
        list_models()
        return

    print()
    print("=" * 60)
    print("  DC-Detector v0.2 — Model Downloader")
    print("  Target: Raspberry Pi 5 (NCNN format)")
    print("=" * 60)

    print("\n[1/3] Standard models (COCO 80 classes)...")
    download_standard()

    print("\n[2/3] Specialized models (fire, smoke)...")
    download_special()

    if not args.no_export:
        print("\n[3/3] Exporting to NCNN (Pi 5 GPU acceleration)...")
        export_ncnn()
    else:
        print("\n[3/3] NCNN export skipped (use without --no-export to enable)")

    print()
    list_models()
    print_manual_instructions()

    print("  Config hint: to use NCNN model in config.yaml:")
    print('    model_path: "./models/yolov8n_ncnn_model"')
    print()


if __name__ == "__main__":
    main()
