"""UART diagnostic tool — check available serial ports and test connectivity.

Usage:
    python tools/check_uart.py
    python tools/check_uart.py --port /dev/ttyAMA0 --baud 115200 --listen 5
"""

import argparse
import sys
import time


def list_ports():
    """List all available serial ports."""
    try:
        import serial.tools.list_ports
    except ImportError:
        print("ERROR: pyserial not installed.  pip install pyserial")
        return []

    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        print()
        print("Checklist:")
        print("  - Is the device physically connected?")
        print("  - Is the UART enabled in /boot/firmware/config.txt?")
        print("    Add: dtparam=uart0=on")
        print("    For 2nd UART: dtoverlay=uart2-pi5")
        print("  - Is the USB cable a data cable (not charge-only)?")
        print("  - Are drivers installed (CH9102/CP2102 for Heltec)?")
        return []

    print(f"Found {len(ports)} serial port(s):\n")
    for p in ports:
        print(f"  Device:       {p.device}")
        print(f"  Description:  {p.description}")
        print(f"  Hardware ID:  {p.hwid}")
        print(f"  Manufacturer: {p.manufacturer or '—'}")
        print(f"  Product:      {p.product or '—'}")
        print()
    return ports


def listen_port(device: str, baudrate: int, duration: int):
    """Open a serial port and print received data for *duration* seconds."""
    try:
        import serial
    except ImportError:
        print("ERROR: pyserial not installed.")
        return

    print(f"Opening {device} @ {baudrate} baud  (listening for {duration} s)...")
    try:
        ser = serial.Serial(device, baudrate, timeout=1)
    except Exception as exc:
        print(f"ERROR: cannot open {device}: {exc}")
        print()
        print("Possible fixes:")
        print(f"  - Check that {device} exists:  ls -la {device}")
        print(f"  - Check permissions:  sudo chmod 666 {device}")
        print(f"  - Add user to dialout:  sudo usermod -aG dialout $USER")
        print(f"  - Check if port is busy:  sudo fuser {device}")
        return

    print(f"Connected.  Waiting for data...\n")
    start = time.time()
    total_bytes = 0
    lines = 0
    while time.time() - start < duration:
        if ser.in_waiting > 0:
            data = ser.readline()
            total_bytes += len(data)
            lines += 1
            text = data.decode("utf-8", errors="replace").strip()
            elapsed = time.time() - start
            print(f"  [{elapsed:6.2f}s]  {text}")
        else:
            time.sleep(0.05)

    ser.close()
    print(f"\nDone.  Received {total_bytes} bytes, {lines} lines in {duration} s.")
    if total_bytes == 0:
        print()
        print("No data received. Check:")
        print("  - Wiring: TX↔RX crossed? GND connected?")
        print("  - Baud rate matches the device?")
        print("  - Device is powered and transmitting?")


def main():
    parser = argparse.ArgumentParser(description="UART diagnostic tool")
    parser.add_argument("--port", "-p", default=None,
                        help="Serial port to test (e.g. /dev/ttyAMA0, COM3)")
    parser.add_argument("--baud", "-b", type=int, default=115200,
                        help="Baud rate (default: 115200)")
    parser.add_argument("--listen", "-l", type=int, default=5,
                        help="Listen duration in seconds (default: 5)")
    args = parser.parse_args()

    print("=" * 50)
    print(" DC-Detector — UART Diagnostic")
    print("=" * 50)
    print()

    ports = list_ports()

    if args.port:
        print("-" * 50)
        listen_port(args.port, args.baud, args.listen)
    elif ports:
        print("-" * 50)
        print(f"Tip: test a port with:")
        print(f"  python tools/check_uart.py --port {ports[0].device} --baud 115200 --listen 5")


if __name__ == "__main__":
    main()
