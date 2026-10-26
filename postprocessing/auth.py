from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

import jwt

from postprocessing.config import Settings


class AuthorizationError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("access_denied")


@lru_cache(maxsize=4)
def signing_keys(tenant: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        cache_jwk_set=True,
        lifespan=3600,
        timeout=5,
    )


def validate_claims(claims: dict[str, Any], config: Settings) -> str:
    tenant = claims.get("tid")
    client = claims.get("azp")
    subject = claims.get("oid")
    roles = claims.get("roles", [])
    scopes = claims.get("scp", "")
    if (
        tenant != config.tenant_id
        or client not in config.allowed_client_ids
        or not isinstance(subject, str)
        or not subject
        or not isinstance(roles, list)
        or not isinstance(scopes, str)
        or (
            config.required_permission not in roles
            and config.required_permission not in scopes.split()
        )
    ):
        raise AuthorizationError(403)
    return hashlib.sha256(f"{tenant}|{client}|{subject}".encode()).hexdigest()


def authenticate(header: str | None, config: Settings) -> str:
    if not header or not header.startswith("Bearer ") or len(header) > 16_384:
        raise AuthorizationError(401)
    token = header[7:]
    try:
        key = signing_keys(config.tenant_id).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=config.audience,
            issuer=f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
            leeway=30,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "azp", "oid"]},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError) as error:
        raise AuthorizationError(401) from error
    return validate_claims(claims, config)
