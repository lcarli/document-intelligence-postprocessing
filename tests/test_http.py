from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import azure.functions as func
import pytest

import function_app
from postprocessing.auth import AuthorizationError
from postprocessing.config import Settings
from postprocessing.models import ProcessingError, Submission
from postprocessing.service import key
from postprocessing.storage import TransientStorageError
from tests.test_service import setup_job

FUNCTIONS = {
    function.get_function_name(): inspect.unwrap(function.get_user_function())
    for function in function_app.app.get_functions()
}


def request(method: str, body: bytes = b"", identifier: str | None = None) -> func.HttpRequest:
    return func.HttpRequest(
        method=method,
        url="https://synthetic.invalid/api/v1/jobs",
        headers={"Authorization": "Bearer synthetic", "Content-Type": "application/json"},
        body=body,
        route_params={"identifier": identifier} if identifier else {},
    )


@pytest.fixture
def http_setup(
    synthetic: tuple[bytes, dict[str, Any]],
    submission: Submission,
    config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, Any]:
    service, repository, manifest = setup_job(synthetic, submission)
    monkeypatch.setattr(function_app, "settings", lambda: config)
    monkeypatch.setattr(function_app, "service", lambda: service)
    monkeypatch.setattr(function_app, "authenticate", lambda header, config: "owner-a")
    return service, repository, manifest


def test_http_complete_flow(http_setup: tuple[Any, Any, Any], submission: Submission) -> None:
    service, _, _ = http_setup
    client = SimpleNamespace(start_new=AsyncMock())
    accepted = asyncio.run(
        FUNCTIONS["submit"](request("POST", submission.model_dump_json().encode()), client)
    )
    assert accepted.status_code == 202
    assert accepted.headers["Cache-Control"] == "no-store"
    body = json.loads(accepted.get_body())
    identifier = body["job_id"]
    started = client.start_new.call_args.kwargs
    assert set(started["client_input"]) == {"processors"}
    assert "systemKey" not in accepted.get_body().decode()
    manifest = service.manifest(identifier)
    service.run(identifier, manifest.processors[0])
    service.aggregate(identifier)
    client.get_status = AsyncMock(
        return_value=SimpleNamespace(runtime_status=SimpleNamespace(value="Completed"))
    )
    status = asyncio.run(FUNCTIONS["status"](request("GET", identifier=identifier), client))
    assert json.loads(status.get_body())["status"] == "needs_review"
    result = asyncio.run(FUNCTIONS["result"](request("GET", identifier=identifier)))
    assert result.status_code == 200
    assert json.loads(result.get_body())["processors"]


def test_http_rejects_bad_input(http_setup: tuple[Any, Any, Any]) -> None:
    client = SimpleNamespace(start_new=AsyncMock())
    invalid = asyncio.run(FUNCTIONS["submit"](request("POST", b'{"extra":"bad"}'), client))
    assert invalid.status_code == 400
    large = asyncio.run(FUNCTIONS["submit"](request("POST", b"x" * 8193), client))
    assert large.status_code == 413
    wrong_type = request("POST")
    wrong_type = func.HttpRequest(
        method="POST", url=wrong_type.url, headers={"Content-Type": "text/plain"}, body=b"{}"
    )
    response = asyncio.run(FUNCTIONS["submit"](wrong_type, client))
    assert response.status_code == 415
    client.start_new.assert_not_called()


@pytest.mark.parametrize("code", [401, 403])
def test_unauthorized_http_calls(
    http_setup: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def denied(header: str, config: Settings) -> str:
        raise AuthorizationError(code)

    monkeypatch.setattr(function_app, "authenticate", denied)
    _, _, manifest = http_setup
    for name in ("submit", "status", "result"):
        args = [request("GET", identifier=manifest.job_id)]
        if name != "result":
            args.append(SimpleNamespace())
        response = asyncio.run(FUNCTIONS[name](*args))
        assert response.status_code == code
        if code == 401:
            assert response.headers["WWW-Authenticate"] == "Bearer"


def test_status_does_not_expose_durable_error_details(http_setup: tuple[Any, Any, Any]) -> None:
    _, _, manifest = http_setup
    client = SimpleNamespace()
    for runtime, expected in (("Running", "processing"), ("Failed", "failed")):
        client.get_status = AsyncMock(
            return_value=SimpleNamespace(runtime_status=SimpleNamespace(value=runtime))
        )
        response = asyncio.run(
            FUNCTIONS["status"](request("GET", identifier=manifest.job_id), client)
        )
        assert json.loads(response.get_body())["status"] == expected
    client.get_status = AsyncMock(return_value=None)
    response = asyncio.run(FUNCTIONS["status"](request("GET", identifier=manifest.job_id), client))
    assert response.status_code == 409
    response = asyncio.run(FUNCTIONS["result"](request("GET", identifier=manifest.job_id)))
    assert response.status_code == 202
    response = asyncio.run(FUNCTIONS["result"](request("GET", identifier="not-a-uuid")))
    assert response.status_code == 404


def test_http_owner_is_checked_before_durable_lookup(
    http_setup: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, manifest = http_setup
    monkeypatch.setattr(function_app, "authenticate", lambda header, config: "other-owner")
    client = SimpleNamespace(get_status=AsyncMock())
    response = asyncio.run(FUNCTIONS["status"](request("GET", identifier=manifest.job_id), client))
    assert response.status_code == 404
    client.get_status.assert_not_called()


def test_activity_entrypoints_and_safe_failures(http_setup: tuple[Any, Any, Any]) -> None:
    _, repository, manifest = http_setup
    payload = {"job_id": manifest.job_id, "processor": manifest.processors[0].model_dump()}
    assert "report_key" in FUNCTIONS["run_processor"](payload)
    assert FUNCTIONS["aggregate_reports"]({"job_id": manifest.job_id})["status"] == "needs_review"
    repository.outputs.pop(key(manifest.job_id, "result"))
    repository.outputs.pop(key(manifest.job_id, "manifest"))
    with pytest.raises(RuntimeError, match="job_not_found"):
        FUNCTIONS["aggregate_reports"]({"job_id": manifest.job_id})
    for error, expected in (
        (TransientStorageError("sensitive SDK detail"), "temporarily_unavailable"),
        (ProcessingError("job_not_found"), "job_not_found"),
    ):
        response = function_app.error_response(error)
        assert json.loads(response.get_body())["error"] == expected


def test_function_discovery_and_generic_response() -> None:
    assert len(FUNCTIONS) == 6
    assert function_app.response({"status": "ok"}).status_code == 200
    with pytest.raises(RuntimeError, match="unexpected"):
        function_app.error_response(RuntimeError("unexpected"))
