"""Tests del handler `cam-counter-clip-presign`.

Valida la firma SigV4 GET (TTL 300s) y, sobre todo, la defensa de path traversal: sólo keys
ancladas a `media/` y sin `..`/barra inicial/doble barra/backslash. La firma es LOCAL (SigV4): no
requiere AWS real, basta con credenciales ficticias en el entorno.
"""

from __future__ import annotations

import importlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

VALID_KEY = "media/sitio-demo/rpi-001/rpi-001-cam0/2026/06/23/" + "a" * 40 + ".jpg"


@pytest.fixture()
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MEDIA_BUCKET", "cam-counter-media-950639281773")
    monkeypatch.setenv("PRESIGN_TTL", "300")
    import handler as mod

    importlib.reload(mod)
    return mod


def _invoke(mod, key=None, route_key="GET /clips/url"):
    qs = {"key": key} if key is not None else None
    resp = mod.lambda_handler({"routeKey": route_key, "queryStringParameters": qs})
    return resp["statusCode"], json.loads(resp["body"])


def test_presign_ok(handler):
    status, body = _invoke(handler, VALID_KEY)
    assert status == 200
    assert body["key"] == VALID_KEY
    assert body["expires_in"] == 300
    parsed = urlparse(body["url"])
    assert "cam-counter-media-950639281773" in parsed.netloc
    assert VALID_KEY in body["url"]
    params = parse_qs(parsed.query)
    assert params["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Signature" in params  # firma SigV4 presente


@pytest.mark.parametrize(
    "bad_key",
    [
        "media/../../etc/passwd",
        "media/a/../../../secret.jpg",
        "/media/abs/path.jpg",
        "media//double/slash.jpg",
        "media\\windows\\path.jpg",
        "clips/outside/prefix.jpg",
        "media/no-extension",
        "",
    ],
)
def test_presign_rejects_bad_keys(handler, bad_key):
    status, body = _invoke(handler, bad_key)
    assert status == 400
    assert "error" in body


def test_presign_missing_key(handler):
    status, body = _invoke(handler, key=None)
    assert status == 400


def test_unknown_route(handler):
    status, _ = _invoke(handler, VALID_KEY, route_key="POST /clips/url")
    assert status == 404
