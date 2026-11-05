from __future__ import annotations

import io
import json
import time
from unittest.mock import patch
from urllib.error import URLError

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from umsal_backend.app import create_app
from umsal_backend.auth import (
    AuthConfigError,
    CognitoSettings,
    CurrentPrincipal,
    JwksUnavailable,
    KeyNotFound,
    TokenVerifier,
    jwks_key_resolver,
    static_key_resolver,
)

POOL = "ap-southeast-2_SYNTHETIC"
CLIENT = "synthetic-client"
SETTINGS = CognitoSettings(
    region="ap-southeast-2", user_pool_id=POOL, client_ids=frozenset({CLIENT})
)
KID = "synthetic-kid"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _resolver(kid: str):
    if kid == KID:
        return _KEY.public_key()
    raise KeyNotFound


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "00000000-0000-4000-8000-000000000001",
        "iss": SETTINGS.issuer,
        "client_id": CLIENT,
        "token_use": "access",
        "scope": "openid profile",
        "username": "synthetic-user",
        "cognito:groups": ["family"],
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims=None, *, key=_KEY, kid=KID, algorithm="RS256"):
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims or _claims(), key, algorithm=algorithm, headers=headers)


def _client(verifier=None, *, configured=True):
    if configured and verifier is None:
        verifier = TokenVerifier(SETTINGS, _resolver)
    app = create_app(token_verifier=verifier if configured else None)

    @app.get("/_test/whoami")
    def whoami(principal: CurrentPrincipal):
        return {
            "subject": principal.subject,
            "username": principal.username,
            "client_id": principal.client_id,
            "scopes": sorted(principal.scopes),
            "groups": sorted(principal.groups),
        }

    return TestClient(app)


def _get(client, token=None, *, header=None):
    headers = {}
    if header is not None:
        headers["Authorization"] = header
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.get("/_test/whoami", headers=headers)


def _assert_unauthenticated(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["code"] == "UNAUTHENTICATED"
    assert body["retryable"] is False
    assert set(body) == {"code", "message", "request_id", "retryable"}


def test_valid_access_token_yields_principal():
    response = _get(_client(), _token())
    assert response.status_code == 200
    assert response.json() == {
        "subject": "00000000-0000-4000-8000-000000000001",
        "username": "synthetic-user",
        "client_id": CLIENT,
        "scopes": ["openid", "profile"],
        "groups": ["family"],
    }


def test_bearer_scheme_is_case_insensitive():
    assert _get(_client(), header=f"bearer {_token()}").status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic abc", "Bearer", "Bearer ", "Bearer a b", "Token abc"],
)
def test_missing_or_malformed_header_is_401(header):
    client = _client()
    response = client.get("/_test/whoami") if header is None else _get(client, header=header)
    _assert_unauthenticated(response)


@pytest.mark.parametrize(
    "claims",
    [
        _claims(exp=int(time.time()) - 120),  # expired beyond leeway
        _claims(iss="https://cognito-idp.ap-southeast-2.amazonaws.com/other-pool"),
        _claims(token_use="id"),  # ID token instead of access token
        _claims(client_id="other-client"),
        _claims(sub=None),
        _claims(token_use=None),
        _claims(client_id=None),
        _claims(exp=None),
        _claims(sub=""),
    ],
    ids=[
        "expired",
        "wrong-issuer",
        "id-token",
        "unknown-client",
        "no-sub",
        "no-token-use",
        "no-client-id",
        "no-exp",
        "empty-sub",
    ],
)
def test_invalid_claims_are_401(claims):
    _assert_unauthenticated(_get(_client(), _token(claims)))


def test_expiry_within_leeway_is_accepted():
    assert _get(_client(), _token(_claims(exp=int(time.time()) - 5))).status_code == 200


def test_signature_from_other_key_is_401():
    _assert_unauthenticated(_get(_client(), _token(key=_OTHER_KEY)))


def test_tampered_payload_is_401():
    header, payload, signature = _token().split(".")
    forged = _token(_claims(sub="00000000-0000-4000-8000-000000000002")).split(".")[1]
    _assert_unauthenticated(_get(_client(), f"{header}.{forged}x.{signature}"))
    _assert_unauthenticated(_get(_client(), f"{header}.{payload}.{signature[:-4]}AAAA"))


