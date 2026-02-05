"""Shared logging configuration with rotation for DC-Detector services."""

import os
import logging
from logging.handlers import RotatingFileHandler

_CONFIGURED = False


def setup_logging(service_name: str, cfg: dict) -> logging.Logger:
    """Create (or return) a logger for *service_name* with file rotation."""
    global _CONFIGURED

    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_dir = log_cfg.get("directory", "./logs")
    max_bytes = int(log_cfg.get("max_bytes", 10 * 1024 * 1024))
    backup_count = int(log_cfg.get("backup_count", 5))

    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(service_name)
    logger.setLevel(level)

    if not _CONFIGURED:
        fmt = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Rotating file handler
        fh = RotatingFileHandler(
            os.path.join(log_dir, f"{service_name}.log"),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        _CONFIGURED = True

    return logger
