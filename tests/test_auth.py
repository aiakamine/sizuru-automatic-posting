"""Tests for sizuru_post.api.auth: in-Lambda Supabase JWT verification.

API Gateway HTTP API's native JWT authorizer only supports RSA (RS256/
RS512); Supabase signs its access tokens with ES256 (EC P-256), so we
verify the token ourselves inside the Lambda with PyJWT instead. No real
network/JWKS calls happen here — a fake resolver stands in for
jwt.PyJWKClient so these tests exercise the real cryptographic
verification (signature/exp/aud/iss/alg) without any I/O.
"""

from __future__ import annotations

import datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from sizuru_post.api.auth import AuthError, verify_bearer_token

ISSUER = "https://hdqrvhnhlyykocphetfg.supabase.co/auth/v1"
AUDIENCE = "authenticated"


def _generate_ec_keypair() -> tuple[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


PRIVATE_KEY, PUBLIC_KEY = _generate_ec_keypair()
OTHER_PRIVATE_KEY, _ = _generate_ec_keypair()


class _FakeSigningKey:
    def __init__(self, key: str) -> None:
        self.key = key


class _FakeJwkClient:
    """Stands in for jwt.PyJWKClient: always returns the same public key,
    regardless of the token's `kid` — good enough since these tests only
    need to exercise jwt.decode()'s own verification logic."""

    def __init__(self, key: str = PUBLIC_KEY) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._key)


def _make_token(
    *,
    sub: str = "user-1",
    aud: str = AUDIENCE,
    iss: str = ISSUER,
    exp_delta: datetime.timedelta = datetime.timedelta(hours=1),
    algorithm: str = "ES256",
    key: str = PRIVATE_KEY,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.datetime.now(datetime.UTC)
    payload: dict[str, Any] = {"sub": sub, "aud": aud, "iss": iss, "exp": now + exp_delta}
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, key, algorithm=algorithm)


def _verify(header: str | None, jwk_client: object = None) -> dict[str, Any]:
    return verify_bearer_token(
        header, issuer=ISSUER, audience=AUDIENCE, jwk_client=jwk_client or _FakeJwkClient()
    )


class TestVerifyBearerToken:
    def test_valid_token_returns_claims(self) -> None:
        # Arrange
        token = _make_token(sub="user-1")

        # Act
        claims = _verify(f"Bearer {token}")

        # Assert
        assert claims["sub"] == "user-1"
        assert claims["aud"] == AUDIENCE
        assert claims["iss"] == ISSUER

    def test_missing_header_raises(self) -> None:
        # Act / Assert
        with pytest.raises(AuthError):
            _verify(None)

    def test_header_without_bearer_prefix_raises(self) -> None:
        # Act / Assert
        with pytest.raises(AuthError):
            _verify(_make_token())  # raw token, no "Bearer " prefix

    def test_empty_bearer_token_raises(self) -> None:
        # Act / Assert
        with pytest.raises(AuthError):
            _verify("Bearer ")

    def test_expired_token_raises(self) -> None:
        # Arrange: well past the 30s leeway
        token = _make_token(exp_delta=datetime.timedelta(hours=-1))

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_wrong_audience_raises(self) -> None:
        # Arrange
        token = _make_token(aud="some-other-app")

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_wrong_issuer_raises(self) -> None:
        # Arrange
        token = _make_token(iss="https://not-our-project.supabase.co/auth/v1")

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_bad_signature_raises(self) -> None:
        # Arrange: signed with a *different* EC private key than the one
        # the (fake) JWKS resolver will hand back for verification.
        token = _make_token(key=OTHER_PRIVATE_KEY)

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_hs256_algorithm_is_rejected(self) -> None:
        # Arrange: alg confusion attack — HS256 isn't in our whitelist
        # (["ES256", "RS256"]) even if the resolver would hand back a key.
        token = _make_token(algorithm="HS256", key="some-shared-secret-that-is-long-enough-for-hs256")

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_none_algorithm_is_rejected(self) -> None:
        # Arrange: the classic "alg: none" unsigned-token attack.
        now = datetime.datetime.now(datetime.UTC)
        payload = {
            "sub": "user-1",
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": now + datetime.timedelta(hours=1),
        }
        token = jwt.encode(payload, key="", algorithm="none")

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_rs256_signed_token_is_accepted_when_resolver_returns_rsa_key(self) -> None:
        # Arrange: RS256 is also whitelisted (API Gateway's native
        # authorizer only ever supported RS256/RS512; we keep supporting it
        # in case Supabase or another issuer ever uses it).
        rsa_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rsa_private_pem = rsa_private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        rsa_public_pem = (
            rsa_private.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )
        token = _make_token(algorithm="RS256", key=rsa_private_pem)

        # Act
        claims = _verify(f"Bearer {token}", jwk_client=_FakeJwkClient(rsa_public_pem))

        # Assert
        assert claims["sub"] == "user-1"

    def test_missing_required_claim_raises(self) -> None:
        # Arrange: no exp at all.
        token = jwt.encode({"sub": "user-1", "aud": AUDIENCE, "iss": ISSUER}, PRIVATE_KEY, algorithm="ES256")

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_auth_error_message_never_leaks_raw_exception_details(self) -> None:
        # Arrange
        token = _make_token(exp_delta=datetime.timedelta(hours=-1))

        # Act
        with pytest.raises(AuthError) as exc_info:
            _verify(f"Bearer {token}")

        # Assert: a generic message only — no PyJWT internals/claim values.
        assert "expired" not in str(exc_info.value).lower()

    def test_future_nbf_raises(self) -> None:
        # Arrange: a token that isn't valid yet (nbf in the future) must be
        # rejected just like an expired one.
        now = datetime.datetime.now(datetime.UTC)
        token = _make_token(extra_claims={"nbf": now + datetime.timedelta(hours=1)})

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_far_future_iat_raises(self) -> None:
        # Arrange: PyJWT treats an implausible iat (issued-at far in the
        # future) the same as an immature token.
        now = datetime.datetime.now(datetime.UTC)
        token = _make_token(extra_claims={"iat": now + datetime.timedelta(days=365)})

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")

    def test_signing_key_resolver_error_for_unknown_kid_raises_auth_error(self) -> None:
        # Arrange: a token whose `kid` isn't in the JWKS (e.g. a rotated-out
        # signing key) makes PyJWKClient raise PyJWKClientError, which is a
        # PyJWTError subclass — verify_bearer_token must map it to the same
        # generic AuthError as any other verification failure, not let it
        # escape unhandled.
        class _RaisingJwkClient:
            def get_signing_key_from_jwt(self, token: str) -> Any:
                raise jwt.PyJWKClientError("Unable to find a signing key that matches")

        token = _make_token()

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}", jwk_client=_RaisingJwkClient())

    def test_leeway_boundary_29_seconds_past_expiry_is_accepted(self) -> None:
        # Arrange: just inside the 30s leeway window.
        token = _make_token(exp_delta=datetime.timedelta(seconds=-29))

        # Act
        claims = _verify(f"Bearer {token}")

        # Assert
        assert claims["sub"] == "user-1"

    def test_leeway_boundary_31_seconds_past_expiry_is_rejected(self) -> None:
        # Arrange: just outside the 30s leeway window.
        token = _make_token(exp_delta=datetime.timedelta(seconds=-31))

        # Act / Assert
        with pytest.raises(AuthError):
            _verify(f"Bearer {token}")
