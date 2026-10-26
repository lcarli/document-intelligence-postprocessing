"""Run the actual processor against an in-memory synthetic form, without Azure."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from postprocessing.models import ProcessorSettings  # noqa: E402
from postprocessing.processors.grayscale import GrayscaleCheckboxProcessor  # noqa: E402


def synthetic_input() -> tuple[bytes, dict[str, Any]]:
    image = Image.new("RGB", (240, 100), "white")
    draw = ImageDraw.Draw(image)
    polygons = []
    for left, shade in ((10, 180), (90, 180), (170, 120)):
        draw.rectangle((left, 20, left + 50, 70), fill=(shade,) * 3, outline="black", width=2)
        polygons.append([left, 20, left + 50, 20, left + 50, 70, left, 70])
    draw.line((100, 30, 130, 60), fill="black", width=6)
    draw.line((100, 60, 130, 30), fill="black", width=6)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    image.close()
    return stream.getvalue(), {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "pages": [
            {
                "pageNumber": 1,
                "width": 240,
                "height": 100,
                "unit": "pixel",
                "selectionMarks": [
                    {"state": "selected", "confidence": 0.95, "polygon": polygon}
                    for polygon in polygons
                ],
            }
        ],
    }


def main() -> None:
    document, analysis = synthetic_input()
    report = GrayscaleCheckboxProcessor().process(document, analysis, ProcessorSettings())
    expected = ["unselected", "selected", "needs_review"]
    actual = [item.decision for item in report.observations]
    if actual != expected:
        raise RuntimeError(f"Synthetic smoke test failed: {actual}")
    print(json.dumps(report.model_dump(), indent=2))


if __name__ == "__main__":
    main()
