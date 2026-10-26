from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from postprocessing.auth import AuthorizationError, authenticate, validate_claims
from postprocessing.config import Settings, settings
from postprocessing.models import BlobReference, GrayscaleOptions


def claims(config: Settings) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
        "aud": config.audience,
        "tid": config.tenant_id,
        "azp": config.allowed_client_ids[0],
        "oid": "00000000-0000-4000-8000-000000000004",
        "roles": ["Postprocess.Run"],
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }


def test_real_signature_and_access_control(
    config: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            return type("Key", (), {"key": private.public_key()})()

    monkeypatch.setattr("postprocessing.auth.signing_keys", lambda tenant: Keys())
    token = jwt.encode(claims(config), private, algorithm="RS256")
    owner = authenticate(f"Bearer {token}", config)
    assert len(owner) == 64
    bad = claims(config)
    bad["aud"] = "different"
    with pytest.raises(AuthorizationError) as error:
        authenticate(f"Bearer {jwt.encode(bad, private, algorithm='RS256')}", config)
    assert error.value.status_code == 401
    bad = claims(config)
    bad["azp"] = "different"
    with pytest.raises(AuthorizationError) as error:
        authenticate(f"Bearer {jwt.encode(bad, private, algorithm='RS256')}", config)
    assert error.value.status_code == 403
    for header in (None, "Bearer junk", "Basic abc"):
        with pytest.raises(AuthorizationError):
            authenticate(header, config)


def test_roles_and_scopes_and_owner_isolation(config: Settings) -> None:
    first = claims(config)
    second = claims(config)
    second["oid"] = "00000000-0000-4000-8000-000000000005"
    assert validate_claims(first, config) != validate_claims(second, config)
    del first["roles"]
    first["scp"] = "Other.Scope Postprocess.Run"
    assert len(validate_claims(first, config)) == 64
    del first["scp"]
    with pytest.raises(AuthorizationError):
        validate_claims(first, config)


@pytest.mark.parametrize(
    "name", ["https://example.org/a", "../a", "a/../b", "a?sig=x", "a\\b", "a//b"]
)
def test_no_arbitrary_document_urls(name: str) -> None:
    with pytest.raises(ValidationError):
        BlobReference(name=name, etag='"v1"')


def test_config_validators(config: Settings) -> None:
    for url in ("http://example.org", "https://example.org", config.blob_account_url + "?sig=x"):
        with pytest.raises(ValidationError):
            Settings.model_validate({**config.model_dump(), "blob_account_url": url})
    for overrides in (
        {"tenant_id": "bad"},
        {"allowed_client_ids": ()},
        {"input_container": "BAD"},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**config.model_dump(), **overrides})
    with pytest.raises(ValidationError):
        GrayscaleOptions(selected_threshold=150, unselected_threshold=90)
    with pytest.raises(ValidationError):
        BlobReference(name="valid/name", etag="*")


def test_load_server_side_settings(config: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "APP_BLOB_ACCOUNT_URL": config.blob_account_url,
        "APP_AUTH_TENANT_ID": config.tenant_id,
        "APP_AUTH_AUDIENCE": config.audience,
        "APP_AUTH_ALLOWED_CLIENT_IDS": config.allowed_client_ids[0],
        "APP_MAX_PAGES": "2",
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    assert settings().processors["grayscale_checkboxes"].max_pages == 2
    assert settings().processors["grayscale_checkboxes"].mode == "shadow"
    settings.cache_clear()
