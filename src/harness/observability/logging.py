"""Structured logging setup.

Writes to stdout (console) and an optional rotating file. All logger names
under ``harness.*`` honour the configured level.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure the ``harness`` logger tree once (idempotent)."""
    root = logging.getLogger("harness")
    if root.handlers:  # already configured
        return

    root.setLevel(level.upper())

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        file_handler = RotatingFileHandler(
            Path(log_file),
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Keep third-party loggers quiet unless explicitly configured.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``harness`` namespace."""
    if not name.startswith("harness"):
        name = f"harness.{name}"
    return logging.getLogger(name)
