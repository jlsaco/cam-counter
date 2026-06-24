"""Paridad del validador stdlib con el contrato canónico (verbatim, fail-closed).

Valida contra el MISMO schema ``contracts/crossing_event.schema.json`` y los
MISMOS ejemplos que el gate de WP02 (``tests/contracts/examples/crossing_event``),
demostrando que el validador autocontenido acepta lo válido y rechaza lo inválido
por su motivo (campos inventados, required ausente, pattern, schema_version).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from contract import ContractError, load_schema, validate_crossing_event

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = _REPO_ROOT / "contracts" / "crossing_event.schema.json"
_EXAMPLES = _REPO_ROOT / "tests" / "contracts" / "examples" / "crossing_event"

_SCHEMA = load_schema(_SCHEMA_PATH)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_examples() -> list[Path]:
    return sorted((_EXAMPLES / "valid").glob("*.json"))


def _invalid_examples() -> list[Path]:
    return sorted((_EXAMPLES / "invalid").glob("*.json"))


@pytest.mark.parametrize("path", _valid_examples(), ids=lambda p: p.name)
def test_valid_examples_pass(path: Path) -> None:
    validate_crossing_event(_load(path), _SCHEMA)  # no debe lanzar


@pytest.mark.parametrize("path", _invalid_examples(), ids=lambda p: p.name)
def test_invalid_examples_fail_closed(path: Path) -> None:
    with pytest.raises(ContractError):
        validate_crossing_event(_load(path), _SCHEMA)


def test_additional_property_rejected() -> None:
    ev = _load(_EXAMPLES / "valid" / "full.json")
    ev["count_delta"] = 1  # campo INVENTADO
    with pytest.raises(ContractError) as ei:
        validate_crossing_event(ev, _SCHEMA)
    assert any("count_delta" in r for r in ei.value.reasons)


def test_missing_required_rejected() -> None:
    ev = _load(_EXAMPLES / "valid" / "minimal_required_only.json")
    del ev["track_id"]
    with pytest.raises(ContractError) as ei:
        validate_crossing_event(ev, _SCHEMA)
    assert any("track_id" in r for r in ei.value.reasons)


def test_wrong_schema_version_rejected() -> None:
    ev = _load(_EXAMPLES / "valid" / "full.json")
    ev["schema_version"] = 2  # const = 1
    with pytest.raises(ContractError):
        validate_crossing_event(ev, _SCHEMA)


def test_clip_key_null_union_accepted() -> None:
    ev = _load(_EXAMPLES / "valid" / "minimal_required_only.json")
    ev["clip_key"] = None  # type union ["string","null"]
    validate_crossing_event(ev, _SCHEMA)


def test_baked_schema_resolves_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_schema()`` sin argumento resuelve el contrato del repo (walk-up)."""
    monkeypatch.delenv("CAMCOUNTER_CROSSING_SCHEMA_PATH", raising=False)
    schema = load_schema()
    assert schema.get("title") == "CrossingEvent"
