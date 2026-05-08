from __future__ import annotations

import os
import tempfile

from PIL import Image, ImageGrab
from rich import print

from fc_utils.excel_utils import paste_image_to_sheet

_TEMP_CROPPED = os.path.join(tempfile.gettempdir(), "fc_screenshot_cropped.png")


def crop_to_element(element: object) -> str:
    """Take a full-screen capture and crop it to a Selenium WebElement's bounding box.

    The caller is responsible for deleting the returned file after use.

    Args:
        element (object): The WebElement whose bounding box defines the crop region.

    Returns:
        str: Absolute path to the temporary cropped image file.

    Raises:
        OSError: If the temporary file cannot be written.
    """
    location = element.location
    size = element.size
    left = location["x"]
    top = location["y"]
    right = left + size["width"]
    bottom = top + size["height"]

    ImageGrab.grab().crop((left, top, right, bottom)).save(_TEMP_CROPPED)
    return _TEMP_CROPPED


def crop_to_box(image_path: str, left: int, top: int, right: int, bottom: int) -> str:
    """Crop an existing image file to the specified pixel box.

    The caller is responsible for deleting the returned file after use.

    Args:
        image_path (str): Absolute path to the source image file.
        left (int): Left edge of the crop box in pixels.
        top (int): Top edge of the crop box in pixels.
        right (int): Right edge of the crop box in pixels.
        bottom (int): Bottom edge of the crop box in pixels.

    Returns:
        str: Absolute path to the temporary cropped image file.

    Raises:
        FileNotFoundError: If the source image does not exist at `image_path`.
        OSError: If the temporary file cannot be written.
    """
    Image.open(image_path).crop((left, top, right, bottom)).save(_TEMP_CROPPED)
    return _TEMP_CROPPED


def paste_to_excel(workbook_path: str, sheet: str | int, cell: str, image_path: str) -> None:
    """Insert a temporary image into an Excel workbook, then delete the image file.

    Inserts the image anchored at `cell`, saves the workbook, then removes the
    temporary image file regardless of whether the insert succeeded.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        sheet (str | int): Sheet name (str) or index (int, zero-based) to insert into.
        cell (str): Cell address to anchor the image's top-left corner (e.g., "B5").
        image_path (str): Absolute path to the temporary image file to insert.

    Raises:
        FileNotFoundError: If the workbook does not exist.
        xlwings.XlwingsError: If the sheet cannot be found or the image fails to insert.
    """
    try:
        paste_image_to_sheet(workbook_path, sheet, cell, image_path)
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)
            print(f"[cyan][INFO][/cyan] Temporary image deleted: [cyan]{image_path}[/cyan]")
