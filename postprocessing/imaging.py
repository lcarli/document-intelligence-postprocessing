from __future__ import annotations

import io
import math
import threading
from collections.abc import Generator, Iterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from postprocessing.models import ProcessingError, ProcessorSettings

RGB = npt.NDArray[np.uint8]
PDF_DPI = 300
_PDFIUM_LOCK = threading.Lock()


def _pixels(width: float, height: float, settings: ProcessorSettings) -> None:
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or min(width, height) < 1
        or math.ceil(width) * math.ceil(height) > settings.max_page_pixels
    ):
        raise ProcessingError("page_pixel_limit")


def raster_pages(
    data: bytes, numbers: set[int], settings: ProcessorSettings
) -> Generator[tuple[int, RGB, str], None, None]:
    if len(data) > settings.max_document_bytes:
        raise ProcessingError("document_too_large")
    if data.lstrip()[:5] == b"%PDF-":
        yield from _pdf_pages(data, numbers, settings)
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (
                image.format not in ("PNG", "JPEG")
                or getattr(image, "n_frames", 1) != 1
                or image.getexif().get(274, 1) != 1
            ):
                raise ProcessingError("unsupported_image_format_or_orientation")
            _pixels(*image.size, settings)
            if numbers - {1}:
                raise ProcessingError("image_page_reference_mismatch")
            if 1 in numbers:
                with image.convert("RGBA") as rgba, Image.new("RGB", image.size, "white") as rgb:
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                    yield 1, np.array(rgb), "pixel"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ProcessingError("invalid_image_document") from None


def _pdf_pages(
    data: bytes, numbers: set[int], settings: ProcessorSettings
) -> Iterator[tuple[int, RGB, str]]:
    try:
        with _PDFIUM_LOCK:
            document = pdfium.PdfDocument(data)
        try:
            with _PDFIUM_LOCK:
                count = len(document)
                if count > settings.max_pages:
                    raise ProcessingError("document_page_limit")
                if any(number < 1 or number > count for number in numbers):
                    raise ProcessingError("pdf_page_reference_mismatch")
            for number in sorted(numbers):
                with _PDFIUM_LOCK:
                    page = document[number - 1]
                    try:
                        width, height = page.get_size()
                        _pixels(width * PDF_DPI / 72, height * PDF_DPI / 72, settings)
                        bitmap = page.render(scale=PDF_DPI / 72, fill_color=(255, 255, 255, 255))
                        try:
                            with bitmap.to_pil() as rendered, rendered.convert("RGB") as rgb:
                                pixels = np.array(rgb)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                yield number, pixels, "inch"
        finally:
            with _PDFIUM_LOCK:
                document.close()
    except (pdfium.PdfiumError, ValueError, OSError):
        raise ProcessingError("invalid_pdf_document") from None


def bounds(
    polygon: Any, page: dict[str, Any], pixels: RGB, expected_unit: str
) -> tuple[int, int, int, int]:
    unit = page.get("unit")
    width, height = page.get("width"), page.get("height")
    if (
        unit != expected_unit
        or not isinstance(width, (float, int))
        or not isinstance(height, (float, int))
        or isinstance(width, bool)
        or isinstance(height, bool)
        or not (0 < width <= 1_000_000 and 0 < height <= 1_000_000)
        or not math.isfinite(width)
        or not math.isfinite(height)
        or min(width, height) <= 0
    ):
        raise ProcessingError("page_unit_or_dimensions_invalid")
    factor = PDF_DPI if unit == "inch" else 1
    image_height, image_width = pixels.shape[:2]
    if abs(width * factor - image_width) > max(2, image_width * 0.02) or abs(
        height * factor - image_height
    ) > max(2, image_height * 0.02):
        raise ProcessingError("page_raster_dimensions_mismatch")
    if (
        not isinstance(polygon, list)
        or len(polygon) < 6
        or len(polygon) % 2
        or any(
            type(value) not in (float, int)
            or not -1_000_000 <= value <= 1_000_000
            or not math.isfinite(value)
            for value in polygon
        )
    ):
        raise ProcessingError("invalid_polygon")
    xs, ys = polygon[::2], polygon[1::2]
    if min(xs) < 0 or min(ys) < 0 or max(xs) > width or max(ys) > height:
        raise ProcessingError("polygon_outside_page")
    x0 = max(0, math.floor(min(xs) * factor))
    y0 = max(0, math.floor(min(ys) * factor))
    x1 = min(image_width, math.ceil(max(xs) * factor))
    y1 = min(image_height, math.ceil(max(ys) * factor))
    if x1 - x0 < 3 or y1 - y0 < 3:
        raise ProcessingError("crop_too_small")
    return x0, y0, x1, y1
