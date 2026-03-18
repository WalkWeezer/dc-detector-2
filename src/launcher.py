"""DC-Detector launcher — starts all services as subprocesses.

Usage:  python src/launcher.py [--config path/to/config.yaml]
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_section, project_root  # noqa: E402
from log_config import setup_logging  # noqa: E402

PYTHON = sys.executable
SRC_DIR = os.path.dirname(os.path.abspath(__file__))


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Block until a TCP port is accepting connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _build_services(cfg: dict) -> list[dict]:
    """Return a list of service descriptors to launch."""
    services = []

    # Startup order matters: capture must be ready before detector reads its stream.
    # Light services (mavlink, lora, web) start after the heavy ones are up.
    # wait_for_port: launcher blocks until the previous service opens its port
    # before spawning the next one — avoids all-at-once CPU/RAM spike on boot.

    # 1. Capture — medium load (camera init)
    services.append({
        "name": "capture",
        "script": os.path.join(SRC_DIR, "capture.py"),
        "port": get_section(cfg, "capture").get("port", 8001),
        "wait_for_port": True,   # detector needs this stream
        "port_timeout": 20,
    })

    # 2. Detection — heaviest (loads YOLO/NCNN model into memory)
    services.append({
        "name": "detection",
        "script": os.path.join(SRC_DIR, "detector.py"),
        "port": get_section(cfg, "detection").get("port", 8002),
        "wait_for_port": True,
        "port_timeout": 60,      # NCNN model load can take ~30 s on cold boot
    })

    # 3. MAVLink — light (if enabled)
    if get_section(cfg, "mavlink").get("enabled", True):
        services.append({
            "name": "mavlink",
            "script": os.path.join(SRC_DIR, "mavlink_service.py"),
            "port": get_section(cfg, "mavlink").get("port", 8003),
            "wait_for_port": False,
        })

    # 4. LoRa — light (if enabled); needs mavlink telemetry, but connects lazily
    if get_section(cfg, "lora").get("enabled", True):
        services.append({
            "name": "lora",
            "script": os.path.join(SRC_DIR, "lora_service.py"),
            "port": get_section(cfg, "lora").get("port", 8004),
            "wait_for_port": False,
        })

    # 5. Web UI — last, lightest
    services.append({
        "name": "web",
        "script": os.path.join(SRC_DIR, "web_server.py"),
        "port": get_section(cfg, "web").get("port", 8080),
        "wait_for_port": False,
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
    log.info("Waiting 5 s for OS to settle...")
    log.info("=" * 60)
    time.sleep(5)

    # On Linux set process group so Ctrl+C reaches children; on Windows leave default.
    _is_linux = sys.platform != "win32"
    _popen_kwargs = {}
    if _is_linux:
        # Lower CPU scheduling priority for child processes — reduces boot spike.
        # nice=5 means slightly below normal; detector is the heavy one but we
        # let the OS share CPU rather than starving it completely.
        def _preexec():
            os.nice(5)
        _popen_kwargs["preexec_fn"] = _preexec

    for svc in services:
        cmd = [PYTHON, svc["script"]]
        log.info("  Starting %-12s  port %-5s", svc["name"], svc["port"])
        for attempt in range(3):
            try:
                proc = subprocess.Popen(cmd, env=env, **_popen_kwargs)
                processes[svc["name"]] = proc
                break
            except PermissionError as exc:
                if attempt < 2:
                    log.warning("  PermissionError starting %s, retrying in 2 s (%s)", svc["name"], exc)
                    time.sleep(2)
                else:
                    log.error("  Failed to start %s after 3 attempts: %s", svc["name"], exc)

        # Wait until this service is accepting connections before starting the next.
        # Prevents all-at-once CPU/RAM spike that can cause voltage drop on Pi.
        if svc.get("wait_for_port") and svc.get("port"):
            timeout = svc.get("port_timeout", 30)
            log.info("  Waiting for %s to open port %s (up to %s s)...",
                     svc["name"], svc["port"], timeout)
            if _wait_for_port(svc["port"], timeout=timeout):
                log.info("  %s is ready", svc["name"])
            else:
                log.warning("  %s did not open port %s within %s s — continuing anyway",
                             svc["name"], svc["port"], timeout)

    log.info("-" * 60)
    log.info("All services launched.  Web UI: http://localhost:%s",
             get_section(cfg, "web").get("port", 8080))
    log.info("Press Ctrl+C to stop all services.")
    log.info("-" * 60)

    # Monitor loop: log crashes and attempt one auto-restart per service.
    restart_counts: dict[str, int] = {svc["name"]: 0 for svc in services}
    service_map = {svc["name"]: svc for svc in services}
    MAX_RESTARTS = 3

    try:
        while True:
            for name, proc in list(processes.items()):
                ret = proc.poll()
                if ret is not None:
                    log.warning("Service '%s' exited with code %s", name, ret)
                    del processes[name]
                    if restart_counts[name] < MAX_RESTARTS:
                        restart_counts[name] += 1
                        delay = 2 ** restart_counts[name]  # 2, 4, 8 s backoff
                        log.info("  Restarting '%s' in %s s (attempt %s/%s)...",
                                 name, delay, restart_counts[name], MAX_RESTARTS)
                        time.sleep(delay)
                        svc = service_map[name]
                        try:
                            proc = subprocess.Popen([PYTHON, svc["script"]], env=env, **_popen_kwargs)
                            processes[name] = proc
                        except OSError as exc:
                            log.error("  Failed to restart %s: %s", name, exc)
                    else:
                        log.error("  '%s' crashed %s times, not restarting", name, MAX_RESTARTS)
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
