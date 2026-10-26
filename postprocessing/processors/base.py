from __future__ import annotations

from typing import Any, Protocol

from postprocessing.models import ProcessorReport, ProcessorSettings, ProcessorSpec


class Processor(Protocol):
    @property
    def spec(self) -> ProcessorSpec: ...

    @property
    def requires_document(self) -> bool: ...

    def process(
        self,
        document: bytes,
        analysis: dict[str, Any],
        settings: ProcessorSettings,
    ) -> ProcessorReport: ...
