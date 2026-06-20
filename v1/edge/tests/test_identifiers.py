"""Validación de slugs (identifiers): aceptación y rechazo explícitos."""

from __future__ import annotations

import pytest

from cam_counter_edge.identifiers import (
    InvalidSlugError,
    is_valid_slug,
    make_camera_id,
    validate_camera_id,
    validate_device_id,
    validate_site_id,
    validate_slug,
)

ACCEPTED = [
    "site-1",
    "dev01",
    "dev01-cam0",
    "a1",
    "0a",
    "x" * 63,  # longitud máxima exacta
]

REJECTED = [
    "with#hash",  # '#' delimita claves DynamoDB
    "with/slash",  # '/' delimita rutas S3
    "WithUpper",  # mayúsculas
    "",  # vacío
    "a",  # demasiado corto (regex exige >= 2)
    "-leading",  # no puede empezar por '-'
    "x" * 64,  # longitud > 63
    "with space",
    "tilde~",
]


@pytest.mark.parametrize("slug", ACCEPTED)
def test_slug_accepts_valid_identifiers(slug: str) -> None:
    assert is_valid_slug(slug) is True
    assert validate_slug(slug) == slug


@pytest.mark.parametrize("slug", REJECTED)
def test_slug_rejects_invalid_identifiers(slug: str) -> None:
    assert is_valid_slug(slug) is False
    with pytest.raises(InvalidSlugError):
        validate_slug(slug)


def test_slug_rejects_non_string_identifier() -> None:
    assert is_valid_slug(123) is False
    assert is_valid_slug(None) is False
    with pytest.raises(InvalidSlugError):
        validate_slug(None)


def test_per_field_validators_round_trip() -> None:
    assert validate_site_id("site-1") == "site-1"
    assert validate_device_id("dev01") == "dev01"
    assert validate_camera_id("dev01-cam0") == "dev01-cam0"


def test_make_camera_id_builds_and_validates() -> None:
    assert make_camera_id("dev01", 0) == "dev01-cam0"
    assert make_camera_id("pi-lobby-01", 2) == "pi-lobby-01-cam2"


def test_make_camera_id_rejects_bad_inputs() -> None:
    with pytest.raises(InvalidSlugError):
        make_camera_id("Bad_Device", 0)  # device_id inválido
    with pytest.raises(InvalidSlugError):
        make_camera_id("dev01", -1)  # índice negativo
    with pytest.raises(InvalidSlugError):
        make_camera_id("dev01", True)  # bool no es un índice válido


def test_device_id_and_camera_id_are_separate_fields() -> None:
    # device_id y camera_id se almacenan SEPARADOS; aquí sólo verificamos que
    # ambos validan como slugs independientes (nunca se reconstruye uno del otro
    # por split, por eso no existe una función inversa).
    device_id = "pi-lobby-01"
    camera_id = make_camera_id(device_id, 1)
    assert validate_device_id(device_id) == device_id
    assert validate_camera_id(camera_id) == camera_id
    assert camera_id != device_id
