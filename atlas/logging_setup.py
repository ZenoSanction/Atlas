"""ATLAS logging configuration.

Two handlers:
- Stdout (colour, INFO+) — for console users watching the server
- Rotating file handler under ``data/logs/`` (DEBUG+, all noise)

Third-party loggers (httpx, anthropic, urllib3) clamped to WARNING.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_dir: Path | None = None,
                  to_file: bool = True) -> None:
    """Idempotent logging setup. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers filter

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(name)-22s] %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler (rotating)
    if to_file and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"atlas_{datetime.now():%Y%m%d}.log"
        file_h = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=14,
            encoding="utf-8",
        )
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(fmt)
        root.addHandler(file_h)

    # Quiet noisy third-party libraries
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the `atlas.` namespace."""
    if not name.startswith("atlas."):
        name = f"atlas.{name}"
    return logging.getLogger(name)
