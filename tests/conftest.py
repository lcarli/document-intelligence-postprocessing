from __future__ import annotations

import copy
from typing import Any

import pytest

from examples.offline_demo import synthetic_input
from postprocessing.config import Settings
from postprocessing.models import BlobReference, ProcessingError, Submission


class MemoryRepository:
    def __init__(self) -> None:
        self.inputs: dict[str, bytes] = {}
        self.outputs: dict[str, dict[str, Any]] = {}
        self.reads = 0

    def read_input(self, reference: BlobReference, limit: int) -> bytes:
        self.reads += 1
        if reference.etag != '"v1"':
            raise ProcessingError("storage_access_or_version_error")
        data = self.inputs[reference.name]
        if len(data) > limit:
            raise ProcessingError("input_too_large")
        return data

    def read_output(self, key: str) -> dict[str, Any] | None:
        value = self.outputs.get(key)
        return copy.deepcopy(value) if value is not None else None

    def write_output(self, key: str, value: dict[str, Any]) -> None:
        self.outputs.setdefault(key, copy.deepcopy(value))


@pytest.fixture
def synthetic() -> tuple[bytes, dict[str, Any]]:
    return synthetic_input()


@pytest.fixture
def config() -> Settings:
    return Settings(
        blob_account_url="https://syntheticaccount.blob.core.windows.net",
        tenant_id="00000000-0000-4000-8000-000000000001",
        audience="00000000-0000-4000-8000-000000000002",
        allowed_client_ids=("00000000-0000-4000-8000-000000000003",),
    )


@pytest.fixture
def submission() -> Submission:
    return Submission(
        document=BlobReference(name="documents/synthetic.png", etag='"v1"'),
        analysis=BlobReference(name="analyses/synthetic.json", etag='"v1"'),
    )
