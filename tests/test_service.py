from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from postprocessing.models import (
    ProcessingError,
    ProcessorReport,
    ProcessorSettings,
    ProcessorSpec,
    Submission,
)
from postprocessing.registry import registered_specs
from postprocessing.service import PostprocessingService, job_id, key
from tests.conftest import MemoryRepository


def setup_job(
    synthetic: tuple[bytes, dict[str, Any]],
    submission: Submission,
    mode: str = "shadow",
) -> tuple[PostprocessingService, MemoryRepository, Any]:
    repository = MemoryRepository()
    repository.inputs[submission.document.name] = synthetic[0]
    repository.inputs[submission.analysis.name] = json.dumps(synthetic[1]).encode()
    service = PostprocessingService(repository)
    manifest = service.submit(
        submission,
        "owner-a",
        {
            spec.name: ProcessorSettings.model_validate({"mode": mode})
            for spec in registered_specs()
        },
    )
    return service, repository, manifest


def test_complete_service_flow(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, repository, manifest = setup_job(synthetic, submission)
    service.run(manifest.job_id, manifest.processors[0])
    first = copy.deepcopy(repository.outputs)
    reads = repository.reads
    service.run(manifest.job_id, manifest.processors[0])
    assert repository.outputs == first
    assert repository.reads == reads
    assert service.aggregate(manifest.job_id) == {"status": "needs_review"}
    result = service.result(manifest.job_id, "owner-a")
    assert result is not None
    assert result["analyze_result"] == synthetic[1]
    assert result["processors"][0]["source_sha256"]["document"]
    assert service.aggregate(manifest.job_id) == {"status": "needs_review"}


def test_owner_bound_status_and_results(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, _, manifest = setup_job(synthetic, submission)
    with pytest.raises(ProcessingError, match="job_not_found"):
        service.result(manifest.job_id, "owner-b")
    assert service.result(manifest.job_id, "owner-a") is None
    with pytest.raises(ProcessingError, match="job_not_found"):
        service.manifest("00000000-0000-4000-8000-000000000099")


def test_cannot_submit_another_owners_inputs(submission: Submission) -> None:
    service = PostprocessingService(MemoryRepository())
    with pytest.raises(ProcessingError, match="input_namespace_forbidden"):
        service.submit(
            submission,
            "owner-b",
            {"grayscale_checkboxes": ProcessorSettings()},
        )


def test_invalid_inputs_fail_without_fake_success(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, repository, manifest = setup_job(synthetic, submission)
    repository.inputs[submission.document.name] = b"invalid document"
    service.run(manifest.job_id, manifest.processors[0])
    assert service.aggregate(manifest.job_id) == {"status": "failed"}
    result = service.result(manifest.job_id, "owner-a")
    assert result is not None
    assert result["status"] == "failed"
    assert result["analyze_result"] is None
    assert result["processors"][0]["errors"] == ["invalid_image_document"]


def test_corrupt_digest_is_not_accepted(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, repository, manifest = setup_job(synthetic, submission)
    service.run(manifest.job_id, manifest.processors[0])
    repository.inputs[submission.analysis.name] = json.dumps({"pages": []}).encode()
    with pytest.raises(ProcessingError, match="analysis_digest_mismatch"):
        service.aggregate(manifest.job_id)


def test_off_does_not_read_source_document(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, repository, manifest = setup_job(synthetic, submission, "off")
    del repository.inputs[submission.document.name]
    service.run(manifest.job_id, manifest.processors[0])
    assert service.aggregate(manifest.job_id) == {"status": "completed"}


def test_missing_report_and_invalid_registry(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    service, _, manifest = setup_job(synthetic, submission)
    with pytest.raises(ProcessingError, match="processor_report_missing"):
        service.aggregate(manifest.job_id)
    with pytest.raises(ProcessingError, match="processor_version_unavailable"):
        service.run(manifest.job_id, ProcessorSpec(name="unknown", version="1.0.0"))


def test_registry_extension_and_conflict_detection(
    synthetic: tuple[bytes, dict[str, Any]],
    submission: Submission,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from postprocessing import registry
    from postprocessing.processors.grayscale import GrayscaleCheckboxProcessor

    class SecondProcessor:
        requires_document = True

        spec = ProcessorSpec(name="example_rule", version="1.0.0")

        def process(
            self, document: bytes, analysis: dict[str, Any], settings: ProcessorSettings
        ) -> ProcessorReport:
            original = GrayscaleCheckboxProcessor().process(document, analysis, settings)
            changed = original.observations[0].model_copy(update={"effective_state": "unselected"})
            return original.model_copy(update={"processor": self.spec, "observations": [changed]})

    monkeypatch.setattr(
        registry, "processors", lambda: (GrayscaleCheckboxProcessor(), SecondProcessor())
    )
    service, _, manifest = setup_job(synthetic, submission)
    assert len(manifest.processors) == 2
    for processor in manifest.processors:
        service.run(manifest.job_id, processor)
    service.aggregate(manifest.job_id)
    result = service.result(manifest.job_id, "owner-a")
    assert result is not None
    assert result["conflicting_targets"] == ["/pages/0/selectionMarks/0"]
    assert result["requires_review"]


def test_invalid_job_and_manifest(
    synthetic: tuple[bytes, dict[str, Any]], submission: Submission
) -> None:
    with pytest.raises(ProcessingError, match="invalid_job_id"):
        job_id("../other")
    service, repository, manifest = setup_job(synthetic, submission)
    repository.outputs[key(manifest.job_id, "manifest")]["settings"] = {"unknown": "bad"}
    with pytest.raises(ProcessingError, match="stored_manifest_invalid"):
        service.manifest(manifest.job_id)
