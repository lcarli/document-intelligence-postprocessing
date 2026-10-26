from __future__ import annotations

import copy
import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from postprocessing.analysis import observations
from postprocessing.models import GrayscaleOptions, ProcessingError, ProcessorSettings
from postprocessing.processors.grayscale import (
    GrayscaleCheckboxProcessor,
    classify,
    score_crop,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, "selected"),
        (89, "selected"),
        (90, "needs_review"),
        (150, "needs_review"),
        (151, "unselected"),
        (255, "unselected"),
    ],
)
def test_strict_thresholds(score: int, expected: str) -> None:
    assert classify(score, GrayscaleOptions()) == expected


def test_score_core_ignores_border() -> None:
    crop = np.full((50, 50, 3), 190, dtype=np.uint8)
    crop[:2] = crop[-2:] = crop[:, :2] = crop[:, -2:] = 0
    score, sigma = score_crop(crop)
    assert score > 150
    assert sigma == pytest.approx(3.5)
    with pytest.raises(ProcessingError, match="crop_too_small"):
        score_crop(crop[:1])


def test_synthetic_shadow_preserves_raw(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    document, analysis = synthetic
    before = copy.deepcopy(analysis)
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    assert [item.decision for item in report.observations] == [
        "unselected",
        "selected",
        "needs_review",
    ]
    assert [item.effective_state for item in report.observations] == ["selected"] * 3
    assert all(item.original_confidence == 0.95 for item in report.observations)
    assert [item.disagrees_with_di for item in report.observations] == [True, False, False]
    assert report.requires_review
    assert analysis == before
    assert report.timings_ms["total_compute"] >= report.timings_ms["roi"]


def test_enforce_explicitly_abstains(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    report = GrayscaleCheckboxProcessor().process(*synthetic, ProcessorSettings(mode="enforce"))
    assert [item.effective_state for item in report.observations] == [
        "unselected",
        "selected",
        None,
    ]


def test_off_never_opens_document(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    report = GrayscaleCheckboxProcessor().process(
        b"not a document", synthetic[1], ProcessorSettings(mode="off")
    )
    assert report.status == "off"
    assert all(item.decision == "not_run" for item in report.observations)
    assert not report.requires_review


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"polygon": None}, "invalid_polygon"),
        ({"polygon": [10, 10, 11, 10, 11, 11, 10, 11]}, "crop_too_small"),
        ({"polygon": [-1, 20, 60, 20, 60, 70, -1, 70]}, "polygon_outside_page"),
        ({"polygon": [10, 20, float("nan"), 20, 60, 70, 10, 70]}, "invalid_polygon"),
        ({"state": "unknown"}, "invalid_original_state"),
    ],
)
def test_invalid_mark_requires_review(
    synthetic: tuple[bytes, dict[str, Any]], change: dict[str, Any], reason: str
) -> None:
    document, analysis = synthetic
    analysis["pages"][0]["selectionMarks"][0].update(change)
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    assert report.observations[0].decision == "needs_review"
    assert report.observations[0].reason == reason


def test_raster_mismatch_does_not_guess(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    document, analysis = synthetic
    analysis["pages"][0]["width"] = 1000
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    assert all(item.reason == "page_raster_dimensions_mismatch" for item in report.observations)


def test_nested_fields_and_missing_geometry(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    document, analysis = synthetic
    analysis["documents"] = [
        {
            "fields": {
                "group/one": {
                    "type": "array",
                    "valueArray": [
                        {
                            "type": "object",
                            "valueObject": {
                                "tick": {
                                    "type": "selectionMark",
                                    "valueSelectionMark": "selected",
                                    "boundingRegions": [],
                                }
                            },
                        }
                    ],
                }
            }
        }
    ]
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    field = report.observations[-1]
    assert field.scope == "field"
    assert "/group~1one/valueArray/0/valueObject/tick" in field.observation_id
    assert field.reason == "missing_or_ambiguous_geometry"
    assert field.effective_state == "selected"


def test_no_detections_are_not_empty_boxes(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    document, analysis = synthetic
    analysis["pages"][0]["selectionMarks"] = []
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    assert not report.observations
    assert report.requires_review
    assert report.findings[0].code == "no_selection_marks_returned"


def test_observation_limit(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    with pytest.raises(ProcessingError, match="too_many_selection_marks"):
        observations(synthetic[1], ProcessorSettings(max_observations=2))


def test_pdf_renders_inches_at_300_dpi() -> None:
    with Image.new("RGB", (300, 300), (180, 180, 180)) as image:
        stream = io.BytesIO()
        image.save(stream, format="PDF", resolution=300)
    analysis = {
        "pages": [
            {
                "pageNumber": 1,
                "width": 1,
                "height": 1,
                "unit": "inch",
                "selectionMarks": [
                    {"state": "selected", "polygon": [0.2, 0.2, 0.8, 0.2, 0.8, 0.8, 0.2, 0.8]}
                ],
            }
        ]
    }
    report = GrayscaleCheckboxProcessor().process(stream.getvalue(), analysis, ProcessorSettings())
    mark = report.observations[0]
    assert mark.decision == "unselected"
    assert mark.crop_bounds == [60, 60, 240, 240]
    assert mark.sigma == pytest.approx(12.6)


def test_invalid_document_is_explicit_failure(synthetic: tuple[bytes, dict[str, Any]]) -> None:
    with pytest.raises(ProcessingError, match="invalid_image_document"):
        GrayscaleCheckboxProcessor().process(b"invalid", synthetic[1], ProcessorSettings())
