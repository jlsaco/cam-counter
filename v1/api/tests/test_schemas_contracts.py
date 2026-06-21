"""Verifica que los modelos Pydantic NO derivan de los JSON Schema de contracts/.

El conjunto de NOMBRES DE CAMPO de ``CrossingEvent`` y ``LineConfig`` debe
coincidir EXACTAMENTE con las propiedades del schema canónico correspondiente; un
drift (rename/alta/baja de campo) rompe el test (es BREAKING y exige bump de
``schema_version``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas import CrossingEvent, LineConfig

_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _schema_props(filename: str) -> set[str]:
    data = json.loads((_CONTRACTS / filename).read_text(encoding="utf-8"))
    return set(data["properties"].keys())


def test_crossing_event_fields_match_contract() -> None:
    model_fields = set(CrossingEvent.model_fields.keys())
    contract_props = _schema_props("crossing_event.schema.json")
    assert model_fields == contract_props


def test_line_config_fields_match_contract() -> None:
    model_fields = set(LineConfig.model_fields.keys())
    contract_props = _schema_props("line_config.schema.json")
    assert model_fields == contract_props


def test_crossing_event_key_fields_and_direction() -> None:
    for field in ("event_id", "direction", "clip_status", "schema_version"):
        assert field in CrossingEvent.model_fields

    # 'direction' sólo admite los valores de cable/almacenado 'in'|'out'.
    base = {
        "event_id": "0" * 40,
        "site_id": "demo-site",
        "device_id": "demo-pi",
        "camera_id": "demo-pi-cam0",
        "track_id": "1",
        "crossing_seq": 1,
        "ts_event_ms": 1_700_000_000_000,
        "ts_event_iso": "2023-11-14T22:13:20.000Z",
        "schema_version": 1,
    }
    assert CrossingEvent.model_validate({**base, "direction": "in"}).direction == "in"
    assert CrossingEvent.model_validate({**base, "direction": "out"}).direction == "out"
    with pytest.raises(ValidationError):
        CrossingEvent.model_validate({**base, "direction": "sideways"})


def test_line_config_positive_side_enum() -> None:
    good = {
        "site_id": "demo-site",
        "device_id": "demo-pi",
        "camera_id": "demo-pi-cam0",
        "config_version": 0,
        "line": {"a": {"x": 0.5, "y": 0.1}, "b": {"x": 0.5, "y": 0.9}},
        "positive_side": 1,
        "schema_version": 1,
    }
    assert LineConfig.model_validate(good).positive_side == 1
    with pytest.raises(ValidationError):
        LineConfig.model_validate({**good, "positive_side": 0})