def test_unknown_kid_and_missing_kid_are_401():
    _assert_unauthenticated(_get(_client(), _token(kid="rotated-away")))
    _assert_unauthenticated(_get(_client(), _token(kid=None)))


def test_hmac_and_none_algorithms_are_rejected():
    hs = jwt.encode(
        _claims(), "synthetic-shared-secret-32-bytes!", algorithm="HS256", headers={"kid": KID}
    )
    _assert_unauthenticated(_get(_client(), hs))
    unsigned = jwt.encode(_claims(), None, algorithm="none", headers={"kid": KID})
    _assert_unauthenticated(_get(_client(), unsigned))


def test_garbage_token_is_401():
    _assert_unauthenticated(_get(_client(), "not-a-jwt"))


def test_unconfigured_auth_fails_closed_with_503():
    response = _get(_client(configured=False), _token())
    assert response.status_code == 503
    assert response.json()["code"] == "TEMPORARILY_UNAVAILABLE"
    assert response.json()["retryable"] is False


def test_jwks_outage_is_retryable_503():
    def unavailable(_kid):
        raise JwksUnavailable

    response = _get(_client(TokenVerifier(SETTINGS, unavailable)), _token())
    assert response.status_code == 503
    assert response.json() | {"request_id": "x"} == {
        "code": "TEMPORARILY_UNAVAILABLE",
        "message": "Authentication is temporarily unavailable.",
        "request_id": "x",
        "retryable": True,
    }


def test_token_is_never_echoed_or_logged(caplog):
    token = _token(_claims(client_id="other-client"))
    response = _get(_client(), token)
    assert token not in response.text
    assert token not in caplog.text


def test_health_stays_public_when_auth_is_configured():
    client = _client()
    assert client.get("/health").status_code == 200
    assert client.get("/api/health/live").status_code == 200


