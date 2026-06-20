"""Los tipos compartidos coinciden con los nombres de campo de ``contracts/``.

Verifica que ``CrossingEvent`` y ``LineConfig`` (dataclasses) usan EXACTAMENTE
los nombres de propiedad de los JSON Schema canónicos, evitando drift de
esquema entre el código edge y los contratos.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from cam_counter_edge.types import CrossingEvent, LineConfig

# Sube desde tests/ -> edge/ -> v1/ -> raíz del repo, donde vive contracts/.
CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"


def _schema_properties(filename: str) -> set[str]:
    path = CONTRACTS_DIR / filename
    if not path.exists():
        pytest.skip(f"contracts/{filename} no disponible en este checkout")
    schema = json.loads(path.read_text(encoding="utf-8"))
    return set(schema["properties"].keys())


def _dataclass_fields(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


def test_crossing_event_fields_match_contract() -> None:
    props = _schema_properties("crossing_event.schema.json")
    dc_fields = _dataclass_fields(CrossingEvent)
    # Coincidencia EXACTA de nombres de campo con las propiedades del schema.
    assert dc_fields == props


def test_crossing_event_required_fields_present() -> None:
    path = CONTRACTS_DIR / "crossing_event.schema.json"
    if not path.exists():
        pytest.skip("contracts no disponible")
    schema = json.loads(path.read_text(encoding="utf-8"))
    required = set(schema["required"])
    dc_fields = _dataclass_fields(CrossingEvent)
    assert required <= dc_fields


def test_line_config_fields_match_contract() -> None:
    props = _schema_properties("line_config.schema.json")
    dc_fields = _dataclass_fields(LineConfig)
    # Coincidencia EXACTA de nombres de campo de nivel superior.
    assert dc_fields == props


def test_crossing_event_default_schema_version_is_1() -> None:
    ev = CrossingEvent(
        event_id="0" * 40,
        site_id="site-1",
        device_id="dev01",
        camera_id="dev01-cam0",
        track_id="t-1",
        crossing_seq=0,
        direction="in",
        ts_event_ms=0,
        ts_event_iso="1970-01-01T00:00:00Z",
    )
    assert ev.schema_version == 1
