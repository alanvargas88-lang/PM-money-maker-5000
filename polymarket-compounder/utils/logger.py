"""
Dual-output logger: coloured console + persistent file.

Every module imports ``get_logger(__name__)`` to get a child logger
that routes through the root 'compounder' logger.  Trade-specific
messages are also written to ``data/trades.log``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Resolve data directory relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "data"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "trades.log"

_ROOT_LOGGER_NAME = "compounder"
_INITIALISED = False


def _setup_root_logger() -> logging.Logger:
    """Create the root 'compounder' logger with console + file handlers."""

    global _INITIALISED
    if _INITIALISED:
        return logging.getLogger(_ROOT_LOGGER_NAME)

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)

    # ---- Console handler (INFO+) ----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    # ---- File handler (DEBUG+) ----
    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    _INITIALISED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'compounder' namespace.

    Usage::

        from utils.logger import get_logger
        log = get_logger(__name__)
        log.info("Market found: %s", market_id)
    """
    _setup_root_logger()
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
