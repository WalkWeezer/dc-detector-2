"""DC-Detector launcher — starts all services as subprocesses.

Usage:  python src/launcher.py [--config path/to/config.yaml]
"""

import argparse
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section, project_root  # noqa: E402
from log_config import setup_logging  # noqa: E402

PYTHON = sys.executable
SRC_DIR = os.path.dirname(os.path.abspath(__file__))


def _build_services(cfg: dict) -> list[dict]:
    """Return a list of service descriptors to launch."""
    services = []

    # 1. Capture (always)
    services.append({
        "name": "capture",
        "script": os.path.join(SRC_DIR, "capture.py"),
        "port": get_section(cfg, "capture").get("port", 8001),
    })

    # 2. Detection (always — will log an error if model missing)
    services.append({
        "name": "detection",
        "script": os.path.join(SRC_DIR, "detector.py"),
        "port": get_section(cfg, "detection").get("port", 8002),
    })

    # 3. MAVLink (if enabled)
    if get_section(cfg, "mavlink").get("enabled", True):
        services.append({
            "name": "mavlink",
            "script": os.path.join(SRC_DIR, "mavlink_service.py"),
            "port": get_section(cfg, "mavlink").get("port", 8003),
        })

    # 4. LoRa (if enabled)
    if get_section(cfg, "lora").get("enabled", True):
        services.append({
            "name": "lora",
            "script": os.path.join(SRC_DIR, "lora_service.py"),
            "port": get_section(cfg, "lora").get("port", 8004),
        })

    # 5. Web UI (always, last)
    services.append({
        "name": "web",
        "script": os.path.join(SRC_DIR, "web_server.py"),
        "port": get_section(cfg, "web").get("port", 8080),
    })

    return services


def main() -> None:
    parser = argparse.ArgumentParser(description="DC-Detector launcher")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logging("launcher", cfg)

    # Ensure PYTHONPATH includes src/
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + existing if existing else "")

    if args.config:
        env["DC_CONFIG"] = os.path.abspath(args.config)

    services = _build_services(cfg)
    processes: dict[str, subprocess.Popen] = {}

    log.info("=" * 60)
    log.info("DC-Detector v0.2 — starting %d services", len(services))
    log.info("=" * 60)

    for svc in services:
        cmd = [PYTHON, svc["script"]]
        log.info("  Starting %-12s  port %-5s  %s", svc["name"], svc["port"], svc["script"])
        proc = subprocess.Popen(cmd, env=env)
        processes[svc["name"]] = proc
        time.sleep(0.5)  # stagger startup

    log.info("-" * 60)
    log.info("All services launched.  Web UI: http://localhost:%s",
             get_section(cfg, "web").get("port", 8080))
    log.info("Press Ctrl+C to stop all services.")
    log.info("-" * 60)

    # Wait for any process to exit or Ctrl+C
    try:
        while True:
            for name, proc in list(processes.items()):
                ret = proc.poll()
                if ret is not None:
                    log.warning("Service '%s' exited with code %s", name, ret)
                    del processes[name]
            if not processes:
                log.info("All services have stopped")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Shutting down all services...")

    # Terminate remaining
    for name, proc in processes.items():
        log.info("  Stopping %s (pid %d)", name, proc.pid)
        try:
            proc.terminate()
        except OSError:
            pass

    # Wait for graceful exit
    for name, proc in processes.items():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("  Force-killing %s", name)
            proc.kill()

    log.info("DC-Detector stopped.")


if __name__ == "__main__":
    main()
