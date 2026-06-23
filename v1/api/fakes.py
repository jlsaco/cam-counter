"""Fuente DETERMINISTA de vídeo + cruces (sin Pi/Hailo/cámara).

Activada por ``CAMCOUNTER_FAKE_SOURCE=1``. Reutiliza el pipeline REAL del borde
(``DummyDetector`` -> ``CentroidIoUTracker`` -> ``LineCounter`` -> ``Store``) para
generar, de forma reproducible:

- un stream MJPEG (frames sintéticos con cajas + línea + HUD), y
- una secuencia guionizada de cruces que produce ``CrossingEvent`` y mueve los
  contadores, exactamente por el mismo camino que en producción.

Así los E2E (Playwright, PR10) y el desarrollo local funcionan sin hardware. La
fuente corre en su PROPIO hilo con su PROPIA conexión SQLite (escritor); la API
lee por otra conexión (WAL admite 1 escritor + N lectores). El event loop de
asyncio NUNCA se bloquea: la fuente publica al hub vía ``publish_threadsafe``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from cam_counter_edge import (
    ConfigWatcher,
    DummyDetector,
    LineCounter,
    StaleConfigVersionError,
    Store,
    smooth_crossing_script,
)
from cam_counter_edge.tracker import CentroidIoUTracker
from cam_counter_edge.types import Line, LineConfig, Point

import mjpeg
from hub import WsHub
from schemas import WsEnvelope
from settings import Settings

__all__ = ["CameraState", "FakeSource", "NullSource", "Source", "default_line_config"]

# Tiempo base DETERMINISTA para los eventos (no se usa el reloj de pared: el
# event_id deriva de ts_event_ms y debe ser reproducible). 2023-11-14T22:13:20Z.
_BASE_TS_MS = 1_700_000_000_000
_STEP_MS = 100
# Cada cuántos frames se reemite un camera_status (heartbeat de la cámara).
_STATUS_EVERY = 25


@dataclass
class CameraState:
    """Estado en memoria por cámara (lo leen salud y MJPEG sin tocar la DB).

    Las lecturas son lock-free: cada atributo se actualiza con asignaciones
    atómicas bajo el GIL y los valores son monotónicos/idempotentes, así que un
    snapshot eventual-consistente es suficiente para salud y HUD.
    """

    camera_id: str
    frames_processed: int = 0
    last_inference_ts: int | None = None
    hailo_inference_ok: bool | None = None
    in_count: int = 0
    out_count: int = 0
    online: bool = False
    frame: bytes | None = None


def default_line_config(settings: Settings, camera_id: str) -> LineConfig:
    """``LineConfig`` por defecto: línea vertical central, sentido L->R = 'in'.

    Geometría normalizada 0..1. Con ``positive_side=-1`` el cruce de izquierda a
    derecha (el guion por defecto) cuenta como ``direction='in'`` (label positivo).
    """
    return LineConfig(
        site_id=settings.site_id,
        device_id=settings.device_id,
        camera_id=camera_id,
        config_version=0,
        line=Line(a=Point(0.5, 0.1), b=Point(0.5, 0.9)),
        positive_side=-1,
        positive_label="subieron",
        negative_label="bajaron",
    )


class NullSource:
    """Fuente inactiva (modo sin hardware): no produce frames ni cruces.

    Deja ``frames_processed=0`` (DISTINGUIBLE de salud real) y sirve un frame
    'sin señal'. La API sigue leyendo del SQLite local (counters/events/config).
    """

    def __init__(self, states: dict[str, CameraState]) -> None:
        self._states = states

    def start(self) -> None:
        for cam, st in self._states.items():
            st.frame = mjpeg.placeholder_frame(cam)
            st.online = False

    def stop(self) -> None:  # noqa: D102 — no-op
        return None


class FakeSource:
    """Fuente determinista en un hilo de fondo (escritor SQLite propio)."""

    def __init__(
        self,
        settings: Settings,
        states: dict[str, CameraState],
        hub: WsHub,
    ) -> None:
        self._settings = settings
        self._states = states
        self._hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for cam in self._states:
            self._states[cam].frame = mjpeg.placeholder_frame(cam, "iniciando")
        self._thread = threading.Thread(
            target=self._run, name="camcounter-fake-source", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    # -- bucle del hilo ---------------------------------------------------

    def _run(self) -> None:
        store = Store(self._settings.db_path)
        try:
            pipelines = self._build_pipelines(store)
            frame_index = 0
            while not self._stop.is_set():
                ts_ms = _BASE_TS_MS + frame_index * _STEP_MS
                for cam, parts in pipelines.items():
                    self._tick_camera(store, cam, parts, frame_index, ts_ms)
                frame_index += 1
                # Espera interrumpible: no bloquea el cierre del proceso.
                self._stop.wait(self._settings.frame_interval_s)
        finally:
            store.close()

    def _build_pipelines(self, store: Store) -> dict[str, dict[str, object]]:
        """Crea (y persiste si hace falta) la config y el pipeline por cámara."""
        pipelines: dict[str, dict[str, object]] = {}
        for cam in self._settings.camera_ids:
            # Persiste la config por defecto SÓLO si la cámara no tiene una (no
            # pisa ediciones del usuario tras un reinicio).
            if store.get_config_version(cam) == 0:
                try:
                    store.set_line_config(
                        cam, default_line_config(self._settings, cam), expected_version=0
                    )
                except StaleConfigVersionError:
                    pass
            config = store.get_line_config(cam)
            if config is None:
                config = default_line_config(self._settings, cam)
            counter = LineCounter.from_config(store, config, min_frames=2)
            pipelines[cam] = {
                # smooth_crossing_script: pasos finos -> el tracker IoU mantiene un
                # track_id estable y el LineCounter confirma 1 cruce por pasada
                # (incrementos deterministas del contador para los E2E por WS).
                "detector": DummyDetector(smooth_crossing_script(), loop=True),
                "tracker": CentroidIoUTracker(max_age=3),
                "counter": counter,
                "watcher": ConfigWatcher(
                    store, counter, cam, initial_version=config.config_version
                ),
            }
        return pipelines

    def _tick_camera(
        self,
        store: Store,
        camera_id: str,
        parts: dict[str, object],
        frame_index: int,
        ts_ms: int,
    ) -> None:
        detector: DummyDetector = parts["detector"]  # type: ignore[assignment]
        tracker: CentroidIoUTracker = parts["tracker"]  # type: ignore[assignment]
        counter: LineCounter = parts["counter"]  # type: ignore[assignment]
        watcher: ConfigWatcher = parts["watcher"]  # type: ignore[assignment]
        state = self._states[camera_id]

        # Hot-reload de config (relee config_version barato; recarga si cambió).
        if watcher.poll():
            self._publish(camera_id, "config_changed", ts_ms, {"config_version": watcher.version})

        detections = detector.detect()
        tracks = tracker.update(detections, ts=float(ts_ms))
        events = counter.process(tracks, ts_event_ms=ts_ms)
        for event in events:
            store.record_event(event)
            self._publish(
                camera_id,
                "crossing",
                ts_ms,
                {
                    "event_id": event.event_id,
                    "direction": event.direction,
                    "label": event.label,
                    "track_id": event.track_id,
                },
            )

        # Totales autoritativos desde el store (coherentes con REST y con resets).
        in_count, out_count = _counter_totals(store, camera_id)
        state.in_count = in_count
        state.out_count = out_count
        if events:
            self._publish(
                camera_id,
                "counter_update",
                ts_ms,
                {"in_count": in_count, "out_count": out_count, "net": in_count - out_count},
            )

        boxes = [d.bbox_norm for d in detections]
        line = ((counter.a[0], counter.a[1]), (counter.b[0], counter.b[1]))
        state.frame = mjpeg.render_frame(
            camera_id=camera_id,
            frame_index=frame_index,
            boxes=boxes,
            line=line,
            in_count=in_count,
            out_count=out_count,
        )
        state.frames_processed += 1
        state.last_inference_ts = ts_ms
        state.online = True
        # Sin Hailo real: el flag de inferencia Hailo NO aplica en fuente falsa.
        state.hailo_inference_ok = None

        if frame_index % _STATUS_EVERY == 0:
            self._publish(
                camera_id,
                "camera_status",
                ts_ms,
                {"online": True, "frames_processed": state.frames_processed},
            )

    def _publish(self, camera_id: str, type_: str, ts_ms: int, data: dict[str, object]) -> None:
        self._hub.publish_threadsafe(
            WsEnvelope(type=type_, camera_id=camera_id, ts_ms=ts_ms, data=data)  # type: ignore[arg-type]
        )


# Tipo del contrato (estructural): cualquier fuente con start()/stop().
# Es un Protocol -y no una union de NullSource|FakeSource|RtspSource- para
# evitar la dependencia circular (rtsp_source importa de fakes).
class Source(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


def _counter_totals(store: Store, camera_id: str) -> tuple[int, int]:
    """Suma los contadores 'in'/'out' de todas las jornadas de una cámara."""
    in_count = 0
    out_count = 0
    for row in store.get_counters(camera_id):
        if row["direction"] == "in":
            in_count += int(row["count"])
        elif row["direction"] == "out":
            out_count += int(row["count"])
    return in_count, out_count
