from __future__ import annotations

import os
import re
from functools import lru_cache
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, field_validator

from postprocessing.models import GrayscaleOptions, Model, ProcessorSettings


class Settings(Model):
    blob_account_url: str
    input_container: str = "postprocessing-inputs"
    output_container: str = "postprocessing-outputs"
    tenant_id: str
    audience: str = Field(min_length=1)
    allowed_client_ids: tuple[str, ...]
    required_permission: str = "Postprocess.Run"
    processors: dict[str, ProcessorSettings] = Field(
        default_factory=lambda: {"grayscale_checkboxes": ProcessorSettings()}
    )

    @field_validator("blob_account_url")
    @classmethod
    def azure_blob_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", host)
            or parsed.netloc != host
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Configure an HTTPS Azure public-cloud Blob account endpoint")
        return value.rstrip("/")

    @field_validator("tenant_id")
    @classmethod
    def tenant_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("allowed_client_ids")
    @classmethod
    def clients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("At least one authorized caller application is required")
        return tuple(str(UUID(value)) for value in values)

    @field_validator("input_container", "output_container")
    @classmethod
    def container_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", value) or "--" in value:
            raise ValueError("Invalid Azure Blob container name")
        return value


@lru_cache(maxsize=1)
def settings() -> Settings:
    limits: dict[str, object] = {
        "mode": os.getenv("CHECKBOX_VERIFIER_MODE", "shadow"),
        "options": GrayscaleOptions.model_validate(
            {
                "selected_threshold": os.getenv("CHECKBOX_SELECTED_THRESHOLD", "90"),
                "unselected_threshold": os.getenv("CHECKBOX_UNSELECTED_THRESHOLD", "150"),
            }
        ).model_dump(),
    }
    for name in (
        "max_document_bytes",
        "max_analysis_bytes",
        "max_pages",
        "max_page_pixels",
        "max_observations",
    ):
        if value := os.getenv(f"APP_{name.upper()}"):
            limits[name] = value
    return Settings(
        blob_account_url=os.environ["APP_BLOB_ACCOUNT_URL"],
        input_container=os.getenv("APP_INPUT_CONTAINER", "postprocessing-inputs"),
        output_container=os.getenv("APP_OUTPUT_CONTAINER", "postprocessing-outputs"),
        tenant_id=os.environ["APP_AUTH_TENANT_ID"],
        audience=os.environ["APP_AUTH_AUDIENCE"],
        allowed_client_ids=tuple(
            value.strip()
            for value in os.environ["APP_AUTH_ALLOWED_CLIENT_IDS"].split(",")
            if value.strip()
        ),
        required_permission=os.getenv("APP_AUTH_REQUIRED_PERMISSION", "Postprocess.Run"),
        processors={"grayscale_checkboxes": ProcessorSettings.model_validate(limits)},
    )
