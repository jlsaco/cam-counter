"""Validación VERBATIM del contrato ``CrossingEvent`` — stdlib pura, sin deps.

Esta Lambda valida el payload MQTT (= ``crossing_event`` verbatim, topic
``cam-counter/{device_id}/events/crossing``) contra el **mismo** JSON Schema
canónico que ``contracts/crossing_event.schema.json`` y que el gate de contratos
de WP02. Para que el paquete sea **autocontenido** (lo empaqueta Terraform vía
``archive_file`` sin `pip install`, y MAD lo aplica como ``terraform apply`` sin
paso de build), NO se vendoriza ``jsonschema``: se implementa el **subconjunto
EXACTO de Draft 2020-12** que usa el contrato y se interpreta el schema HORNEADO
(``crossing_event.schema.json`` copiado al lado del handler en el zip).

Reconciliación (issue WP05 esbozaba «validation con jsonschema»): se valida
**verbatim contra el contrato** (criterio de aceptación real) pero con un
validador stdlib para no introducir dependencias que romperían el empaquetado
autocontenido de Terraform. El conjunto de features cubre EXACTAMENTE las que el
contrato emplea: ``type`` (incl. unión ``["string","null"]``), ``enum``,
``const``, ``pattern``, ``minimum``, ``required`` y ``additionalProperties:false``.
``format`` NO se asevera (igual que ``Draft202012Validator`` por defecto), para
ser equivalente carácter-a-carácter al gate de WP02.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

__all__ = ["ContractError", "load_schema", "validate_crossing_event"]

# Nombre del schema horneado junto al handler dentro del zip de la Lambda.
_BAKED_SCHEMA_NAME = "crossing_event.schema.json"
# Variable de entorno (canon CAMCOUNTER_*) para override explícito de la ruta.
_SCHEMA_ENV = "CAMCOUNTER_CROSSING_SCHEMA_PATH"


class ContractError(ValueError):
    """El payload NO se ajusta al contrato ``CrossingEvent`` (fail-closed).

    Lleva la lista de motivos (``reasons``) para que la Lambda registre POR QUÉ
    rechazó (anti-spoof / additionalProperties / required / pattern…). El primer
    motivo va también en el mensaje para los logs.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons) if reasons else "contrato no satisfecho")


def _find_schema_path() -> Path:
    """Resuelve la ruta del schema canónico (override → horneado → repo).

    1. ``$CAMCOUNTER_CROSSING_SCHEMA_PATH`` si está definido (tests/override).
    2. El schema HORNEADO al lado de este módulo (lo que ve la Lambda en runtime).
    3. ``contracts/crossing_event.schema.json`` subiendo por el árbol del repo
       (lo que ven los tests, sin copia commiteada del contrato).
    """
    override = os.environ.get(_SCHEMA_ENV)
    if override:
        return Path(override)

    baked = Path(__file__).resolve().with_name(_BAKED_SCHEMA_NAME)
    if baked.is_file():
        return baked

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / _BAKED_SCHEMA_NAME
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        f"no se encontró {_BAKED_SCHEMA_NAME} (horneado ni en contracts/); "
        f"define {_SCHEMA_ENV} para apuntarlo explícitamente."
    )


def load_schema(path: str | os.PathLike[str] | None = None) -> dict:
    """Carga el JSON Schema canónico del contrato (cacheable por el caller)."""
    schema_path = Path(path) if path is not None else _find_schema_path()
    return json.loads(schema_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Subconjunto de Draft 2020-12 que usa el contrato (type/enum/const/pattern/...)
# --------------------------------------------------------------------------- #


def _type_ok(value: object, json_type: str) -> bool:
    """¿``value`` casa con el ``json_type`` de JSON Schema? (bool ≠ integer/number)."""
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "null":
        return value is None
    if json_type == "integer":
        # En JSON, 1.0 es number, no integer; bool es subclase de int → se excluye.
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ValueError(f"tipo JSON Schema no soportado por el validador: {json_type!r}")


def _check_property(name: str, value: object, spec: dict, reasons: list[str]) -> None:
    """Valida UNA propiedad contra su subschema (acumula motivos en ``reasons``)."""
    # type: string o lista de strings (unión, p.ej. ["string","null"] de clip_key).
    declared = spec.get("type")
    if declared is not None:
        types = [declared] if isinstance(declared, str) else list(declared)
        if not any(_type_ok(value, t) for t in types):
            reasons.append(f"{name}: tipo inválido (esperado {declared}, valor {value!r})")
            return  # sin tipo correcto, el resto de checks no aplica con seguridad

    if "const" in spec and value != spec["const"]:
        reasons.append(f"{name}: debe ser const {spec['const']!r}, no {value!r}")

    if "enum" in spec and value not in spec["enum"]:
        reasons.append(f"{name}: {value!r} no está en enum {spec['enum']!r}")

    pattern = spec.get("pattern")
    if pattern is not None and isinstance(value, str) and re.search(pattern, value) is None:
        reasons.append(f"{name}: {value!r} no casa el patrón {pattern!r}")

    minimum = spec.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < minimum:
            reasons.append(f"{name}: {value!r} < minimum {minimum!r}")


def validate_crossing_event(payload: object, schema: dict) -> None:
    """Valida ``payload`` contra el contrato. Lanza ``ContractError`` si no cumple.

    Cubre el subconjunto de Draft 2020-12 del contrato: objeto raíz con
    ``additionalProperties:false``, ``required`` y por-propiedad
    ``type``/``const``/``enum``/``pattern``/``minimum``. Fail-closed: cualquier
    campo INVENTADO, ``required`` ausente o tipo/patrón inválido => error.
    """
    reasons: list[str] = []

    if not isinstance(payload, dict):
        raise ContractError([f"payload raíz debe ser objeto, no {type(payload).__name__}"])

    properties: dict = schema.get("properties", {})

    # additionalProperties:false — ningún campo fuera del contrato (anti-spoof de
    # campos inventados, p.ej. count_delta / line_config_version).
    if schema.get("additionalProperties") is False:
        for key in payload:
            if key not in properties:
                reasons.append(f"propiedad no permitida (additionalProperties:false): {key!r}")

    # required — los campos obligatorios del contrato deben estar presentes.
    for req in schema.get("required", []):
        if req not in payload:
            reasons.append(f"falta el campo requerido: {req!r}")

    # Por-propiedad: valida sólo las presentes (las opcionales ausentes no fallan).
    for name, value in payload.items():
        spec = properties.get(name)
        if isinstance(spec, dict):
            _check_property(name, value, spec, reasons)

    if reasons:
        raise ContractError(reasons)
