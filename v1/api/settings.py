"""Configuración por entorno de la API local (sin secretos en el repo).

Toda la parametrización de runtime se lee de variables de entorno. NUNCA se
commitea un secreto: el gate OPCIONAL de token de escritura se lee de
``CAMCOUNTER_API_TOKEN`` y, si no está definido, las escrituras LAN se permiten
(modelo de confianza LAN, ver CLAUDE.md §2/§5).

Edge-first: la API sirve datos LOCALES (SQLite del borde) y funciona SIN
internet; nada aquí requiere red a la nube.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cam_counter_edge import make_camera_id, validate_device_id, validate_site_id

__all__ = ["Settings", "get_settings"]

# Versión del CONTRATO de la API (info.version del OpenAPI). Es ESTABLE y
# distinta del app_version derivado de git (ese fluye por /api/device). Mantenerla
# constante hace reproducible el snapshot de /api/openapi.json en CI.
API_SCHEMA_VERSION = "1.0.0"


def _env_flag(name: str, default: bool = False) -> bool:
    """Lee una variable de entorno como flag booleano ('1','true','yes','on')."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Configuración inmutable derivada del entorno.

    Attributes:
        fake_source: si ``True`` (``CAMCOUNTER_FAKE_SOURCE=1``) usa la fuente
            determinista (MJPEG en bucle + cruces guionizados) en vez de la
            cámara/Hailo reales — para E2E y desarrollo sin Pi.
        db_path: ruta del SQLite del borde (compartido WAL con el conteo).
        site_id/device_id: identificadores (slugs validados) del Pi/sitio.
        camera_count: nº de cámaras lógicas a exponer (multi-cámara desde v1).
        api_token: token compartido OPCIONAL para escrituras (None = LAN abierta).
        frame_interval_s: cadencia del stream MJPEG y de la fuente falsa.
        ui_dist: directorio de la SPA construida (``v1/ui/dist``) a servir.
    """

    fake_source: bool
    db_path: str
    site_id: str
    device_id: str
    camera_count: int
    api_token: str | None
    frame_interval_s: float
    ui_dist: Path

    @property
    def camera_ids(self) -> list[str]:
        """``camera_id`` global único por cámara: ``{device_id}-cam{N}``."""
        return [make_camera_id(self.device_id, n) for n in range(self.camera_count)]


def get_settings() -> Settings:
    """Construye ``Settings`` leyendo el entorno EN EL MOMENTO de la llamada.

    Se lee perezosamente (no a nivel de módulo) para que los tests puedan ajustar
    el entorno antes de crear la app. Valida ``site_id``/``device_id`` como slugs.
    """
    site_id = validate_site_id(os.environ.get("CAMCOUNTER_SITE_ID", "demo-site"))
    device_id = validate_device_id(os.environ.get("CAMCOUNTER_DEVICE_ID", "demo-pi"))
    try:
        camera_count = int(os.environ.get("CAMCOUNTER_CAMERA_COUNT", "2"))
    except ValueError:
        camera_count = 2
    camera_count = max(1, camera_count)
    try:
        frame_interval = float(os.environ.get("CAMCOUNTER_FRAME_INTERVAL", "0.2"))
    except ValueError:
        frame_interval = 0.2

    default_db = str(Path(__file__).resolve().parent / "cam-counter.db")
    token = os.environ.get("CAMCOUNTER_API_TOKEN")
    return Settings(
        fake_source=_env_flag("CAMCOUNTER_FAKE_SOURCE"),
        db_path=os.environ.get("CAMCOUNTER_DB_PATH", default_db),
        site_id=site_id,
        device_id=device_id,
        camera_count=camera_count,
        api_token=token if token else None,
        frame_interval_s=max(0.01, frame_interval),
        ui_dist=Path(__file__).resolve().parent.parent / "ui" / "dist",
    )
