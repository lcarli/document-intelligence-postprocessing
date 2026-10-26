from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Mode = Literal["off", "shadow", "enforce"]
State = Literal["selected", "unselected"]
Decision = Literal["selected", "unselected", "needs_review", "not_run"]


class ProcessingError(Exception):
    """A safe, fixed error code; never contains document content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class BlobReference(Model):
    name: str = Field(min_length=1, max_length=512)
    etag: str = Field(min_length=3, max_length=128)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if (
            any(ord(char) < 32 for char in value)
            or any(char in value for char in "\\?#:%")
            or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
        ):
            raise ValueError("Expected a relative blob name, not a URL or path traversal")
        return value

    @field_validator("etag")
    @classmethod
    def exact_etag(cls, value: str) -> str:
        if not re.fullmatch(r'"[A-Za-z0-9-]+"', value):
            raise ValueError("An exact quoted ETag is required")
        return value


class Submission(Model):
    document: BlobReference
    analysis: BlobReference


class ProcessorSettings(Model):
    mode: Mode = "shadow"
    options: dict[str, Any] = Field(default_factory=dict)
    max_document_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    max_analysis_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_page_pixels: int = Field(default=16_000_000, ge=1_000_000, le=40_000_000)
    max_observations: int = Field(default=5000, ge=1, le=10_000)


class GrayscaleOptions(Model):
    selected_threshold: int = Field(default=90, ge=0, le=255)
    unselected_threshold: int = Field(default=150, ge=0, le=255)

    @model_validator(mode="after")
    def threshold_order(self) -> GrayscaleOptions:
        if self.selected_threshold >= self.unselected_threshold:
            raise ValueError("Selected threshold must be below unselected threshold")
        return self


class ProcessorSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


class JobManifest(Model):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    owner: str
    created_at: str
    submission: Submission
    settings: dict[str, ProcessorSettings]
    processors: list[ProcessorSpec]

    @model_validator(mode="after")
    def matching_configuration(self) -> JobManifest:
        names = [processor.name for processor in self.processors]
        if len(names) != len(set(names)) or not names or set(names) != set(self.settings):
            raise ValueError("Processor specifications and configuration must match")
        return self


class ObservationResult(Model):
    observation_id: str
    scope: Literal["page", "field"]
    page_number: int | None
    original_state: State | None
    original_confidence: float | None
    decision: Decision
    disagrees_with_di: bool = False
    effective_state: State | None
    requires_review: bool
    score: int | None = None
    sigma: float | None = None
    crop_bounds: list[int] | None = None
    reason: str | None = None


class Finding(Model):
    code: str
    severity: Literal["info", "warning", "error"]
    target: str | None = None
    requires_review: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProcessorReport(Model):
    processor: ProcessorSpec
    mode: Mode
    status: Literal["completed", "failed", "off"]
    requires_review: bool
    observations: list[ObservationResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    source_sha256: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
