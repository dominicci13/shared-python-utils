from __future__ import annotations

import time
from pathlib import Path

import xlwings as xw
import logging

log = logging.getLogger(__name__)
def refresh_workbook(workbook_path: str, macro_name: str = "Module1.Refresh", wait: int = 30) -> None:
    """Open an Excel workbook, run a refresh macro, and save it.

    Opens the workbook in a hidden Excel instance, executes the specified macro,
    waits for it to complete (useful for async Power Query refreshes), then saves
    and closes cleanly.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        macro_name (str): Full macro name to execute (e.g., "Module1.Refresh").
            Defaults to "Module1.Refresh".
        wait (int): Seconds to wait after running the macro before saving.
            Defaults to 30.

    Raises:
        FileNotFoundError: If the workbook does not exist at the given path.
        xlwings.XlwingsError: If the macro cannot be found or fails to run.
    """
    log.info(f"Opening workbook: [cyan]{Path(workbook_path).name}[/cyan]")

    with xw.App(visible=False) as excel:
        excel.display_alerts = False
        excel.screen_updating = False

        wb = excel.books.open(workbook_path)

        log.info(f"Running macro: [cyan]{macro_name}[/cyan]")
        wb.macro(macro_name)()
        time.sleep(wait)

        wb.save()
        wb.close()

        excel.display_alerts = True
        excel.screen_updating = True

    log.success("Workbook refreshed and saved successfully.")


def run_macro(workbook_path: str, macro_name: str) -> None:
    """Open an Excel workbook, run a named macro, and save it.

    Intended for synchronous macros that complete immediately (e.g., formatting,
    chart resizing). For macros that trigger async data refreshes, use
    refresh_workbook() which includes a configurable wait period.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        macro_name (str): Full macro name to execute (e.g., "Module1.FormatSheet").

    Raises:
        FileNotFoundError: If the workbook does not exist at the given path.
        xlwings.XlwingsError: If the macro cannot be found or fails to run.
    """
    log.info(f"Running macro [cyan]{macro_name}[/cyan] in [cyan]{Path(workbook_path).name}[/cyan]")

    with xw.App(visible=False) as excel:
        excel.display_alerts = False
        excel.screen_updating = False

        wb = excel.books.open(workbook_path)
        wb.macro(macro_name)()
        wb.save()
        wb.close()

        excel.display_alerts = True
        excel.screen_updating = True

    log.success(f"Macro [cyan]{macro_name}[/cyan] completed successfully.")


def paste_image_to_sheet(workbook_path: str, sheet: str | int, cell: str, image_path: str) -> None:
    """Open an Excel workbook and insert an image anchored at a given cell.

    Opens the workbook in a hidden Excel instance, inserts the image with its
    top-left corner aligned to the target cell, then saves and closes.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        sheet (str | int): Sheet name (str) or index (int, zero-based) to insert into.
        cell (str): Cell address to anchor the image's top-left corner (e.g., "B5").
        image_path (str): Absolute path to the image file to insert.

    Raises:
        FileNotFoundError: If the workbook or image file does not exist.
        xlwings.XlwingsError: If the sheet cannot be found or the image fails to insert.
    """
    log.info(f"Inserting image into [cyan]{Path(workbook_path).name}[/cyan] at {cell}.")

    with xw.App(visible=False) as excel:
        excel.display_alerts = False
        excel.screen_updating = False

        wb = excel.books.open(workbook_path)
        ws = wb.sheets[sheet]
        ws.pictures.add(image_path, left=ws.range(cell).left, top=ws.range(cell).top)
        wb.save()
        wb.close()

        excel.display_alerts = True
        excel.screen_updating = True

    log.success(f"Image inserted at [cyan]{cell}[/cyan] successfully.")
