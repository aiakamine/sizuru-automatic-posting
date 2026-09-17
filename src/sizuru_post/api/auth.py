"""Verifies the admin dashboard's Supabase-issued JWT ourselves, inside the
Lambda, with PyJWT.

Why not API Gateway HTTP API's native JWT authorizer? It only supports RSA
(RS256/RS512) signatures, but Supabase Auth signs access tokens with ES256
(EC P-256) — the authorizer would reject every real token. See
infra/sam/README.md for the full story; this module (plus the
`_authenticate` wiring in api/handler.py) replaces it.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import jwt

logger = logging.getLogger(__name__)

# Whitelisting both explicitly (rather than trusting the token's own `alg`
# header) is what actually blocks alg-confusion attacks (HS256 using the
# public key as an HMAC secret, "alg: none", ...) — PyJWT rejects any token
# whose header `alg` isn't in this list before it even looks at the key.
_ALGORITHMS = ["ES256", "RS256"]
_LEEWAY_SECONDS = 30
_BEARER_PREFIX = "Bearer "


class AuthError(Exception):
    """Missing/invalid/expired/malformed token. Always carries a generic
    message — real PyJWT details are only logged, never returned to the
    client (which could otherwise leak claim values or timing info)."""


class SigningKeyResolver(Protocol):
    """Matches the one jwt.PyJWKClient method this module actually calls,
    so tests can inject a fake resolver instead of hitting a real JWKS
    endpoint."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


def build_jwk_client(issuer: str) -> jwt.PyJWKClient:
    """One per container (see api/handler.py's module-level caching):
    PyJWKClient caches fetched keys in-process for `lifespan` seconds, so
    most invocations don't refetch the JWKS at all."""
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=600)


def verify_bearer_token(
    authorization_header: str | None,
    *,
    issuer: str,
    audience: str,
    jwk_client: SigningKeyResolver,
) -> dict[str, Any]:
    """Verify an `Authorization: Bearer <token>` header's JWT and return
    its claims, or raise AuthError."""
    if not authorization_header or not authorization_header.startswith(_BEARER_PREFIX):
        raise AuthError("missing or malformed Authorization header")
    token = authorization_header[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise AuthError("missing bearer token")

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "aud", "iss"]},
            leeway=_LEEWAY_SECONDS,
        )
    except jwt.PyJWTError as exc:
        logger.warning("JWT検証に失敗しました: %s", exc)
        raise AuthError("invalid token") from exc
    return claims
