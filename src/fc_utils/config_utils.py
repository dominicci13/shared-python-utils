from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any


def load_env(env_path: str | Path = ".env") -> None:
    """
    Loads environment variables from a .env file into os.environ
    """
    env_file = Path(env_path)

    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def get_env(key: str, default: Any = None, required: bool = False) -> Any:
    """
    Safely get environment variables
    """
    value = os.getenv(key, default)

    if required and value is None:
        raise ValueError(f"Missing required environment variable: {key}")

    return value


def load_config(config_path: str | Path = "config/config.json") -> dict:
    """
    Load JSON config file
    """
    config_file = Path(config_path)

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    return json.loads(config_file.read_text(encoding="utf-8"))