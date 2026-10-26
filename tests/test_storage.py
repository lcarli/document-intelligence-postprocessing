from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)

from postprocessing.models import BlobReference, ProcessingError
from postprocessing.storage import BlobRepository, TransientStorageError


def repository(blob: Any) -> BlobRepository:
    value = BlobRepository.__new__(BlobRepository)
    value._client = SimpleNamespace(get_blob_client=Mock(return_value=blob))
    value._input, value._output = "inputs", "outputs"
    return value


def test_conditional_bounded_read() -> None:
    blob = SimpleNamespace(
        download_blob=Mock(return_value=SimpleNamespace(readall=lambda: b"test"))
    )
    store = repository(blob)
    reference = BlobReference(name="synthetic.bin", etag='"version1"')
    assert store.read_input(reference, 100) == b"test"
    kwargs = blob.download_blob.call_args.kwargs
    assert kwargs["length"] == 101
    assert kwargs["etag"] == '"version1"'
    assert kwargs["match_condition"] == MatchConditions.IfNotModified
    with pytest.raises(ProcessingError, match="input_too_large"):
        store.read_input(reference, 3)


def test_output_loading_and_write_once() -> None:
    blob = SimpleNamespace(
        download_blob=Mock(return_value=SimpleNamespace(readall=lambda: b'{"ok":true}')),
        upload_blob=Mock(),
    )
    store = repository(blob)
    assert store.read_output("key") == {"ok": True}
    store.write_output("key", {"ok": True})
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    blob.upload_blob.side_effect = ResourceExistsError()
    store.write_output("key", {"ok": True})
    blob.download_blob.side_effect = ResourceNotFoundError()
    assert store.read_output("key") is None
    blob.download_blob.side_effect = None
    blob.download_blob.return_value = SimpleNamespace(readall=lambda: b"invalid")
    with pytest.raises(ProcessingError, match="stored_result_invalid"):
        store.read_output("key")
    blob.download_blob.return_value = SimpleNamespace(readall=lambda: b"[]")
    with pytest.raises(ProcessingError, match="stored_result_invalid"):
        store.read_output("key")


@pytest.mark.parametrize("error", [ServiceRequestError("private"), ServiceResponseError("private")])
def test_transient_error_is_sanitized(error: Exception) -> None:
    blob = SimpleNamespace(download_blob=Mock(side_effect=error))
    with pytest.raises(TransientStorageError, match="storage_temporarily_unavailable") as caught:
        repository(blob).read_output("private-name")
    assert "private-name" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_http_failures_are_classified() -> None:
    for status in (403, 404, 412, 429, 500):
        error = HttpResponseError("private detail")
        error.status_code = status
        blob = SimpleNamespace(download_blob=Mock(side_effect=error))
        expected = TransientStorageError if status in (429, 500) else ProcessingError
        with pytest.raises(expected):
            repository(blob).read_output("private-name")
