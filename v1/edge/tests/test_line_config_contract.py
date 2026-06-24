"""Tests de validación VERBATIM del ``desired`` del shadow contra el contrato.

Cubren en x86 (sin red) que ``line_config_from_document`` valida VERBATIM contra
``contracts/line_config.schema.json`` (fail-closed) ANTES de construir el
``LineConfig``: required, additionalProperties:false, patrón de slug, geometría
anidada (min/max 0..1), ``positive_side`` enum, ``schema_version`` const=1 y
tipos. Y que ``line_config_to_document`` produce un documento que vuelve a casar
el contrato (round-trip), base de que ``reported == desired`` converja.
"""

from __future__ import annotations

import pytest

from cam_counter_edge.line_config_contract import (
    LineConfigContractError,
    line_config_from_document,
    line_config_to_document,
    load_line_config_schema,
    validate_document,
)

SCHEMA = load_line_config_schema()

SITE = "site-a"
DEVICE = "pi-001"
CAMERA = "pi-001-cam0"


def _doc(**over) -> dict:
    """Documento ``LineConfig`` válido base; ``over`` sobreescribe campos top-level."""
    doc = {
        "site_id": SITE,
        "device_id": DEVICE,
        "camera_id": CAMERA,
        "config_version": 3,
        "line": {"a": {"x": 0.5, "y": 0.0}, "b": {"x": 0.5, "y": 1.0}},
        "positive_side": 1,
        "positive_label": "subieron",
        "negative_label": "bajaron",
        "schema_version": 1,
    }
    doc.update(over)
    return doc


def test_valid_document_parses() -> None:
    cfg = line_config_from_document(_doc(), schema=SCHEMA)
    assert cfg.camera_id == CAMERA
    assert cfg.config_version == 3
    assert cfg.positive_side == 1
    assert (cfg.line.a.x, cfg.line.a.y) == (0.5, 0.0)
    assert (cfg.line.b.x, cfg.line.b.y) == (0.5, 1.0)


def test_rejects_additional_property() -> None:
    # 'min_confidence' NO existe en el contrato (nota del revisor): debe rechazarse.
    reasons = validate_document(_doc(min_confidence=0.4), SCHEMA)
    assert any("min_confidence" in r for r in reasons)
    with pytest.raises(LineConfigContractError):
        line_config_from_document(_doc(min_confidence=0.4), schema=SCHEMA)


def test_rejects_missing_required() -> None:
    doc = _doc()
    del doc["config_version"]  # campo REQUERIDO por el contrato
    with pytest.raises(LineConfigContractError) as exc:
        line_config_from_document(doc, schema=SCHEMA)
    assert any("config_version" in r for r in exc.value.reasons)


def test_rejects_bad_slug_pattern() -> None:
    with pytest.raises(LineConfigContractError) as exc:
        line_config_from_document(_doc(camera_id="Bad/Camera"), schema=SCHEMA)
    assert any("camera_id" in r for r in exc.value.reasons)


def test_rejects_nested_geometry_out_of_range() -> None:
    # y=1.5 viola maximum=1 del endpoint anidado (validación RECURSIVA).
    bad = _doc(line={"a": {"x": 0.5, "y": 1.5}, "b": {"x": 0.5, "y": 1.0}})
    reasons = validate_document(bad, SCHEMA)
    assert any("line.a.y" in r and "máximo" in r for r in reasons)
    with pytest.raises(LineConfigContractError):
        line_config_from_document(bad, schema=SCHEMA)


def test_rejects_nested_missing_point_field() -> None:
    bad = _doc(line={"a": {"x": 0.5}, "b": {"x": 0.5, "y": 1.0}})  # falta a.y
    reasons = validate_document(bad, SCHEMA)
    assert any("line.a" in r and "y" in r for r in reasons)


def test_rejects_bad_positive_side_enum() -> None:
    with pytest.raises(LineConfigContractError) as exc:
        line_config_from_document(_doc(positive_side=0), schema=SCHEMA)
    assert any("positive_side" in r for r in exc.value.reasons)


def test_rejects_wrong_schema_version_const() -> None:
    with pytest.raises(LineConfigContractError) as exc:
        line_config_from_document(_doc(schema_version=2), schema=SCHEMA)
    assert any("schema_version" in r for r in exc.value.reasons)


def test_rejects_non_object() -> None:
    assert validate_document([1, 2, 3], SCHEMA)  # no es objeto
    with pytest.raises(LineConfigContractError):
        line_config_from_document("nope", schema=SCHEMA)


def test_round_trip_to_document_matches_contract() -> None:
    cfg = line_config_from_document(_doc(), schema=SCHEMA)
    doc = line_config_to_document(cfg)
    # El documento reconstruido vuelve a validar VERBATIM (reported == desired).
    assert validate_document(doc, SCHEMA) == []
    cfg2 = line_config_from_document(doc, schema=SCHEMA)
    assert cfg2.config_version == cfg.config_version
    assert cfg2.positive_side == cfg.positive_side
    assert cfg2.line.a.x == cfg.line.a.x
