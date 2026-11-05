"""Cognito access-token authentication for user APIs (api-contract decision D3).

Routes opt in with ``CurrentPrincipal`` (``Depends(require_principal)``); health
endpoints stay public.
The token is verified in the app (not only at API Gateway) so the same check runs
locally, in tests and on Lambda:

* RS256 signature against the user pool JWKS, selected by ``kid``; ``alg`` is pinned
  so ``none`` and HMAC tokens are rejected
* ``iss`` equals the user pool issuer, ``exp``/``iat`` checked with a small leeway
* ``token_use == "access"`` (ID tokens are rejected) and ``client_id`` is allowed

A missing configuration or an unreachable JWKS endpoint fails closed with 503; it
never lets a request through. Tokens and headers are never logged or echoed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request

from umsal_backend.errors import ApiError

ALGORITHM = "RS256"
LEEWAY_SECONDS = 30
JWKS_TIMEOUT_SECONDS = 3
JWKS_CACHE_SECONDS = 3600
JWKS_REFRESH_COOLDOWN_SECONDS = 30
_JWKS_FAILURES = (jwt.PyJWTError, ValueError, OSError)

# Resolves a JWKS ``kid`` to a public key. Raises KeyNotFound or JwksUnavailable.
KeyResolver = Callable[[str], Any]


class KeyNotFound(Exception):
    """The token names a key the user pool does not publish."""


class JwksUnavailable(Exception):
    """The JWKS endpoint could not be reached or returned an unusable document."""


class InvalidToken(Exception):
    """The token failed verification. The reason is internal and never returned."""


@dataclass(frozen=True)
class CognitoSettings:
    region: str
    user_pool_id: str
    client_ids: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> CognitoSettings | None:
        pool = os.environ.get("COGNITO_USER_POOL_ID", "").strip()
        clients = frozenset(
            item.strip()
            for item in os.environ.get("COGNITO_APP_CLIENT_IDS", "").split(",")
            if item.strip()
        )
        if not pool or not clients:
            return None
        region = os.environ.get("COGNITO_REGION", "").strip() or pool.split("_", 1)[0]
        return cls(region=region, user_pool_id=pool, client_ids=clients)


@dataclass(frozen=True)
class Principal:
    """Authenticated caller. ``subject`` is the stable Cognito user id (``sub``)."""

    subject: str
    username: str | None
    client_id: str
    scopes: frozenset[str]
    groups: frozenset[str]


def jwks_key_resolver(settings: CognitoSettings) -> KeyResolver:
    client = jwt.PyJWKClient(
        settings.jwks_url,
        cache_keys=True,
        lifespan=JWKS_CACHE_SECONDS,
        timeout=JWKS_TIMEOUT_SECONDS,
        # Random kids can force at most one JWKS refetch per cooldown window.
        cooldown_duration=JWKS_REFRESH_COOLDOWN_SECONDS,
    )

    def resolve(kid: str) -> Any:
        try:
            return client.get_signing_key(kid).key
        except jwt.PyJWKClientConnectionError as exc:
            raise JwksUnavailable from exc
        except jwt.PyJWKClientError as exc:
            # Unknown kid, or a JWKS without usable keys. Tell them apart by reading
            # the (cached) key set rather than parsing the library's message text.
            try:
                published = {key.key_id for key in client.get_signing_keys()}
            except _JWKS_FAILURES as inner:
                raise JwksUnavailable from inner
            if kid in published:
                raise JwksUnavailable from exc
            raise KeyNotFound from exc
        except _JWKS_FAILURES as exc:
            # Malformed JSON, invalid key set or unsupported key type from the endpoint.
            raise JwksUnavailable from exc

    return resolve


class AuthConfigError(RuntimeError):
    """Cognito settings are incomplete or invalid; raised at startup, not per request."""


def static_key_resolver(jwks_document: str) -> KeyResolver:
    """Resolve keys from a JWKS document fixed at deploy time (no network at runtime).

    Used in the VPC, where the Lambda cannot reach Cognito. An unknown ``kid`` is a
    KeyNotFound (401); after a Cognito key rotation the API must be redeployed with the
    new document (docs/aws-stage2-design.md runbook).
    """
    try:
        key_set = jwt.PyJWKSet.from_json(jwks_document)
    except _JWKS_FAILURES as exc:
        raise AuthConfigError("COGNITO_JWKS is not a valid JWKS document.") from exc
    keys = {
        key.key_id: key.key
        for key in key_set.keys
        if key.key_id and key.public_key_use in (None, "sig")
    }
    if not keys:
        raise AuthConfigError("COGNITO_JWKS contains no signing keys.")

    def resolve(kid: str) -> Any:
        try:
            return keys[kid]
        except KeyError as exc:
            raise KeyNotFound from exc

    return resolve


class TokenVerifier:
    def __init__(self, settings: CognitoSettings, resolve_key: KeyResolver | None = None) -> None:
        self.settings = settings
        self._resolve_key = resolve_key or jwks_key_resolver(settings)

    @classmethod
    def from_env(cls) -> TokenVerifier | None:
        """Verifier from the environment, or None when Cognito is not configured.

        Keys come from ``COGNITO_JWKS`` (fixed at deploy time). Fetching the JWKS URL
        at runtime needs internet access and is used only when explicitly allowed with
        ``COGNITO_JWKS_FETCH=allow`` (local development outside the VPC). There is no
        silent fallback: a missing document in the VPC fails at startup, not per request.
        """
        settings = CognitoSettings.from_env()
        if settings is None:
            return None
        document = os.environ.get("COGNITO_JWKS", "").strip()
        if document:
            return cls(settings, static_key_resolver(document))
        if os.environ.get("COGNITO_JWKS_FETCH", "").strip() == "allow":
            return cls(settings)
        raise AuthConfigError(
            "COGNITO_JWKS is required (set COGNITO_JWKS_FETCH=allow only outside the VPC)."
        )

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken from exc
        kid = header.get("kid")
        if header.get("alg") != ALGORITHM or not isinstance(kid, str) or not kid:
            raise InvalidToken
        try:
            key = self._resolve_key(kid)
        except KeyNotFound as exc:
            raise InvalidToken from exc
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self.settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                    # Cognito access tokens carry client_id, not aud.
                    "verify_aud": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken from exc
        if claims.get("token_use") != "access":
            raise InvalidToken
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or client_id not in self.settings.client_ids:
            raise InvalidToken
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidToken
        username = claims.get("username")
        groups = claims.get("cognito:groups")
        if not isinstance(groups, list):
            groups = []
        return Principal(
            subject=subject,
            username=username if isinstance(username, str) else None,
            client_id=client_id,
            scopes=frozenset(str(claims.get("scope", "")).split()),
            groups=frozenset(g for g in groups if isinstance(g, str)),
        )


def _unauthenticated() -> ApiError:
    return ApiError(
        401,
        "UNAUTHENTICATED",
        "A valid access token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str:
    value = request.headers.get("authorization")
    if not value:
        raise _unauthenticated()
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise _unauthenticated()
    return token


def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the verified caller, or a contract ``Error`` response."""
    verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "Authentication is not configured.")
    token = _bearer_token(request)
    try:
        return verifier.verify(token)
    except InvalidToken:
        raise _unauthenticated() from None
    except JwksUnavailable:
        raise ApiError(
            503,
            "TEMPORARILY_UNAVAILABLE",
            "Authentication is temporarily unavailable.",
            retryable=True,
        ) from None


# Use in route signatures: ``def handler(principal: CurrentPrincipal): ...``
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
