from __future__ import annotations

import copy
import io
from typing import Any

import pytest
from PIL import Image

from postprocessing.analysis import observations, page_map, parse_analysis
from postprocessing.imaging import raster_pages
from postprocessing.models import ProcessingError, ProcessorSettings


@pytest.mark.parametrize("data", [b"{", b"[]", b"\xff", b'{"value": NaN}'])
def test_invalid_json(data: bytes) -> None:
    with pytest.raises(ProcessingError):
        parse_analysis(data)


@pytest.mark.parametrize(
    "pages",
    [None, [], [1], [{"pageNumber": 0}], [{"pageNumber": 1}, {"pageNumber": 1}]],
)
def test_invalid_pages(pages: Any) -> None:
    with pytest.raises(ProcessingError):
        page_map({"pages": pages}, ProcessorSettings())


@pytest.mark.parametrize("change", [{"selectionMarks": {}}, {"selectionMarks": [True]}])
def test_invalid_marks(synthetic: tuple[bytes, dict[str, Any]], change: dict[str, Any]) -> None:
    synthetic[1]["pages"][0].update(change)
    with pytest.raises(ProcessingError):
        observations(synthetic[1], ProcessorSettings())


def test_bad_custom_shapes_are_not_silently_skipped(
    synthetic: tuple[bytes, dict[str, Any]],
) -> None:
    for field in (
        "not-a-field",
        {"type": "object", "valueObject": []},
        {"type": "array", "valueArray": {}},
    ):
        analysis = copy.deepcopy(synthetic[1])
        analysis["documents"] = [{"fields": {"test": field}}]
        with pytest.raises(ProcessingError):
            observations(analysis, ProcessorSettings())


def test_out_of_range_page_is_reviewed(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    analysis = synthetic[1]
    analysis["documents"] = [
        {
            "fields": {
                "test": {
                    "type": "selectionMark",
                    "valueSelectionMark": "selected",
                    "boundingRegions": [{"pageNumber": 99, "polygon": [0, 0, 1, 0, 1, 1]}],
                }
            }
        }
    ]
    assert (
        observations(analysis, ProcessorSettings())[-1].geometry_error == "invalid_page_reference"
    )


def test_image_limits(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    with pytest.raises(ProcessingError, match="image_page_reference_mismatch"):
        list(raster_pages(synthetic[0], {2}, ProcessorSettings()))
    with pytest.raises(ProcessingError, match="document_too_large"):
        list(raster_pages(b"x" * 1025, {1}, ProcessorSettings(max_document_bytes=1024)))
    stream = io.BytesIO()
    with Image.new("RGB", (1100, 1100)) as image:
        image.save(stream, format="PNG")
    with pytest.raises(ProcessingError, match="page_pixel_limit"):
        list(raster_pages(stream.getvalue(), {1}, ProcessorSettings(max_page_pixels=1_000_000)))


def test_invalid_pdf() -> None:
    with pytest.raises(ProcessingError, match="invalid_pdf_document"):
        list(raster_pages(b"%PDF-invalid", {1}, ProcessorSettings()))


def test_transparent_background_is_composited_on_white() -> None:
    stream = io.BytesIO()
    with Image.new("RGBA", (100, 100), (0, 0, 0, 0)) as image:
        image.save(stream, format="PNG")
    _, pixels, unit = next(raster_pages(stream.getvalue(), {1}, ProcessorSettings()))
    assert unit == "pixel"
    assert pixels.min() == 255
