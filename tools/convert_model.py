"""Convert a YOLO .pt model to ONNX (optimised for Raspberry Pi 5).

Usage:
    python tools/convert_model.py models/yolov8n.pt
    python tools/convert_model.py models/yolov8n.pt --imgsz 640 --half
"""

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert YOLO .pt model to ONNX for Raspberry Pi 5 deployment"
    )
    parser.add_argument("model", help="Path to .pt model file")
    parser.add_argument("--imgsz", type=int, default=640, help="Image size (default: 640)")
    parser.add_argument("--half", action="store_true", help="Export FP16 (half precision)")
    parser.add_argument("--simplify", action="store_true", default=True,
                        help="Simplify ONNX graph (default: True)")
    parser.add_argument("--opset", type=int, default=17, help="ONNX opset version (default: 17)")
    args = parser.parse_args()

    if not os.path.isfile(args.model):
        print(f"ERROR: model file not found: {args.model}", file=sys.stderr)
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERROR: ultralytics is not installed.  pip install ultralytics", file=sys.stderr)
        sys.exit(1)

    print(f"Loading model: {args.model}")
    model = YOLO(args.model)

    print(f"Exporting to ONNX  (imgsz={args.imgsz}, half={args.half}, opset={args.opset})")
    out = model.export(
        format="onnx",
        imgsz=args.imgsz,
        half=args.half,
        simplify=args.simplify,
        opset=args.opset,
    )
    print(f"Done.  ONNX model saved to: {out}")


if __name__ == "__main__":
    main()
