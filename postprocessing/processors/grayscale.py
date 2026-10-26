from __future__ import annotations

import math
import time
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import ValidationError

from postprocessing.analysis import Observation, observations, page_map
from postprocessing.imaging import RGB, bounds, raster_pages
from postprocessing.models import (
    Decision,
    Finding,
    GrayscaleOptions,
    ObservationResult,
    ProcessingError,
    ProcessorReport,
    ProcessorSettings,
    ProcessorSpec,
    State,
)


def classify(score: int, options: GrayscaleOptions) -> Decision:
    if score < options.selected_threshold:
        return "selected"
    if score > options.unselected_threshold:
        return "unselected"
    return "needs_review"


def score_crop(crop: RGB) -> tuple[int, float]:
    height, width = crop.shape[:2]
    if min(height, width) < 3:
        raise ProcessingError("crop_too_small")
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    sigma = max(1.5, 0.07 * min(width, height))
    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    x0, y0 = math.ceil(width * 0.2), math.ceil(height * 0.2)
    x1, y1 = math.floor(width * 0.8), math.floor(height * 0.8)
    central = blurred[y0:y1, x0:x1]
    if not central.size:
        raise ProcessingError("empty_crop_core")
    return int(np.min(central)), sigma


def _result(
    item: Observation,
    settings: ProcessorSettings,
    decision: Decision,
    *,
    score: int | None = None,
    sigma: float | None = None,
    crop_bounds: list[int] | None = None,
    reason: str | None = None,
) -> ObservationResult:
    effective: State | None = item.original_state
    if settings.mode == "enforce":
        effective = decision if decision in ("selected", "unselected") else None
    scope: Literal["page", "field"] = "field" if item.scope == "field" else "page"
    return ObservationResult(
        observation_id=item.observation_id,
        scope=scope,
        page_number=item.page_number,
        original_state=item.original_state,
        original_confidence=item.confidence,
        decision=decision,
        disagrees_with_di=(
            decision in ("selected", "unselected")
            and item.original_state is not None
            and decision != item.original_state
        ),
        effective_state=effective,
        requires_review=decision == "needs_review",
        score=score,
        sigma=sigma,
        crop_bounds=crop_bounds,
        reason=reason,
    )


class GrayscaleCheckboxProcessor:
    requires_document = True

    @property
    def spec(self) -> ProcessorSpec:
        return ProcessorSpec(name="grayscale_checkboxes", version="1.0.0")

    def process(
        self, document: bytes, analysis: dict[str, Any], settings: ProcessorSettings
    ) -> ProcessorReport:
        started = time.perf_counter()
        try:
            options = GrayscaleOptions.model_validate(settings.options)
        except ValidationError:
            raise ProcessingError("invalid_grayscale_options") from None
        items = observations(analysis, settings)
        results: dict[str, ObservationResult] = {}
        parameters = {
            "pdf_dpi": 300,
            "selected_threshold": options.selected_threshold,
            "unselected_threshold": options.unselected_threshold,
            "central_fraction": 0.6,
            "sigma_min": 1.5,
            "sigma_fraction": 0.07,
        }
        if settings.mode == "off":
            return ProcessorReport(
                processor=self.spec,
                mode="off",
                status="off",
                requires_review=False,
                observations=[_result(item, settings, "not_run") for item in items],
                timings_ms={"total_compute": (time.perf_counter() - started) * 1000},
                parameters=parameters,
            )
        pages = page_map(analysis, settings)
        valid: dict[int, list[Observation]] = {}
        for item in items:
            reason = item.geometry_error
            if item.original_state is None:
                reason = "invalid_original_state"
            if reason or item.page_number is None:
                results[item.observation_id] = _result(
                    item, settings, "needs_review", reason=reason or "invalid_page_reference"
                )
            else:
                valid.setdefault(item.page_number, []).append(item)
        roi_ms = 0.0
        rendering_ms = 0.0
        iterator = raster_pages(document, set(valid), settings)
        try:
            while True:
                rendering_started = time.perf_counter()
                try:
                    number, pixels, unit = next(iterator)
                except StopIteration:
                    break
                rendering_ms += (time.perf_counter() - rendering_started) * 1000
                for item in valid[number]:
                    roi_started = time.perf_counter()
                    try:
                        rectangle = bounds(item.polygon, pages[number], pixels, unit)
                        x0, y0, x1, y1 = rectangle
                        score, sigma = score_crop(pixels[y0:y1, x0:x1])
                        results[item.observation_id] = _result(
                            item,
                            settings,
                            classify(score, options),
                            score=score,
                            sigma=sigma,
                            crop_bounds=list(rectangle),
                        )
                    except ProcessingError as error:
                        results[item.observation_id] = _result(
                            item, settings, "needs_review", reason=error.code
                        )
                    roi_ms += (time.perf_counter() - roi_started) * 1000
        finally:
            iterator.close()
        ordered = [results[item.observation_id] for item in items]
        findings = (
            []
            if ordered
            else [
                Finding(
                    code="no_selection_marks_returned",
                    severity="warning",
                    requires_review=True,
                )
            ]
        )
        return ProcessorReport(
            processor=self.spec,
            mode=settings.mode,
            status="completed",
            requires_review=not ordered or any(item.requires_review for item in ordered),
            observations=ordered,
            findings=findings,
            timings_ms={
                "render": round(rendering_ms, 3),
                "roi": round(roi_ms, 3),
                "total_compute": round((time.perf_counter() - started) * 1000, 3),
            },
            parameters=parameters,
        )
