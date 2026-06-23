"""Adaptador hilo<->asyncio entre la API y el paquete de borde ``cam_counter_edge``.

El motor PUENTEA el subsistema de conteo (SQLite del borde, pipeline, fuente de
vídeo) hacia los handlers asíncronos de FastAPI SIN bloquear NUNCA el event loop:

- Toda operación de SQLite (bloqueante) se ejecuta en un executor de UN solo hilo,
  de modo que la conexión ``api_store`` SÓLO se toca desde ese hilo (seguro) y el
  loop queda libre.
- La fuente de vídeo/cruces corre en su PROPIO hilo (ver ``fakes.py``) con su
  PROPIA conexión SQLite (WAL: 1 escritor + N lectores).
- El hub WebSocket recibe eventos vía ``publish_threadsafe`` (desde el hilo de la
  fuente) o ``await broadcast`` (desde los handlers).

El motor expone snapshots de device/health/cameras/counters/events/config por
cámara y propaga el hot-reload de config (CAS de ``config_version`` + señal WS).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypeVar

from cam_counter_edge import SCHEMA_USER_VERSION, Store, validate_camera_id
from cam_counter_edge.types import Line as EdgeLine
from cam_counter_edge.types import LineConfig as EdgeLineConfig
from cam_counter_edge.types import Point as EdgePoint

import mjpeg
from fakes import CameraState, FakeSource, NullSource, Source, default_line_config
from hub import WsHub
from rtsp_source import RtspSource, any_rtsp_configured
from schemas import (
    Camera,
    CameraHealth,
    CounterDay,
    Counters,
    CrossingEvent,
    DeviceInfo,
    Health,
    LineConfig,
    LineConfigUpdate,
    WsEnvelope,
)
from settings import Settings

__all__ = ["Engine", "UnknownCameraError"]

T = TypeVar("T")


class UnknownCameraError(LookupError):
    """``camera_id`` con slug válido pero que no pertenece a este dispositivo."""


def _version_info() -> tuple[str, str]:
    """Deriva ``(app_version, git_sha)`` de ``scripts/version.py`` (degrada limpio).

    Importa el módulo por ruta (``scripts/`` no es paquete) y usa ``derive()``;
    si algo falla, degrada a un dev-version sin lanzar (coherente con version.py).
    """
    try:
        version_py = Path(__file__).resolve().parents[2] / "scripts" / "version.py"
        spec = importlib.util.spec_from_file_location("_camcounter_version", version_py)
        if spec is None or spec.loader is None:
            return "0.0.0-dev.0+gunknown", "unknown"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        version, git_sha, _dirty, _release = module.derive()
        return str(version), str(git_sha)
    except Exception:  # noqa: BLE001 — version.py NUNCA debe tumbar la API
        return "0.0.0-dev.0+gunknown", "unknown"


class Engine:
    """Fachada asíncrona sobre el store del borde + la fuente de vídeo/cruces."""

    def __init__(self, settings: Settings, hub: WsHub) -> None:
        self._settings = settings
        self._hub = hub
        self._states: dict[str, CameraState] = {
            cam: CameraState(camera_id=cam) for cam in settings.camera_ids
        }
        self._store: Store | None = None
        self._source: Source | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._app_version, self._git_sha = _version_info()

    # -- ciclo de vida ----------------------------------------------------

    async def start(self) -> None:
        """Abre el store, arranca la fuente y asocia el loop al hub."""
        loop = asyncio.get_running_loop()
        self._hub.bind_loop(loop)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camcounter-db")
        self._store = await self._run(Store, self._settings.db_path)
        if self._settings.fake_source:
            self._source = FakeSource(self._settings, self._states, self._hub)
        elif any_rtsp_configured(self._settings):
            # Pi real: vídeo en vivo de la cámara por RTSP (ffmpeg). El conteo lo
            # hace cam-counter-edge; esta fuente sólo alimenta el MJPEG de la UI.
            self._source = RtspSource(self._settings, self._states)
        else:
            self._source = NullSource(self._states)
        self._source.start()

    async def stop(self) -> None:
        """Detiene la fuente, el executor y cierra el store."""
        if self._source is not None:
            self._source.stop()
            self._source = None
        if self._store is not None:
            store = self._store
            await self._run(store.close)
            self._store = None
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    async def _run(self, fn: Any, *args: Any) -> Any:
        """Ejecuta una operación bloqueante en el executor de un solo hilo."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    def _require_store(self) -> Store:
        if self._store is None:
            raise RuntimeError("Engine no iniciado: el store no está abierto")
        return self._store

    # -- validación de cámara --------------------------------------------

    def require_known_camera(self, camera_id: str) -> str:
        """Valida el slug y comprueba que la cámara pertenece al dispositivo.

        Lanza ``InvalidSlugError`` (slug malformado) o ``UnknownCameraError``
        (slug válido pero desconocido). Los handlers lo mapean a 400/404.
        """
        validate_camera_id(camera_id)
        if camera_id not in self._states:
            raise UnknownCameraError(camera_id)
        return camera_id

    # -- device / health --------------------------------------------------

    async def get_device_info(self) -> DeviceInfo:
        store = self._require_store()
        schema_version = await self._run(lambda: store.user_version)
        return DeviceInfo(
            device_id=self._settings.device_id,
            site_id=self._settings.site_id,
            app_version=self._app_version,
            git_sha=self._git_sha,
            camera_ids=list(self._settings.camera_ids),
            db_schema_version=int(schema_version),
            fake_source=self._settings.fake_source,
        )

    async def get_health(self) -> Health:
        store = self._require_store()
        schema_version = int(await self._run(lambda: store.user_version))
        cameras = [
            CameraHealth(
                camera_id=cam,
                frames_processed=st.frames_processed,
                last_inference_ts=st.last_inference_ts,
                hailo_inference_ok=st.hailo_inference_ok,
                config_version=int(await self._run(store.get_config_version, cam)),
            )
            for cam, st in self._states.items()
        ]
        frames_flowing = any(c.frames_processed > 0 for c in cameras)
        status: Literal["ok", "degraded"] = (
            "ok" if schema_version == SCHEMA_USER_VERSION else "degraded"
        )
        return Health(
            status=status,
            app_version=self._app_version,
            db_schema_version=schema_version,
            fake_source=self._settings.fake_source,
            frames_flowing=frames_flowing,
            cameras=cameras,
        )

    # -- cámaras ----------------------------------------------------------

    async def list_cameras(self) -> list[Camera]:
        return [await self.get_camera(cam) for cam in self._settings.camera_ids]

    async def get_camera(self, camera_id: str) -> Camera:
        store = self._require_store()
        st = self._states[camera_id]
        config_version = int(await self._run(store.get_config_version, camera_id))
        return Camera(
            camera_id=camera_id,
            site_id=self._settings.site_id,
            device_id=self._settings.device_id,
            config_version=config_version,
            has_config=config_version > 0,
            frames_processed=st.frames_processed,
            online=st.online,
        )

    # -- config de línea (hot-reload) ------------------------------------

    async def get_line_config(self, camera_id: str) -> LineConfig:
        """Config vigente; si la cámara no tiene una, devuelve el default (v0)."""
        store = self._require_store()
        edge_config = await self._run(store.get_line_config, camera_id)
        if edge_config is None:
            edge_config = default_line_config(self._settings, camera_id)
        return _to_api_config(edge_config)

    async def put_line_config(self, camera_id: str, update: LineConfigUpdate) -> LineConfig:
        """Persiste la config con CAS; lanza ``StaleConfigVersionError`` si stale.

        En éxito dispara la SEÑAL de hot-reload del motor (WS ``config_changed``);
        la fuente la recoge vía su ``ConfigWatcher`` (relectura de ``config_version``).
        """
        store = self._require_store()
        edge_config = EdgeLineConfig(
            site_id=self._settings.site_id,
            device_id=self._settings.device_id,
            camera_id=camera_id,
            config_version=0,  # lo gobierna la DB; se ignora en la escritura
            line=EdgeLine(
                a=EdgePoint(update.line.a.x, update.line.a.y),
                b=EdgePoint(update.line.b.x, update.line.b.y),
            ),
            positive_side=update.positive_side,
            positive_label=update.positive_label,
            negative_label=update.negative_label,
        )
        new_version = int(
            await self._run(
                store.set_line_config, camera_id, edge_config, update.expected_config_version
            )
        )
        await self.notify_config_changed(camera_id, new_version)
        return await self.get_line_config(camera_id)

    async def notify_config_changed(self, camera_id: str, config_version: int) -> None:
        """Señal de hot-reload: emite ``config_changed`` al hub WS."""
        await self._hub.broadcast(
            WsEnvelope(
                type="config_changed",
                camera_id=camera_id,
                ts_ms=0,
                data={"config_version": config_version},
            )
        )

    # -- counters ---------------------------------------------------------

    async def get_counters(self, camera_id: str) -> Counters:
        store = self._require_store()
        rows = await self._run(store.get_counters, camera_id)
        return _aggregate_counters(camera_id, rows)

    async def reset_counters(self, camera_id: str) -> Counters:
        store = self._require_store()
        await self._run(store.reset_counters, camera_id)
        st = self._states[camera_id]
        st.in_count = 0
        st.out_count = 0
        return await self.get_counters(camera_id)

    # -- events (histórico paginado) -------------------------------------

    async def get_events(self, camera_id: str, *, limit: int, offset: int) -> list[CrossingEvent]:
        store = self._require_store()
        rows = await self._run(store.get_recent_events, camera_id, offset + limit)
        page = rows[offset : offset + limit]
        return [CrossingEvent.model_validate(row) for row in page]

    # -- MJPEG ------------------------------------------------------------

    def get_frame(self, camera_id: str) -> bytes:
        """Último frame JPEG de la cámara (o un 'sin señal'). NO bloquea (memoria)."""
        st = self._states[camera_id]
        frame = st.frame
        if frame is None:
            return mjpeg.placeholder_frame(camera_id)
        return frame

    @property
    def frame_interval_s(self) -> float:
        return self._settings.frame_interval_s


