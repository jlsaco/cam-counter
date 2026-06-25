"""Tests del payload MQTT del cruce (VERBATIM del contrato, fail-closed, anti-spoof).

Cubre los criterios de aceptación del WP14 sobre el payload:
- el payload ES el ``CrossingEvent`` del contrato VERBATIM (snake_case, con
  ``track_id``/``crossing_seq``/``ts_event_iso``/``schema_version``/``line_version``),
- NO lleva campos inventados (``count_delta``, ``line_config_version``),
- ``synced`` (sólo-local) NUNCA viaja,
- mapeo validado fail-closed contra ``contracts/crossing_event.schema.json``,
- anti-spoof: el ``event_id`` DEBE derivar de su tupla de identidad.
"""

from __future__ import annotations

import json

import pytest

from cam_counter_edge import compute_event_id, ms_to_iso_utc
from cam_counter_edge.crossing_payload import (
    PayloadContractError,
    crossing_event_payload,
    encode_payload,
    load_contract_schema,
)
from cam_counter_edge.types import CrossingEvent

SITE = "demo-site"
DEVICE = "demo-pi"
CAMERA = "demo-pi-cam0"


def _make_event(track_id: str = "t1", crossing_seq: int = 1, ts_ms: int = 1_700_000_000_000):
    event_id = compute_event_id(SITE, DEVICE, CAMERA, track_id, crossing_seq)
    return CrossingEvent(
        event_id=event_id,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=CAMERA,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_ms,
        ts_event_iso=ms_to_iso_utc(ts_ms),
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=3,
        confidence=0.9,
        clip_key=None,
        clip_status="pending",
        synced=0,
        created_at=ms_to_iso_utc(ts_ms),
    )


def test_payload_is_contract_verbatim() -> None:
    """El payload trae los campos del contrato en snake_case y pasa la validación."""
    payload = crossing_event_payload(_make_event())
    # Requeridos presentes.
    for field in (
        "event_id",
        "site_id",
        "device_id",
        "camera_id",
        "track_id",
        "crossing_seq",
        "direction",
        "ts_event_ms",
        "ts_event_iso",
        "schema_version",
    ):
        assert field in payload, field
    assert payload["schema_version"] == 1
    assert payload["line_version"] == 3  # line_version (NO line_config_version)
    assert isinstance(payload["track_id"], str)
    assert isinstance(payload["crossing_seq"], int)


def test_payload_omits_local_only_and_invented_fields() -> None:
    """``synced`` (sólo-local) NUNCA viaja; tampoco campos inventados."""
    payload = crossing_event_payload(_make_event())
    assert "synced" not in payload
    assert "count_delta" not in payload
    assert "line_config_version" not in payload


def test_payload_omits_optional_none_fields() -> None:
    """Los opcionales con valor ``None`` (p.ej. ``clip_key``) se OMITEN (no ``null``)."""
    payload = crossing_event_payload(_make_event())
    assert "clip_key" not in payload  # era None
    assert payload["clip_status"] == "pending"


def test_anti_spoof_rejects_forged_event_id() -> None:
    """Un ``event_id`` que NO deriva de la tupla de identidad se rechaza (fail-closed)."""
    event = _make_event()
    event.event_id = "0" * 40  # patrón válido, pero no deriva de la identidad
    with pytest.raises(PayloadContractError) as exc:
        crossing_event_payload(event)
    assert any("anti-spoof" in r for r in exc.value.reasons)


def test_fail_closed_on_bad_slug() -> None:
    """Un identificador que viola el patrón del contrato hace fallar el mapeo."""
    event = _make_event()
    # site_id con '#' viola el patrón (y rompería claves DynamoDB/S3). El event_id
    # se recomputa para aislar el fallo en el patrón (no en anti-spoof).
    event.site_id = "bad#site"
    event.event_id = compute_event_id(
        event.site_id, DEVICE, CAMERA, event.track_id, event.crossing_seq
    )
    with pytest.raises(PayloadContractError) as exc:
        crossing_event_payload(event)
    assert any("site_id" in r for r in exc.value.reasons)


def test_fail_closed_on_bad_direction_enum() -> None:
    """``direction`` fuera del enum del contrato (``in``/``out``) se rechaza."""
    event = _make_event()
    event.direction = "sideways"
    with pytest.raises(PayloadContractError) as exc:
        crossing_event_payload(event)
    assert any("direction" in r for r in exc.value.reasons)


def test_fail_closed_on_wrong_schema_version() -> None:
    """``schema_version`` distinto del ``const`` 1 del contrato se rechaza."""
    event = _make_event()
    event.schema_version = 2
    with pytest.raises(PayloadContractError) as exc:
        crossing_event_payload(event)
    assert any("schema_version" in r for r in exc.value.reasons)


def test_encode_payload_is_deterministic_compact_json() -> None:
    """``encode_payload`` serializa JSON compacto, UTF-8 y con claves ordenadas."""
    payload = crossing_event_payload(_make_event())
    blob = encode_payload(payload)
    assert isinstance(blob, bytes)
    assert b", " not in blob and b": " not in blob  # separadores compactos
    decoded = json.loads(blob)
    assert decoded == payload
    # Determinista: misma entrada -> mismos bytes.
    assert encode_payload(payload) == blob


def test_payload_validates_against_real_contract_schema() -> None:
    """El payload válido pasa el schema canónico cargado del repo ``contracts/``."""
    from cam_counter_edge.crossing_payload import validate_against_contract

    schema = load_contract_schema()
    payload = crossing_event_payload(_make_event())
    assert validate_against_contract(payload, schema) == []
