"""Serializa un ``CrossingEvent`` al payload MQTT **VERBATIM** del contrato.

El payload que el device publica en ``cam-counter/{device_id}/events/crossing`` ES el
``CrossingEvent`` del contrato canónico (``contracts/crossing_event.schema.json``) tal
cual — **snake_case**, con ``track_id`` / ``crossing_seq`` / ``ts_event_iso`` /
``schema_version`` / ``line_version`` y ``clip_key`` (el clip va aparte a S3). La Lambda
de ingesta (WP05) valida ESE MISMO payload contra ESE MISMO schema con
``additionalProperties:false``; por eso el mapeo se valida aquí **fail-closed** ANTES de
publicar: si no casa el contrato, NO se publica (el evento queda ``synced=0`` y se
reintenta), evitando que un payload inválido se pierda en el broker.

**Anti-spoof:** además de validar el schema, se RECOMPUTA el ``event_id`` determinista a
partir de los identificadores del propio evento y se exige que coincida. Un evento con un
``event_id`` que no derive de ``site|device|camera|track_id|crossing_seq`` se rechaza
(no se puede falsificar el id sin que cuadre con su tupla de identidad).

NO importa ``jsonschema``: implementa el subconjunto EXACTO de Draft 2020-12 que usa el
contrato (igual filosofía que la Lambda de ingesta), para que el device valide sin deps.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .line_counter import compute_event_id
from .types import CrossingEvent

__all__ = [
    "PayloadContractError",
    "crossing_event_payload",
    "encode_payload",
    "load_contract_schema",
    "validate_against_contract",
]

# El flag ``synced`` es SÓLO-local (SQLite): NUNCA viaja a la nube (ni en el put
# directo a DynamoDB ni en el payload MQTT). Se omite del payload verbatim.
_LOCAL_ONLY_FIELDS = frozenset({"synced"})

# Override explícito de la ruta del contrato (tests / empaquetado).
_SCHEMA_ENV = "CAMCOUNTER_CROSSING_SCHEMA_PATH"
_SCHEMA_NAME = "crossing_event.schema.json"


class PayloadContractError(ValueError):
    """El payload NO se ajusta al contrato ``CrossingEvent`` (fail-closed).

    ``reasons`` enumera POR QUÉ se rechazó (additionalProperties / required /
    pattern / anti-spoof…) para registrar la causa sin publicar nada inválido.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons) if reasons else "contrato no satisfecho")


def _find_schema_path() -> Path:
    """Resuelve la ruta del contrato: override → árbol del repo ``contracts/``."""
    override = os.environ.get(_SCHEMA_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / _SCHEMA_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no se encontró contracts/{_SCHEMA_NAME}; define {_SCHEMA_ENV} para apuntarlo."
    )


@lru_cache(maxsize=2)
def load_contract_schema(path: str | None = None) -> dict[str, Any]:
    """Carga (cacheado) el JSON Schema canónico del ``CrossingEvent``."""
    schema_path = Path(path) if path is not None else _find_schema_path()
    return json.loads(schema_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Validador stdlib: subconjunto EXACTO de Draft 2020-12 que usa el contrato
#   type (incl. unión ["string","null"]), enum, const, pattern, minimum,
#   required y additionalProperties:false. (format NO se asevera, igual que la
#   Lambda y el gate de contratos.)
# --------------------------------------------------------------------------- #

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "object": dict,
    "array": list,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, type_spec: Any) -> bool:
    specs = type_spec if isinstance(type_spec, list) else [type_spec]
    for spec in specs:
        py = _JSON_TYPES.get(spec)
        if py is None:
            continue
        # bool es subclase de int en Python; el contrato no usa booleanos numéricos.
        if spec in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def validate_against_contract(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Valida ``payload`` contra ``schema`` (subconjunto Draft 2020-12). Lista de motivos."""
    reasons: list[str] = []
    props: dict[str, Any] = schema.get("properties", {})

    if schema.get("additionalProperties") is False:
        for key in payload:
            if key not in props:
                reasons.append(f"propiedad no permitida: {key!r}")

    for req in schema.get("required", []):
        if req not in payload:
            reasons.append(f"falta campo requerido: {req!r}")

    for key, value in payload.items():
        spec = props.get(key)
        if spec is None:
            continue  # ya reportado por additionalProperties
        if "type" in spec and not _type_ok(value, spec["type"]):
            reasons.append(f"{key!r}: tipo inválido (esperado {spec['type']})")
            continue
        if "const" in spec and value != spec["const"]:
            reasons.append(f"{key!r}: debe ser {spec['const']!r}")
        if "enum" in spec and value not in spec["enum"]:
            reasons.append(f"{key!r}: valor fuera de enum {spec['enum']}")
        if "pattern" in spec and isinstance(value, str):
            if not re.search(spec["pattern"], value):
                reasons.append(f"{key!r}: no casa el patrón {spec['pattern']!r}")
        if "minimum" in spec and isinstance(value, (int, float)):
            if value < spec["minimum"]:
                reasons.append(f"{key!r}: menor que el mínimo {spec['minimum']}")
    return reasons


def crossing_event_payload(
    event: CrossingEvent, *, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Construye el payload MQTT verbatim del contrato y lo valida (fail-closed).

    Incluye los campos requeridos + opcionales con valor; OMITE ``synced``
    (sólo-local). Recompone el ``event_id`` determinista (anti-spoof) y valida contra
    el contrato. Lanza ``PayloadContractError`` si algo no casa.
    """
    payload: dict[str, Any] = {
        "event_id": event.event_id,
        "site_id": event.site_id,
        "device_id": event.device_id,
        "camera_id": event.camera_id,
        "track_id": str(event.track_id),
        "crossing_seq": int(event.crossing_seq),
        "direction": event.direction,
        "ts_event_ms": int(event.ts_event_ms),
        "ts_event_iso": event.ts_event_iso,
        "schema_version": int(event.schema_version),
    }
    # Opcionales: sólo si tienen valor (igual criterio que el put directo a DynamoDB).
    optionals = {
        "positive_label": event.positive_label,
        "negative_label": event.negative_label,
        "label": event.label,
        "line_version": event.line_version,
        "confidence": event.confidence,
        "clip_key": event.clip_key,
        "clip_status": event.clip_status,
        "created_at": event.created_at,
    }
    for key, value in optionals.items():
        if value is not None and key not in _LOCAL_ONLY_FIELDS:
            payload[key] = value

    reasons: list[str] = []

    # Anti-spoof: el event_id DEBE derivar de su propia tupla de identidad.
    expected_id = compute_event_id(
        event.site_id,
        event.device_id,
        event.camera_id,
        str(event.track_id),
        int(event.crossing_seq),
    )
    if event.event_id != expected_id:
        reasons.append(
            "anti-spoof: event_id no deriva de site|device|camera|track_id|crossing_seq"
        )

    schema = schema if schema is not None else load_contract_schema()
    reasons.extend(validate_against_contract(payload, schema))

    if reasons:
        raise PayloadContractError(reasons)
    return payload


def encode_payload(payload: dict[str, Any]) -> bytes:
    """Serializa el payload a bytes JSON compactos y deterministas (UTF-8)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
