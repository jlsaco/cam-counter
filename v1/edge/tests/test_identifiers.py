"""Validación de slugs (identifiers): acepta válidos y RECHAZA inválidos."""

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


def test_valid_slugs_are_accepted():
    valid = ["site-1", "dev01", "dev01-cam0", "a1", "0ab", "x" * 63]
    for s in valid:
        assert is_valid_slug(s) is True
        assert validate_slug(s) == s


def test_invalid_slugs_are_rejected():
    invalid = [
        "with#hash",  # '#' prohibido (delimita PK/SK de DynamoDB)
        "with/slash",  # '/' prohibido (delimita rutas S3)
        "WithUpper",  # mayúsculas prohibidas
        "",  # vacío
        "x" * 64,  # longitud > 63
        "a",  # demasiado corto para el regex (longitud mínima 2)
        "-leading",  # no puede empezar por '-'
        "has space",  # espacios prohibidos
        None,  # no-string
    ]
    for s in invalid:
        assert is_valid_slug(s) is False
        with pytest.raises(InvalidSlugError):
            validate_slug(s)


def test_site_device_camera_validators():
    assert validate_site_id("site-1") == "site-1"
    assert validate_device_id("dev01") == "dev01"
    assert validate_camera_id("dev01-cam0") == "dev01-cam0"
    with pytest.raises(InvalidSlugError):
        validate_device_id("Dev01")  # mayúscula
    with pytest.raises(InvalidSlugError):
        validate_camera_id("dev01/cam0")  # '/'


def test_make_camera_id_builds_valid_identifier():
    assert make_camera_id("dev01", 0) == "dev01-cam0"
    assert make_camera_id("dev01", 2) == "dev01-cam2"
    # device_id inválido -> rechazo antes de construir la clave.
    with pytest.raises(InvalidSlugError):
        make_camera_id("Dev01", 0)
    with pytest.raises(InvalidSlugError):
        make_camera_id("dev#01", 0)
    # índice inválido (negativo o no-int).
    with pytest.raises(InvalidSlugError):
        make_camera_id("dev01", -1)
    with pytest.raises(InvalidSlugError):
        make_camera_id("dev01", True)  # bool no cuenta como índice


def test_device_id_is_not_reconstructed_from_camera_id_split():
    """device_id y camera_id son campos SEPARADOS; no se reconstruye por split.

    Un split ingenuo de camera_id por '-cam' es frágil porque device_id puede contener
    '-'. Este test documenta el invariante: se valida y se persiste cada uno por su
    cuenta. make_camera_id NO devuelve el device_id ni obliga a derivarlo del camera_id.
    """
    device_id = "store-front-pi"  # device_id con guiones
    camera_id = make_camera_id(device_id, 1)
    assert camera_id == "store-front-pi-cam1"
    # Ambos se validan independientemente como campos separados.
    assert validate_device_id(device_id) == device_id
    assert validate_camera_id(camera_id) == camera_id
    # Un split ingenuo por '-' NO recupera el device_id (demuestra por qué está prohibido).
    assert camera_id.split("-")[0] != device_id
