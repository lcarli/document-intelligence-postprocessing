from __future__ import annotations

import json
from typing import Any, NoReturn, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from postprocessing.config import Settings
from postprocessing.models import BlobReference, ProcessingError


class TransientStorageError(Exception):
    """Safe retry signal without SDK messages or document references."""


class Repository(Protocol):
    def read_input(self, reference: BlobReference, limit: int) -> bytes: ...
    def read_output(self, key: str) -> dict[str, Any] | None: ...
    def write_output(self, key: str, value: dict[str, Any]) -> None: ...


class BlobRepository:
    def __init__(self, config: Settings) -> None:
        self._client = BlobServiceClient(
            config.blob_account_url,
            credential=DefaultAzureCredential(exclude_interactive_browser_credential=True),
            retry_total=2,
            connection_timeout=5,
            read_timeout=30,
            logging_enable=False,
        )
        self._input = config.input_container
        self._output = config.output_container

    @staticmethod
    def _error(error: HttpResponseError | ServiceRequestError | ServiceResponseError) -> NoReturn:
        if isinstance(error, (ServiceRequestError, ServiceResponseError)) or (
            isinstance(error, HttpResponseError)
            and (error.status_code in (408, 429) or (error.status_code or 0) >= 500)
        ):
            raise TransientStorageError("storage_temporarily_unavailable") from None
        raise ProcessingError("storage_access_or_version_error") from None

    def read_input(self, reference: BlobReference, limit: int) -> bytes:
        blob = self._client.get_blob_client(self._input, reference.name)
        try:
            data = blob.download_blob(
                offset=0,
                length=limit + 1,
                etag=reference.etag,
                match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            ).readall()
        except (HttpResponseError, ServiceRequestError, ServiceResponseError) as error:
            self._error(error)
        if len(data) > limit:
            raise ProcessingError("input_too_large")
        return bytes(data)

    def read_output(self, key: str) -> dict[str, Any] | None:
        blob = self._client.get_blob_client(self._output, key)
        try:
            data = blob.download_blob(offset=0, length=32 * 1024 * 1024 + 1).readall()
        except ResourceNotFoundError:
            return None
        except (HttpResponseError, ServiceRequestError, ServiceResponseError) as error:
            self._error(error)
        if len(data) > 32 * 1024 * 1024:
            raise ProcessingError("stored_result_too_large")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ProcessingError("stored_result_invalid") from None
        if not isinstance(value, dict):
            raise ProcessingError("stored_result_invalid")
        return value

    def write_output(self, key: str, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        if len(data) > 32 * 1024 * 1024:
            raise ProcessingError("stored_result_too_large")
        try:
            self._client.get_blob_client(self._output, key).upload_blob(
                data,
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            return  # First committed activity result wins after a retry.
        except (HttpResponseError, ServiceRequestError, ServiceResponseError) as error:
            self._error(error)
