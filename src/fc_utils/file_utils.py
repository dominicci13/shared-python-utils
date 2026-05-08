from __future__ import annotations

import os
import time
from pathlib import Path

from rich import print


def create_dir_structure(base: str, folders: list[str]) -> None:
    """Create a set of subdirectories under a base path.

    Skips any directory that already exists.

    Args:
        base (str): Root directory under which all folders will be created.
        folders (list[str]): Subdirectory names or relative paths to create
            (e.g., ["logs", "output/reports"]).

    Raises:
        OSError: If a directory cannot be created due to permissions or an invalid path.
    """
    for folder in folders:
        path = Path(base) / folder
        path.mkdir(parents=True, exist_ok=True)
        print(f"[cyan][INFO][/cyan] Directory ready: [cyan]{path}[/cyan]")


def wait_for_download(directory: str, extension: str = ".csv", timeout_sec: int = 60) -> str:
    """Poll a directory until a completed download file appears and return its path.

    Checks every 2 seconds for a file with the given extension that is not a
    browser in-progress file (.crdownload or .part).

    Args:
        directory (str): Folder to watch for the downloaded file.
        extension (str): File extension to wait for (e.g., ".csv", ".xlsx"). Defaults to ".csv".
        timeout_sec (int): Maximum seconds to wait before raising a TimeoutError. Defaults to 60.

    Returns:
        str: Absolute path to the completed download file.

    Raises:
        TimeoutError: If no completed file with the given extension appears within `timeout_sec`.
    """
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        for entry in os.scandir(directory):
            if (
                entry.name.endswith(extension)
                and not entry.name.endswith(".crdownload")
                and not entry.name.endswith(".part")
            ):
                print(f"[green][SUCCESS][/green] Download complete: [cyan]{entry.name}[/cyan]")
                return entry.path
        time.sleep(2)

    raise TimeoutError(f"No '{extension}' file appeared in '{directory}' within {timeout_sec}s.")


def clear_directory(directory: str, extension: str | None = None) -> None:
    """Delete files in a directory, optionally filtered by extension.

    Only removes files, not subdirectories.

    Args:
        directory (str): Path to the directory to clear.
        extension (str | None): If provided, only files with this extension are deleted
            (e.g., ".csv"). Pass None to delete all files. Defaults to None.

    Raises:
        FileNotFoundError: If `directory` does not exist.
        OSError: If a file cannot be deleted.
    """
    deleted = 0
    for entry in os.scandir(directory):
        if entry.is_file():
            if extension is None or entry.name.endswith(extension):
                os.remove(entry.path)
                deleted += 1

    print(f"[green][SUCCESS][/green] Cleared [bold]{deleted}[/bold] file(s) from [cyan]{directory}[/cyan].")
