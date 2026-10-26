from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from postprocessing.analysis import parse_analysis
from postprocessing.models import (
    JobManifest,
    ProcessingError,
    ProcessorReport,
    ProcessorSettings,
    ProcessorSpec,
    Submission,
)
from postprocessing.registry import registered_specs, resolve
from postprocessing.storage import Repository

logger = logging.getLogger(__name__)


def job_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProcessingError("invalid_job_id") from None
    return value


def key(identifier: str, suffix: str) -> str:
    return f"jobs/{job_id(identifier)}/{suffix}.json"


def report_suffix(spec: ProcessorSpec) -> str:
    if spec not in registered_specs():
        raise ProcessingError("processor_version_unavailable")
    return f"processors/{spec.name}-{spec.version}"


class PostprocessingService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def submit(
        self, submission: Submission, owner: str, settings: dict[str, ProcessorSettings]
    ) -> JobManifest:
        specs = registered_specs()
        if set(settings) != {spec.name for spec in specs}:
            raise ProcessingError("processor_configuration_mismatch")
        manifest = JobManifest(
            job_id=str(uuid4()),
            owner=owner,
            created_at=datetime.now(UTC).isoformat(),
            submission=submission,
            settings=settings,
            processors=specs,
        )
        self.repository.write_output(key(manifest.job_id, "manifest"), manifest.model_dump())
        return manifest

    def manifest(self, identifier: str, owner: str | None = None) -> JobManifest:
        data = self.repository.read_output(key(identifier, "manifest"))
        if data is None:
            raise ProcessingError("job_not_found")
        try:
            manifest = JobManifest.model_validate(data)
        except ValidationError:
            raise ProcessingError("stored_manifest_invalid") from None
        if owner is not None and not hmac.compare_digest(manifest.owner, owner):
            raise ProcessingError("job_not_found")
        return manifest

    def run(self, identifier: str, spec: ProcessorSpec) -> dict[str, str]:
        output_key = key(identifier, report_suffix(spec))
        if self.repository.read_output(output_key) is not None:
            return {"report_key": output_key}
        manifest = self.manifest(identifier)
        if spec not in manifest.processors:
            raise ProcessingError("unrequested_processor")
        settings = manifest.settings[spec.name]
        try:
            processor = resolve(spec)
            analysis_bytes = self.repository.read_input(
                manifest.submission.analysis, settings.max_analysis_bytes
            )
            document = (
                self.repository.read_input(
                    manifest.submission.document, settings.max_document_bytes
                )
                if settings.mode != "off" and processor.requires_document
                else b""
            )
            report = processor.process(document, parse_analysis(analysis_bytes), settings)
            report = report.model_copy(
                update={
                    "source_sha256": {
                        "analysis": hashlib.sha256(analysis_bytes).hexdigest(),
                        **({"document": hashlib.sha256(document).hexdigest()} if document else {}),
                    }
                }
            )
        except ProcessingError as error:
            logger.warning(
                "processor_failed",
                extra={"job_id": identifier, "processor": spec.name, "error_code": error.code},
            )
            report = ProcessorReport(
                processor=spec,
                mode=settings.mode,
                status="failed",
                requires_review=True,
                errors=[error.code],
            )
        self.repository.write_output(output_key, report.model_dump())
        return {"report_key": output_key}

    def aggregate(self, identifier: str) -> dict[str, str]:
        output_key = key(identifier, "result")
        existing = self.repository.read_output(output_key)
        if existing is not None:
            return {"status": str(existing["status"])}
        manifest = self.manifest(identifier)
        reports = []
        for spec in manifest.processors:
            data = self.repository.read_output(key(identifier, report_suffix(spec)))
            if data is None:
                raise ProcessingError("processor_report_missing")
            try:
                report = ProcessorReport.model_validate(data)
            except ValidationError:
                raise ProcessingError("processor_report_invalid") from None
            if report.processor != spec or report.mode != manifest.settings[spec.name].mode:
                raise ProcessingError("processor_report_mismatch")
            reports.append(report)
        failed = any(report.status == "failed" for report in reports)
        decisions: dict[str, set[str | None]] = {}
        for report in reports:
            for item in report.observations:
                decisions.setdefault(item.observation_id, set()).add(item.effective_state)
        conflicts = sorted(target for target, states in decisions.items() if len(states) > 1)
        review = bool(conflicts) or any(
            report.requires_review or any(finding.requires_review for finding in report.findings)
            for report in reports
        )
        status = "failed" if failed else ("needs_review" if review else "completed")
        result: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": identifier,
            "status": status,
            "requires_review": review,
            "conflicting_targets": conflicts,
            "created_at": manifest.created_at,
            "processors": [report.model_dump() for report in reports],
            "analyze_result": None,
        }
        if not failed:
            raw = self.repository.read_input(
                manifest.submission.analysis,
                min(settings.max_analysis_bytes for settings in manifest.settings.values()),
            )
            digest = hashlib.sha256(raw).hexdigest()
            if any(report.source_sha256.get("analysis") != digest for report in reports):
                raise ProcessingError("analysis_digest_mismatch")
            result["analyze_result"] = parse_analysis(raw)
        self.repository.write_output(output_key, result)
        logger.info("job_finalized", extra={"job_id": identifier, "processing_status": status})
        return {"status": status}

    def result(self, identifier: str, owner: str) -> dict[str, Any] | None:
        self.manifest(identifier, owner)
        return self.repository.read_output(key(identifier, "result"))
