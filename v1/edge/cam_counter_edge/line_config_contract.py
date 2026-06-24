"""Valida un documento ``LineConfig`` **VERBATIM** contra el contrato canónico.

El canal comando/config nube->dispositivo (Device Shadow, WP15) entrega el
``desired`` como un documento ``LineConfig`` (``contracts/line_config.schema.json``).
ANTES de aplicarlo a SQLite se valida **fail-closed** contra ESE MISMO schema con
``additionalProperties:false``: si el ``desired`` no casa el contrato, NO se aplica
(el reconciliador lo ignora y re-reporta su versión vigente). Validar VERBATIM
(nota del revisor) impide que la nube empuje geometría/campos fuera de contrato.

Reglas del contrato que se asertan (subconjunto EXACTO de Draft 2020-12 que usa
``line_config.schema.json``): ``type`` (incl. uniones), ``const``, ``enum``,
``pattern``, ``minimum``, ``maximum``, ``required``, ``additionalProperties:false``
y **recursión en objetos anidados** (``line.a.x`` etc.). NO se importa
``jsonschema``: misma filosofía que ``crossing_payload`` y la Lambda de ingesta
(el device valida sin dependencias). ``format`` NO se asevera (igual que el resto
del sistema).
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .types import Line, LineConfig, Point

__all__ = [
    "LineConfigContractError",
    "line_config_from_document",
    "line_config_to_document",
    "load_line_config_schema",
    "validate_document",
]

# Override explícito de la ruta del contrato (tests / empaquetado).
_SCHEMA_ENV = "CAMCOUNTER_LINE_CONFIG_SCHEMA_PATH"
_SCHEMA_NAME = "line_config.schema.json"


class LineConfigContractError(ValueError):
    """El documento NO se ajusta al contrato ``LineConfig`` (fail-closed).

    ``reasons`` enumera POR QUÉ se rechazó (additionalProperties / required /
    pattern / minimum / maximum / type…) para registrar la causa sin aplicar
    nada inválido a SQLite.
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
def load_line_config_schema(path: str | None = None) -> dict[str, Any]:
    """Carga (cacheado) el JSON Schema canónico del ``LineConfig``."""
    schema_path = Path(path) if path is not None else _find_schema_path()
    return json.loads(schema_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Validador stdlib RECURSIVO: subconjunto Draft 2020-12 que usa el contrato.
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


def _validate(value: Any, schema: dict[str, Any], path: str, reasons: list[str]) -> None:
    """Valida ``value`` contra ``schema`` acumulando motivos (recursivo)."""
    if "type" in schema and not _type_ok(value, schema["type"]):
        reasons.append(f"{path or '<root>'}: tipo inválido (esperado {schema['type']})")
        return  # sin el tipo correcto, las demás aserciones no aplican

    if "const" in schema and value != schema["const"]:
        reasons.append(f"{path or '<root>'}: debe ser {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        reasons.append(f"{path or '<root>'}: valor fuera de enum {schema['enum']}")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            reasons.append(f"{path or '<root>'}: no casa el patrón {schema['pattern']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            reasons.append(f"{path or '<root>'}: menor que el mínimo {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > schema["maximum"]:
            reasons.append(f"{path or '<root>'}: mayor que el máximo {schema['maximum']}")

    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    reasons.append(f"{path or '<root>'}: propiedad no permitida: {key!r}")
        for req in schema.get("required", []):
            if req not in value:
                reasons.append(f"{path or '<root>'}: falta campo requerido: {req!r}")
        for key, sub in value.items():
            spec = props.get(key)
            if spec is None:
                continue  # ya reportado por additionalProperties
            child_path = f"{path}.{key}" if path else key
            _validate(sub, spec, child_path, reasons)


def validate_document(doc: Any, schema: dict[str, Any]) -> list[str]:
    """Valida ``doc`` contra ``schema`` (recursivo). Devuelve la lista de motivos."""
    reasons: list[str] = []
    if not isinstance(doc, dict):
        return [f"<root>: tipo inválido (esperado object, recibido {type(doc).__name__})"]
    _validate(doc, schema, "", reasons)
    return reasons


def line_config_from_document(
    doc: Any, *, schema: dict[str, Any] | None = None
) -> LineConfig:
    """Valida ``doc`` VERBATIM y lo convierte a ``LineConfig`` (fail-closed).

    Lanza ``LineConfigContractError`` si ``doc`` no casa el contrato (incl. la
    geometría anidada y el ``schema_version`` const=1). Sólo se llega a construir
    el ``LineConfig`` si el documento es válido.
    """
    schema = schema if schema is not None else load_line_config_schema()
    reasons = validate_document(doc, schema)
    if reasons:
        raise LineConfigContractError(reasons)
    line = doc["line"]
    return LineConfig(
        site_id=doc["site_id"],
        device_id=doc["device_id"],
        camera_id=doc["camera_id"],
        config_version=int(doc["config_version"]),
        line=Line(
            a=Point(float(line["a"]["x"]), float(line["a"]["y"])),
            b=Point(float(line["b"]["x"]), float(line["b"]["y"])),
        ),
        positive_side=int(doc["positive_side"]),
        positive_label=doc.get("positive_label"),
        negative_label=doc.get("negative_label"),
        updated_at=doc.get("updated_at"),
        schema_version=int(doc.get("schema_version", 1)),
    )


def line_config_to_document(config: LineConfig) -> dict[str, Any]:
    """Construye el documento ``LineConfig`` canónico (para ``reported`` del shadow).

    Incluye los campos requeridos + los opcionales con valor. El documento
    resultante casa el contrato VERBATIM (es el espejo de lo que la nube pone en
    ``desired``), de modo que cuando ``reported == desired`` el delta del shadow
    converge. ``updated_at`` (campo del editor/local) se incluye sólo si existe;
    la nube no debería ponerlo en ``desired`` (lo gobierna quien escribe SQLite).
    """
    doc: dict[str, Any] = {
        "site_id": config.site_id,
        "device_id": config.device_id,
        "camera_id": config.camera_id,
        "config_version": int(config.config_version),
        "line": {
            "a": {"x": float(config.line.a.x), "y": float(config.line.a.y)},
            "b": {"x": float(config.line.b.x), "y": float(config.line.b.y)},
        },
        "positive_side": int(config.positive_side),
        "schema_version": int(config.schema_version),
    }
    if config.positive_label is not None:
        doc["positive_label"] = config.positive_label
    if config.negative_label is not None:
        doc["negative_label"] = config.negative_label
    if config.updated_at is not None:
        doc["updated_at"] = config.updated_at
    return doc
