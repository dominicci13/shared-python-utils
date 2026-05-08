from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime

def setup_logger(
    name: str,
    logs_dir: str | Path = "logs",
    level: int = logging.INFO,
) -> logging.Logger:
    """Create a logger that writes to both the terminal and a dated log file.

    The log file is created inside logs_dir with the name pattern "{name}_YYYY-MM-DD.log".
    Safe to call multiple times — returns the existing logger if already configured.

    Args:
        name (str): Logger name and log file prefix (e.g., "aged_report").
        logs_dir (str | Path, optional): Directory where log files are written. Defaults to "logs".
        level (int, optional): Logging level (e.g., logging.DEBUG, logging.INFO). Defaults to logging.INFO.

    Returns:
        logging.Logger: Configured logger instance.
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