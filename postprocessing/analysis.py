from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, NoReturn

from postprocessing.models import ProcessingError, ProcessorSettings, State


@dataclass(frozen=True)
class Observation:
    observation_id: str
    scope: str
    page_number: int | None
    polygon: Any
    original_state: State | None
    confidence: float | None
    geometry_error: str | None = None


def _reject_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON numeric literal")


def parse_analysis(data: bytes) -> dict[str, Any]:
    try:
        result = json.loads(data, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProcessingError("invalid_analysis_json") from None
    if not isinstance(result, dict):
        raise ProcessingError("invalid_analyze_result")
    return result


def page_map(analysis: dict[str, Any], limits: ProcessorSettings) -> dict[int, dict[str, Any]]:
    pages = analysis.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) > limits.max_pages:
        raise ProcessingError("invalid_or_excessive_analysis_pages")
    result: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise ProcessingError("invalid_analysis_page")
        number = page.get("pageNumber")
        if type(number) is not int or number < 1 or number in result:
            raise ProcessingError("invalid_analysis_page_number")
        result[number] = page
    return result


def confidence(value: Any) -> float | None:
    if type(value) in (float, int) and 0 <= value <= 1 and math.isfinite(value):
        return float(value)
    return None


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def observations(analysis: dict[str, Any], limits: ProcessorSettings) -> list[Observation]:
    pages = page_map(analysis, limits)
    found: list[Observation] = []
    visited = 0

    def append(path: str, scope: str, state: Any, conf: Any, regions: Any) -> None:
        if len(found) >= limits.max_observations:
            raise ProcessingError("too_many_selection_marks")
        page_number, polygon, reason = None, None, None
        if isinstance(regions, list) and len(regions) == 1 and isinstance(regions[0], dict):
            page_number = regions[0].get("pageNumber")
            polygon = regions[0].get("polygon")
            if type(page_number) is not int or page_number not in pages:
                page_number = None
                reason = "invalid_page_reference"
        else:
            reason = "missing_or_ambiguous_geometry"
        original: State | None = state if state in ("selected", "unselected") else None
        found.append(
            Observation(path, scope, page_number, polygon, original, confidence(conf), reason)
        )

    def walk(value: Any, path: str, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > 32 or visited > 20_000:
            raise ProcessingError("analysis_nesting_limit")
        if not isinstance(value, dict):
            raise ProcessingError("invalid_custom_field")
        kind = value.get("type")
        if kind == "selectionMark":
            append(
                path,
                "field",
                value.get("valueSelectionMark"),
                value.get("confidence"),
                value.get("boundingRegions"),
            )
        elif kind == "array":
            children = value.get("valueArray", [])
            if not isinstance(children, list):
                raise ProcessingError("invalid_custom_array")
            for index, child in enumerate(children):
                walk(child, f"{path}/valueArray/{index}", depth + 1)
        elif kind == "object":
            children = value.get("valueObject", {})
            if not isinstance(children, dict):
                raise ProcessingError("invalid_custom_object")
            for name, child in children.items():
                walk(child, f"{path}/valueObject/{_pointer(name)}", depth + 1)

    for index, page in enumerate(pages.values()):
        marks = page.get("selectionMarks", [])
        if not isinstance(marks, list):
            raise ProcessingError("invalid_page_selection_marks")
        for mark_index, mark in enumerate(marks):
            if not isinstance(mark, dict):
                raise ProcessingError("invalid_page_selection_mark")
            append(
                f"/pages/{index}/selectionMarks/{mark_index}",
                "page",
                mark.get("state"),
                mark.get("confidence"),
                [{"pageNumber": page["pageNumber"], "polygon": mark.get("polygon")}],
            )
    documents = analysis.get("documents", [])
    if not isinstance(documents, list):
        raise ProcessingError("invalid_custom_documents")
    for document_index, document in enumerate(documents):
        if not isinstance(document, dict) or not isinstance(document.get("fields", {}), dict):
            raise ProcessingError("invalid_custom_documents")
        for name, field in document.get("fields", {}).items():
            walk(field, f"/documents/{document_index}/fields/{_pointer(name)}", 0)
    return found
