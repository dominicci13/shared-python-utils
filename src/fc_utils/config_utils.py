from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

def load_env(env_path: str | Path = ".env") -> None:
    """Load environment variables from a .env file into os.environ.

    Skips blank lines and lines beginning with '#'. If the file does not exist,
    the function returns silently without raising an error.

    Args:
        env_path (str | Path, optional): Path to the .env file. Defaults to ".env".
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
    """Retrieve an environment variable, with optional default and required enforcement.

    Args:
        key (str): Name of the environment variable.
        default (Any, optional): Value to return if the variable is not set. Defaults to None.
        required (bool, optional): If True, raises ValueError when the variable is missing. Defaults to False.

    Returns:
        Any: The value of the environment variable, or default if not set.
    """
    value = os.getenv(key, default)

    if required and value is None:
        raise ValueError(f"Missing required environment variable: {key}")

    return value

def load_config(config_path: str | Path = "config/config.json") -> dict:
    """Load and parse a JSON configuration file.

    Args:
        config_path (str | Path, optional): Path to the JSON file. Defaults to "config/config.json".

    Returns:
        dict: Parsed contents of the config file.

    Raises:
        FileNotFoundError: If the config file does not exist at the given path.
    """
    config_file = Path(config_path)

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    return json.loads(config_file.read_text(encoding="utf-8"))

def load_config_safe(config_path: str | Path = "config/config.json") -> dict:
    """Load and parse a JSON configuration file, returning an empty dict if the file is missing.

    Useful for optional config files where a missing file is an acceptable default state.

    Args:
        config_path (str | Path, optional): Path to the JSON file. Defaults to "config/config.json".

    Returns:
        dict: Parsed contents of the config file, or an empty dict if the file does not exist.
    """
    try:
        return load_config(config_path)
    except FileNotFoundError:
        return {}