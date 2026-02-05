# DC-Detector v0.2

Real-time object detection system for Raspberry Pi 5 with MAVLink telemetry, LoRa communication, video recording, and a web dashboard.

## Requirements

- **Python 3.10+**
- USB or CSI camera (or a video file for development)
- (Optional) Flight controller connected via UART (MAVLink 2)
- (Optional) Heltec LoRa32 module via USB or UART

## Quick start — Windows

```
launch.bat
```

The script creates a virtual environment, installs dependencies, copies the example config, and starts all services. Open `http://localhost:8080` in a browser.

For development without a camera, set `source: "file"` and `video_file` in `config.yaml`.

## Quick start — Raspberry Pi 5

```bash
chmod +x launch.sh
./launch.sh
```

### Wi-Fi Access Point (optional)

To make the Pi broadcast its own Wi-Fi network:

```bash
sudo bash scripts/setup_ap.sh
```

After that, connect to **DC-Detector** Wi-Fi and open `http://192.168.4.1:8080`.

## Project structure

```
├── src/
│   ├── config.py            # YAML config loader
│   ├── log_config.py        # Logging with rotation
│   ├── capture.py           # Video capture, MJPEG stream, recording
│   ├── detector.py          # YOLO detection & tracking
│   ├── mavlink_service.py   # MAVLink 2 UART telemetry
│   ├── lora_service.py      # LoRa / ESP32 UART
│   ├── web_server.py        # Web UI server
│   └── launcher.py          # Starts all services
├── web/
│   └── index.html           # Dashboard (all endpoints & WebSocket demos)
├── tools/
│   └── convert_model.py     # Convert .pt → ONNX
├── scripts/
│   └── setup_ap.sh          # Wi-Fi AP setup (Raspberry Pi)
├── firmware/                # ESP32 LoRa firmware (PlatformIO)
├── docs/
│   └── ARCHITECTURE.md      # Hardware pinout & architecture reference
├── config.example.yaml      # Example configuration
├── requirements.txt         # Python dependencies
├── launch.bat               # Windows launcher
├── launch.sh                # Linux / RPi launcher
└── .gitignore
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit as needed. Key settings:

| Section | Key | Description |
|---------|-----|-------------|
| `capture.source` | `auto` / `csi` / `usb` / `file` | Video source |
| `capture.video_file` | path | Video file for `file` mode |
| `capture.width`, `height`, `fps` | int | Capture resolution and frame rate |
| `detection.model_path` | path | YOLO model (`.pt` or `.onnx`) |
| `detection.confidence` | float | Detection confidence threshold |
| `mavlink.device` | `/dev/ttyAMA0` or `COM3` | MAVLink UART device |
| `mavlink.baudrate` | int | UART baud rate (57600 / 115200) |
| `lora.device` | `/dev/ttyUSB0` or `COM4` | LoRa serial device |
| `servo.servo1_pin` | 18 | GPIO pin for servo 1 (PWM) |
| `servo.servo2_pin` | 12 | GPIO pin for servo 2 (PWM) |

## Services & ports

| Service | Default port | Description |
|---------|-------------|-------------|
| Capture | 8001 | MJPEG stream, recording API |
| Detection | 8002 | YOLO tracks, annotated stream, results |
| MAVLink | 8003 | Telemetry from flight controller |
| LoRa | 8004 | LoRa / ESP32 message exchange |
| Web UI | 8080 | Dashboard with endpoint demos |

## API overview

### Capture (`:8001`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/stream` | MJPEG video stream |
| GET | `/frame` | Single JPEG frame |
| POST | `/recording/start` | Start recording |
| POST | `/recording/stop` | Stop recording |
| GET | `/recordings` | List recorded videos |
| GET | `/recordings/{name}` | Download a recording |
| POST | `/playback` | Play a recorded file `{"filename":"..."}` |
| POST | `/playback/stop` | Return to live camera |
| WS | `/ws` | Status push (recording state) |

### Detection (`:8002`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/tracks` | Active tracked objects |
| GET | `/detections` | Session detection log |
| GET | `/media/{path}` | Detection JPEG / GIF |
| GET | `/stream` | Annotated MJPEG (bounding boxes) |
| WS | `/ws` | Real-time tracks push |

### MAVLink (`:8003`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/telemetry` | Latest telemetry snapshot |
| GET | `/status` | Connection info |
| GET | `/messages` | Recent MAVLink messages |
| POST | `/command` | Send command `{"msg_type":"...","params":{}}` |
| WS | `/ws` | Real-time telemetry push |

### LoRa (`:8004`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/status` | Connection info |
| GET | `/messages` | Received messages |
| POST | `/send` | Send text `{"text":"..."}` |
| WS | `/ws` | Real-time messages push |

## YOLO model

Place your model file (e.g. `yolov8n.pt`) into the `models/` directory.

To convert to ONNX for better Raspberry Pi performance:

```bash
python tools/convert_model.py models/yolov8n.pt --imgsz 640
```

## Hardware pinout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full wiring diagrams. Summary:

- **MAVLink UART**: Pin 8 (TX) → FC RX, Pin 10 (RX) ← FC TX, Pin 9 (GND)
- **LoRa**: USB (`/dev/ttyUSB0`) or UART on pins 27/28
- **Servo 1**: GPIO18 (Pin 12, PWM)
- **Servo 2**: GPIO12 (Pin 32, PWM)

## Logs

Logs are written to the `logs/` directory with automatic rotation (10 MB per file, 5 backups). All log messages are in English.