def test_settings_from_env(monkeypatch):
    monkeypatch.delenv("COGNITO_REGION", raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_APP_CLIENT_IDS", f" {CLIENT} , second ,")
    settings = CognitoSettings.from_env()
    assert settings == CognitoSettings("ap-southeast-2", POOL, frozenset({CLIENT, "second"}))
    assert settings.jwks_url == (
        f"https://cognito-idp.ap-southeast-2.amazonaws.com/{POOL}/.well-known/jwks.json"
    )
    monkeypatch.setenv("COGNITO_REGION", "us-west-2")
    assert CognitoSettings.from_env().region == "us-west-2"


@pytest.mark.parametrize(("pool", "clients"), [("", CLIENT), (POOL, ""), (POOL, " , ")])
def test_settings_absent_when_incomplete(monkeypatch, pool, clients):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", pool)
    monkeypatch.setenv("COGNITO_APP_CLIENT_IDS", clients)
    assert CognitoSettings.from_env() is None
    assert TokenVerifier.from_env() is None


def test_app_reads_verifier_from_env(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_APP_CLIENT_IDS", CLIENT)
    monkeypatch.setenv("COGNITO_JWKS_FETCH", "allow")
    assert isinstance(create_app().state.token_verifier, TokenVerifier)
    monkeypatch.delenv("COGNITO_USER_POOL_ID")
    assert create_app().state.token_verifier is None


def _jwks(*kids, key=_KEY):
    entries = []
    for kid in kids:
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        entries.append(jwk | {"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": entries}


class _FakeOpener:
    """Stands in for urllib's opener so PyJWKClient's own fetch/cache/cooldown code runs."""

    def __init__(self, document):
        self.document = document
        self.calls = 0

    def open(self, _request, timeout):
        assert timeout == 3
        self.calls += 1
        value = self.document["value"]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return io.BytesIO(body)


@pytest.fixture
def jwks_endpoint():
    document = {"value": _jwks(KID)}
    opener = _FakeOpener(document)
    with patch("jwt.jwks_client.urllib.request.build_opener", return_value=opener):
        yield document, opener


def _real_client():
    return _client(TokenVerifier(SETTINGS, jwks_key_resolver(SETTINGS)))


def test_real_jwks_client_verifies_token_end_to_end(jwks_endpoint):
    response = _get(_real_client(), _token())
    assert response.status_code == 200
    assert response.json()["subject"] == "00000000-0000-4000-8000-000000000001"


def test_real_jwks_client_unknown_kid_is_401_and_refetch_is_rate_limited(jwks_endpoint):
    _document, opener = jwks_endpoint
    client = _real_client()
    for _ in range(5):
        _assert_unauthenticated(_get(client, _token(kid="random-kid")))
    # One initial fetch; refetches for unknown kids wait for the 30s cooldown.
    assert opener.calls == 1


@pytest.mark.parametrize(
    "failure",
    [
        {"keys": []},
        {"not-keys": True},
        {"keys": [_jwks(KID)["keys"][0] | {"use": "enc"}]},
        b"<html>not json</html>",
        URLError("timed out"),
    ],
    ids=["empty", "malformed", "no-signing-keys", "not-json", "unreachable"],
)
def test_real_jwks_client_broken_endpoint_is_503(jwks_endpoint, failure):
    document, _opener = jwks_endpoint
    document["value"] = failure
    response = _get(_real_client(), _token())
    assert response.status_code == 503
    assert response.json()["code"] == "TEMPORARILY_UNAVAILABLE"
    assert response.json()["retryable"] is True


def test_jwks_client_is_configured_with_timeout_cache_and_cooldown():
    with patch("umsal_backend.auth.jwt.PyJWKClient") as client_cls:
        jwks_key_resolver(SETTINGS)
    client_cls.assert_called_once_with(
        SETTINGS.jwks_url, cache_keys=True, lifespan=3600, timeout=3, cooldown_duration=30
    )


def test_kid_listed_but_unusable_is_503():
    with patch("umsal_backend.auth.jwt.PyJWKClient") as client_cls:
        client = client_cls.return_value
        client.get_signing_key.side_effect = jwt.PyJWKClientError("unexpected")
        client.get_signing_keys.return_value = [type("K", (), {"key_id": "kid"})()]
        with pytest.raises(JwksUnavailable):
            jwks_key_resolver(SETTINGS)("kid")


@pytest.mark.parametrize(
    "claims",
    [
        _claims(iat=int(time.time()) + 600),
        _claims(nbf=int(time.time()) + 600),
        _claims(client_id=[CLIENT]),
    ],
    ids=["future-iat", "future-nbf", "client-id-list"],
)
def test_more_invalid_claims_are_401(claims):
    _assert_unauthenticated(_get(_client(), _token(claims)))


def test_non_list_groups_are_ignored():
    response = _get(_client(), _token(_claims(**{"cognito:groups": 7})))
    assert response.status_code == 200
    assert response.json()["groups"] == []


def test_unconfigured_auth_is_503_even_without_header():
    response = _client(configured=False).get("/_test/whoami")
    assert response.status_code == 503


# --- static JWKS (stage 2, Lambda in the VPC) -----------------------------------------


def _jwks_json(*kids: str, use: str = "sig") -> str:
    keys = [
        RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True) | {"kid": kid, "use": use}
        for kid in kids
    ]
    return json.dumps({"keys": keys})


def test_static_jwks_verifies_without_network(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_APP_CLIENT_IDS", CLIENT)
    monkeypatch.setenv("COGNITO_JWKS", _jwks_json("other-kid", KID))
    with patch("umsal_backend.auth.jwt.PyJWKClient") as fetcher:
        verifier = TokenVerifier.from_env()
        assert verifier.verify(_token()).subject == "00000000-0000-4000-8000-000000000001"
    fetcher.assert_not_called()


def test_static_jwks_unknown_kid_is_401():
    client = _client(TokenVerifier(SETTINGS, static_key_resolver(_jwks_json("other-kid"))))
    _assert_unauthenticated(_get(client, _token()))


def test_missing_jwks_in_configured_pool_fails_at_startup(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_APP_CLIENT_IDS", CLIENT)
    with pytest.raises(AuthConfigError):
        TokenVerifier.from_env()
    with pytest.raises(AuthConfigError):
        create_app()


@pytest.mark.parametrize(
    "document",
    ["not json", '{"keys": []}', _jwks_json(KID, use="enc")],
    ids=["not-json", "empty", "encryption-only"],
)
def test_invalid_static_jwks_fails_at_startup(document):
    with pytest.raises(AuthConfigError):
        static_key_resolver(document)