# --------------------------------------------------------------------------- #
# Conversión edge <-> API y agregaciones (funciones puras)
# --------------------------------------------------------------------------- #


def _to_api_config(edge_config: EdgeLineConfig) -> LineConfig:
    return LineConfig.model_validate(
        {
            "site_id": edge_config.site_id,
            "device_id": edge_config.device_id,
            "camera_id": edge_config.camera_id,
            "config_version": edge_config.config_version,
            "line": {
                "a": {"x": edge_config.line.a.x, "y": edge_config.line.a.y},
                "b": {"x": edge_config.line.b.x, "y": edge_config.line.b.y},
            },
            "positive_side": edge_config.positive_side,
            "positive_label": edge_config.positive_label,
            "negative_label": edge_config.negative_label,
            "updated_at": edge_config.updated_at,
            "schema_version": edge_config.schema_version,
        }
    )


def _aggregate_counters(camera_id: str, rows: list[dict]) -> Counters:
    days: list[CounterDay] = []
    in_count = 0
    out_count = 0
    for row in rows:
        direction = row["direction"]
        count = int(row["count"])
        if direction == "in":
            in_count += count
        elif direction == "out":
            out_count += count
        days.append(
            CounterDay(day_utc=row["day_utc"], direction=direction, count=count)
        )
    return Counters(
        camera_id=camera_id,
        in_count=in_count,
        out_count=out_count,
        net=in_count - out_count,
        days=days,
    )
