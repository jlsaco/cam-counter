"""Builders de claves + anti-spoof (event_id determinista, clip_key acotado)."""

from __future__ import annotations

import hashlib

import pytest
from keys import build_keys, device_pk, recompute_event_id, validate_clip_key, validate_slug


def _event(**over: object) -> dict:
    ev = {
        "site_id": "casa",
        "device_id": "rpi5-puerta",
        "camera_id": "rpi5-puerta-cam1",
        "track_id": "t-0007",
        "crossing_seq": 42,
        "ts_event_ms": 1718900000000,
        "event_id": "dced058e990a343d94fdf5296ad81ce51b6c4eb7",
    }
    ev.update(over)
    return ev


def test_event_id_matches_contract_formula() -> None:
    ev = _event()
    raw = f"{ev['site_id']}|{ev['device_id']}|{ev['camera_id']}|{ev['track_id']}|{ev['crossing_seq']}"
    assert recompute_event_id(
        ev["site_id"], ev["device_id"], ev["camera_id"], ev["track_id"], ev["crossing_seq"]
    ) == hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324


def test_keys_shape_matches_edge_and_contract() -> None:
    keys = build_keys(_event())
    assert keys["PK"] == "CAM#casa#rpi5-puerta#rpi5-puerta-cam1"
    assert keys["SK"] == "TS#1718900000000#dced058e990a343d94fdf5296ad81ce51b6c4eb7"
    assert keys["GSI1PK"] == "SITE#casa"
    assert keys["GSI1SK"] == keys["SK"]


def test_ts_event_ms_zero_padded_to_13() -> None:
    keys = build_keys(_event(ts_event_ms=42))
    assert keys["SK"].startswith("TS#0000000000042#")


def test_slug_rejects_hash_and_slash() -> None:
    for bad in ("rpi5#puerta", "a/b", "UPPER", ""):
        with pytest.raises(ValueError):
            validate_slug("device_id", bad)


def test_device_pk() -> None:
    assert device_pk("rpi-001") == "DEVICE#rpi-001"


def test_clip_key_within_identity_ok() -> None:
    ev = _event()
    key = (
        "media/casa/rpi5-puerta/rpi5-puerta-cam1/2024/06/20/"
        "dced058e990a343d94fdf5296ad81ce51b6c4eb7.mp4"
    )
    validate_clip_key(key, ev)  # no lanza


def test_clip_key_wrong_camera_rejected() -> None:
    ev = _event()
    key = (
        "media/casa/rpi5-puerta/OTRA-cam9/2024/06/20/"
        "dced058e990a343d94fdf5296ad81ce51b6c4eb7.mp4"
    )
    with pytest.raises(ValueError):
        validate_clip_key(key, ev)


def test_clip_key_wrong_event_id_rejected() -> None:
    ev = _event()
    key = "media/casa/rpi5-puerta/rpi5-puerta-cam1/2024/06/20/deadbeef.mp4"
    with pytest.raises(ValueError):
        validate_clip_key(key, ev)
