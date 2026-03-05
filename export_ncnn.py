#!/usr/bin/env python3
"""Export YOLO .pt models to NCNN format (optimised for Raspberry Pi 5).

Usage:
    python export_ncnn.py                       # export all .pt in models/
    python export_ncnn.py models/yolo11n.pt     # export a specific model
    python export_ncnn.py --imgsz 320           # custom input size (default 320)
"""

import argparse
import glob
import os
import sys


def export_one(pt_path: str, imgsz: int) -> bool:
    ncnn_dir = pt_path.replace(".pt", "_ncnn_model")
    if os.path.isdir(ncnn_dir):
        print(f"  [skip] NCNN already exists: {ncnn_dir}")
        return True

    print(f"  Exporting {pt_path} → NCNN (imgsz={imgsz}) ...")
    try:
        from ultralytics import YOLO
        model = YOLO(pt_path)
        model.export(format="ncnn", imgsz=imgsz)
        if os.path.isdir(ncnn_dir):
            size_mb = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(ncnn_dir) for f in fns
            ) / (1024 * 1024)
            print(f"  [ok] {ncnn_dir}  ({size_mb:.1f} MB)")
            return True
        else:
            print(f"  [error] Export finished but {ncnn_dir} not found")
            return False
    except Exception as exc:
        print(f"  [error] {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Export YOLO .pt → NCNN")
    parser.add_argument("models", nargs="*", help="Paths to .pt files (default: all in models/)")
    parser.add_argument("--imgsz", type=int, default=320,
                        help="Input image size for export (default: 320)")
    args = parser.parse_args()

    models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

    if args.models:
        pt_files = args.models
    else:
        pt_files = sorted(glob.glob(os.path.join(models_dir, "*.pt")))
        if not pt_files:
            print(f"No .pt files found in {models_dir}")
            sys.exit(1)

    print(f"Found {len(pt_files)} model(s), imgsz={args.imgsz}\n")

    ok, fail = 0, 0
    for pt in pt_files:
        if export_one(pt, args.imgsz):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} exported, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
