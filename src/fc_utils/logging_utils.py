from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime


def setup_logger(
    name: str,
    logs_dir: str | Path = "logs",
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Creates a logger that writes to both:
    1. Terminal
    2. A dated log file inside /logs
    """

    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)

    log_file = logs_path / f"{name}_{datetime.now():%Y-%m-%d}.log"

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Prevent duplicate handlers when script is rerun in same session
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger