"""Validación VERBATIM del ``CrossingEvent`` contra el contrato JSON Schema.

El contrato canónico es ``contracts/crossing_event.schema.json``. En el paquete de
despliegue se **hornea** una copia byte-a-byte bajo ``schema/`` (la copia la hace
``make build-lambdas``); en CI/local (sin build) se resuelve el contrato del repo.
NUNCA se descarga el esquema por red en el camino caliente.

IMPORTANTE: el contrato declara ``additionalProperties: false``. La IoT Rule
enriquece el payload con campos de meta (``_device_id_topic``, ``_client_id``,
``_ingest_ts_ms``); por eso esos campos DEBEN separarse ANTES de validar (lo hace
``handler.split_meta``). Aquí se valida el ``CrossingEvent`` LIMPIO contra el
contrato, sin relajarlo.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import jsonschema

SCHEMA_FILENAME = "crossing_event.schema.json"


def _candidate_paths():
    """Rutas candidatas del esquema, en orden de preferencia."""
    env = os.environ.get("CROSSING_EVENT_SCHEMA_PATH")
    if env:
        yield Path(env)

    here = Path(__file__).resolve().parent
    # 1) horneado en el paquete (despliegue real).
    yield here / "schema" / SCHEMA_FILENAME
    # 2) contracts/ del repo, subiendo desde el módulo (tests / local sin build).
    for base in (here, *here.parents):
        yield base / "contracts" / SCHEMA_FILENAME


@functools.lru_cache(maxsize=1)
def load_schema() -> dict:
    """Carga el contrato (cacheado). Falla cerrado si no encuentra ninguna copia."""
    tried = []
    for cand in _candidate_paths():
        tried.append(str(cand))
        if cand.is_file():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        "No se encontró crossing_event.schema.json (horneado ni en contracts/). "
        f"Rutas probadas: {tried}"
    )


@functools.lru_cache(maxsize=1)
def _validator() -> jsonschema.protocols.Validator:
    schema = load_schema()
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def validate_crossing_event(payload: dict) -> None:
    """Valida VERBATIM contra el contrato. Lanza ``jsonschema.ValidationError`` si falla.

    Una excepción aquí propaga al runtime de Lambda → reintento async → DLQ tras
    agotar reintentos (payload malformado / spoofeado no se persiste).
    """
    _validator().validate(payload)
