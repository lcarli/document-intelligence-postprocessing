from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from pydantic import ValidationError

from postprocessing.auth import AuthorizationError, authenticate
from postprocessing.config import settings
from postprocessing.models import ProcessingError, ProcessorSpec, Submission
from postprocessing.service import PostprocessingService, job_id
from postprocessing.storage import BlobRepository, TransientStorageError
from postprocessing.workflow import orchestrate

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def service() -> PostprocessingService:
    return PostprocessingService(BlobRepository(settings()))


def response(data: dict[str, Any], status: int = 200) -> func.HttpResponse:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status in (202, 503):
        headers["Retry-After"] = "3"
    return func.HttpResponse(
        json.dumps(data, ensure_ascii=True, allow_nan=False),
        status_code=status,
        mimetype="application/json",
        headers=headers,
    )


def error_response(error: Exception) -> func.HttpResponse:
    if isinstance(error, AuthorizationError):
        return response({"error": "access_denied"}, error.status_code)
    if isinstance(error, (ValidationError, ValueError, UnicodeError, RecursionError)):
        return response({"error": "invalid_request"}, 400)
    if isinstance(error, TransientStorageError):
        return response({"error": "temporarily_unavailable"}, 503)
    if isinstance(error, ProcessingError):
        status = 404 if error.code in ("job_not_found", "invalid_job_id") else 409
        return response({"error": error.code}, status)
    raise error


@app.route(route="v1/jobs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def submit(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    try:
        owner = await asyncio.to_thread(authenticate, req.headers.get("Authorization"), settings())
        if len(req.get_body()) > 8192:
            return response({"error": "request_too_large"}, 413)
        if req.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return response({"error": "application_json_required"}, 415)
        submission = Submission.model_validate_json(req.get_body())
        manifest = await asyncio.to_thread(
            service().submit, submission, owner, settings().processors
        )
        await client.start_new(
            "postprocess",
            instance_id=manifest.job_id,
            client_input={"processors": [spec.model_dump() for spec in manifest.processors]},
        )
        logger.info("job_submitted", extra={"job_id": manifest.job_id})
        return response(
            {
                "job_id": manifest.job_id,
                "status": "accepted",
                "status_path": f"/api/v1/jobs/{manifest.job_id}",
                "result_path": f"/api/v1/jobs/{manifest.job_id}/result",
            },
            202,
        )
    except (
        AuthorizationError,
        ValidationError,
        ProcessingError,
        TransientStorageError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as error:
        return error_response(error)


@app.route(route="v1/jobs/{identifier}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def status(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    try:
        owner = await asyncio.to_thread(authenticate, req.headers.get("Authorization"), settings())
        identifier = job_id(req.route_params["identifier"])
        await asyncio.to_thread(service().manifest, identifier, owner)
        state = await client.get_status(identifier, show_input=False)
        if state is None:
            return response({"job_id": identifier, "status": "not_scheduled"}, 409)
        runtime = state.runtime_status.value
        data: dict[str, Any] = {"job_id": identifier, "runtime_status": runtime}
        if runtime == "Completed":
            result = await asyncio.to_thread(service().result, identifier, owner)
            if result is None:
                raise ProcessingError("completed_result_missing")
            data["status"] = result["status"]
            data["requires_review"] = result["requires_review"]
        elif runtime in ("Failed", "Terminated", "Canceled"):
            data["status"] = "failed"
            data["error"] = "workflow_failed"
        else:
            data["status"] = "processing"
        return response(data)
    except (AuthorizationError, ProcessingError, TransientStorageError) as error:
        return error_response(error)


@app.route(route="v1/jobs/{identifier}/result", methods=["GET"])
async def result(req: func.HttpRequest) -> func.HttpResponse:
    try:
        owner = await asyncio.to_thread(authenticate, req.headers.get("Authorization"), settings())
        identifier = job_id(req.route_params["identifier"])
        data = await asyncio.to_thread(service().result, identifier, owner)
        if data is None:
            return response({"job_id": identifier, "status": "result_not_available"}, 202)
        return response(data)
    except (AuthorizationError, ProcessingError, TransientStorageError) as error:
        return error_response(error)


@app.orchestration_trigger(context_name="context")
def postprocess(context: df.DurableOrchestrationContext) -> Any:
    return (yield from orchestrate(context))


@app.activity_trigger(input_name="payload")
def run_processor(payload: dict[str, Any]) -> dict[str, str]:
    try:
        return service().run(payload["job_id"], ProcessorSpec.model_validate(payload["processor"]))
    except (ProcessingError, TransientStorageError) as error:
        # Durable retries see only fixed operational codes, not SDK exception messages.
        raise RuntimeError(
            error.code if isinstance(error, ProcessingError) else "storage_temporarily_unavailable"
        ) from None


@app.activity_trigger(input_name="payload")
def aggregate_reports(payload: dict[str, Any]) -> dict[str, str]:
    try:
        return service().aggregate(payload["job_id"])
    except (ProcessingError, TransientStorageError) as error:
        raise RuntimeError(
            error.code if isinstance(error, ProcessingError) else "storage_temporarily_unavailable"
        ) from None
