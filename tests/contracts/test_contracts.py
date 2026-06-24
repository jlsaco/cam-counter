"""Gate de contratos: valida los ejemplos canónicos contra los JSON Schemas de
``contracts/`` y FALLA CERRADO si un ejemplo no se ajusta a su contrato.

Este test es la red de seguridad de WP02 (reconciliación de contratos):

- Los ejemplos ``examples/<contrato>/valid/*.json`` DEBEN validar. El de
  ``crossing_event`` es el **payload MQTT verbatim** (topic
  ``cam-counter/{device_id}/events/crossing``); el de ``line_config`` es el
  **desired de la named shadow ``line-config-{camera_id}`` verbatim**.
- Los ejemplos ``examples/<contrato>/invalid/*.json`` DEBEN fallar, y cada uno
  por su motivo declarado (ver ``INVALID_REASONS``). Esto prueba que el contrato
  ``additionalProperties:false`` rechaza los campos INVENTADOS (``count_delta``,
  ``line_config_version``, ``direction_positive``, ``version``) y que los
  required reales (``track_id``, ``crossing_seq``, ``camera_id``) se exigen.
- ``event_id`` es DETERMINISTA: se recomputa desde la tupla de identidad y debe
  coincidir con el del ejemplo (reproducibilidad cloud-side).

No depende del paquete ``cam_counter_edge``: sólo ``jsonschema`` + stdlib, para
que el gate corra aislado en cualquier PR (ver ``.github/workflows/contracts.yml``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

# tests/contracts/test_contracts.py -> repo root = parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"

# Mapa: carpeta de ejemplos -> schema canónico en contracts/.
SCHEMA_FOR = {
    "crossing_event": CONTRACTS_DIR / "crossing_event.schema.json",
    "line_config": CONTRACTS_DIR / "line_config.schema.json",
}

# Motivo declarado de cada ejemplo INVÁLIDO: subcadena que DEBE aparecer en algún
# mensaje de error de validación. Garantiza que el ejemplo falla por LA razón
# pretendida (no por un typo colateral): la demostración del fail-closed es real.
INVALID_REASONS = {
    "crossing_event/invented_count_delta.json": "count_delta",
    "crossing_event/invented_line_config_version.json": "line_config_version",
    "crossing_event/missing_track_id.json": "track_id",
    "crossing_event/missing_crossing_seq.json": "crossing_seq",
    "crossing_event/bad_event_id_pattern.json": "NOT-A-SHA1",
    "crossing_event/wrong_schema_version.json": "1",
    "crossing_event/slug_with_hash.json": "rpi5#puerta",
    "line_config/invented_version_field.json": "version",
    "line_config/invented_direction_positive.json": "direction_positive",
    "line_config/missing_camera_id.json": "camera_id",
    "line_config/coord_out_of_range.json": "1.5",
    "line_config/bad_positive_side.json": "0",
}


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(contract: str) -> Draft202012Validator:
    schema = _load_json(SCHEMA_FOR[contract])
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _examples(kind: str) -> list[Path]:
    """Lista ordenada de ejemplos de un tipo ('valid' | 'invalid')."""
    out: list[Path] = []
    for contract in SCHEMA_FOR:
        out.extend(sorted((EXAMPLES_DIR / contract / kind).glob("*.json")))
    return out


def _rel(path: Path) -> str:
    """Clave estable 'contrato/archivo.json' (independiente del cwd)."""
    return f"{path.parent.parent.name}/{path.name}"


def _contract_of(path: Path) -> str:
    return path.parent.parent.name


def test_examples_present() -> None:
    """Sanity: hay ejemplos válidos e inválidos para AMBOS contratos en alcance."""
    for contract in SCHEMA_FOR:
        valid = sorted((EXAMPLES_DIR / contract / "valid").glob("*.json"))
        invalid = sorted((EXAMPLES_DIR / contract / "invalid").glob("*.json"))
        assert valid, f"faltan ejemplos válidos para {contract}"
        assert invalid, f"faltan ejemplos inválidos para {contract}"


@pytest.mark.parametrize("path", _examples("valid"), ids=_rel)
def test_valid_example_validates(path: Path) -> None:
    """Cada ejemplo VÁLIDO se ajusta a su contrato (gate verde legítimo)."""
    validator = _validator(_contract_of(path))
    instance = _load_json(path)
    error = best_match(validator.iter_errors(instance))
    assert error is None, f"{_rel(path)} debería validar pero falló: {error}"


@pytest.mark.parametrize("path", _examples("invalid"), ids=_rel)
def test_invalid_example_fails_closed(path: Path) -> None:
    """Cada ejemplo INVÁLIDO falla, y por su motivo declarado (fail-closed)."""
    validator = _validator(_contract_of(path))
    instance = _load_json(path)
    errors = list(validator.iter_errors(instance))
    assert errors, f"{_rel(path)} debería FALLAR la validación pero pasó"

    rel = _rel(path)
    expected = INVALID_REASONS.get(rel)
    assert expected is not None, f"sin motivo declarado para {rel} en INVALID_REASONS"
    blob = " || ".join(e.message for e in errors)
    assert expected in blob, (
        f"{rel} falla, pero no por el motivo esperado {expected!r}. "
        f"Errores: {blob}"
    )


def test_event_id_is_deterministic() -> None:
    """``event_id`` = sha1('site|device|camera|track_id|crossing_seq').

    Recomputa el id desde la tupla de identidad de cada ejemplo válido de
    crossing_event y exige que coincida: si no fuese reproducible cloud-side, la
    deduplicación idempotente edge->cloud se rompería.
    """
    for path in sorted((EXAMPLES_DIR / "crossing_event" / "valid").glob("*.json")):
        ev = _load_json(path)
        assert isinstance(ev, dict)
        raw = (
            f"{ev['site_id']}|{ev['device_id']}|{ev['camera_id']}"
            f"|{ev['track_id']}|{ev['crossing_seq']}"
        )
        expected = hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 (dedupe, no crypto)
        assert ev["event_id"] == expected, (
            f"{_rel(path)}: event_id no reproducible. "
            f"esperado {expected}, encontrado {ev['event_id']}"
        )


def test_invalid_reasons_cover_all_invalid_examples() -> None:
    """No queda ningún ejemplo inválido sin motivo declarado (evita drift mudo)."""
    on_disk = {_rel(p) for p in _examples("invalid")}
    declared = set(INVALID_REASONS)
    assert on_disk == declared, (
        f"desajuste entre ejemplos inválidos y INVALID_REASONS. "
        f"sólo en disco: {on_disk - declared}; sólo declarados: {declared - on_disk}"
    )
