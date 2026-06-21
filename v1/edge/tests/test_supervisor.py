"""Tests del supervisor multi-cámara (sin hardware: DummyDetector + fakes).

Cubre:
- UN pipeline por cámara sobre un detector/VDevice COMPARTIDO con lock CORTO: el
  ``infer()`` se serializa (concurrencia máx = 1) y el lock está tomado SÓLO
  durante ``detect``.
- ``/healthz`` con salud de PRODUCTO: ``frames_processed`` creciente +
  ``last_inference_ts`` reciente -> 200 ``ok``.
- Caso "200 pero frames=0" -> DEGRADADO (503).
- Reinicio INDIVIDUAL de un pipeline caído sin tumbar a los demás.
"""

from __future__ import annotations

import threading
import time

from cam_counter_edge.app import (
    CameraPipeline,
    HealthRegistry,
    Supervisor,
    build_health_payload,
)
from cam_counter_edge.dummy import (
    DummyDetector,
    default_crossing_script,
    smooth_crossing_script,
)
from cam_counter_edge.line_counter import LineCounter
from cam_counter_edge.tracker import CentroidIoUTracker

SITE = "demo-site"
DEVICE = "demo-pi"


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeStore:
    """Store mínimo: ``next_crossing_seq`` monótono + ``record_event`` en memoria."""

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.events: list = []
        self._lock = threading.Lock()

    def next_crossing_seq(self, camera_id: str) -> int:
        with self._lock:
            self._seq[camera_id] = self._seq.get(camera_id, 0) + 1
            return self._seq[camera_id]

    def record_event(self, event) -> bool:
        with self._lock:
            self.events.append(event)
        return True


class _FakeSource:
    """Fuente de frames falsa: un frame por ``read`` a cadencia fija."""

    def __init__(self, stop: threading.Event, interval: float = 0.003) -> None:
        self._stop = stop
        self._interval = interval
        self._i = 0

    def read(self):
        if self._stop.is_set():
            return None
        self._stop.wait(self._interval)
        self._i += 1
        return {"frame_index": self._i}

    def close(self) -> None:
        return None


class _SerialAssertDetector:
    """Detector COMPARTIDO que verifica serialización del VDevice por el lock.

    Registra la concurrencia máxima observada dentro de ``detect`` (debe ser 1 si
    el lock corto serializa el ``infer()``) y comprueba que el lock compartido está
    TOMADO durante ``detect`` (lock corto alrededor de infer, no más).
    """

    def __init__(self, infer_lock: threading.Lock) -> None:
        self._infer_lock = infer_lock
        self._cur = 0
        self._counter_lock = threading.Lock()
        self.max_concurrent = 0
        self.calls = 0
        self.lock_held_every_call = True

    def detect(self, frame):
        if not self._infer_lock.locked():
            self.lock_held_every_call = False
        with self._counter_lock:
            self._cur += 1
            self.calls += 1
            self.max_concurrent = max(self.max_concurrent, self._cur)
        time.sleep(0.002)  # ensancha la ventana para detectar concurrencia
        with self._counter_lock:
            self._cur -= 1
        return []


def _make_counter(store, camera_id: str) -> LineCounter:
    return LineCounter(
        store=store,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=camera_id,
        a=(0.5, 0.1),
        b=(0.5, 0.9),
        positive_side=-1,
        min_frames=2,
    )


def test_shared_vdevice_lock_serializes_infer_across_cameras() -> None:
    """Dos cámaras comparten detector+lock: ``infer()`` NUNCA corre en paralelo."""
    cams = [f"{DEVICE}-cam0", f"{DEVICE}-cam1"]
    health = HealthRegistry(cams)
    store = _FakeStore()
    stop = threading.Event()
    infer_lock = threading.Lock()
    detector = _SerialAssertDetector(infer_lock)

    pipelines = [
        CameraPipeline(
            cam,
            frame_source=_FakeSource(stop),
            detector=detector,  # COMPARTIDO (un único VDevice simulado)
            infer_lock=infer_lock,  # COMPARTIDO
            tracker=CentroidIoUTracker(max_age=15),
            counter=_make_counter(store, cam),
            store=store,
            health=health,
            stop_event=stop,
        )
        for cam in cams
    ]
    for p in pipelines:
        p.start()
    try:
        assert _wait_until(lambda: detector.calls >= 8, timeout=5.0)
    finally:
        stop.set()
        for p in pipelines:
            p.stop()
            p.join(timeout=2.0)

    # El lock CORTO serializa el VDevice compartido: nunca dos infer a la vez.
    assert detector.max_concurrent == 1
    assert detector.lock_held_every_call is True
    # Ambas cámaras procesaron frames (salud por-cámara creciente).
    snap = {st.camera_id: st for st in health.snapshot()}
    for cam in cams:
        assert snap[cam].frames_processed > 0
        assert snap[cam].last_inference_ts is not None


def test_healthz_reports_product_health_when_frames_flow() -> None:
    """``/healthz`` -> 200 ``ok`` con ``frames_processed``/``last_inference_ts``."""
    cam = f"{DEVICE}-cam0"
    health = HealthRegistry([cam])
    store = _FakeStore()
    stop = threading.Event()
    infer_lock = threading.Lock()
    pipeline = CameraPipeline(
        cam,
        frame_source=_FakeSource(stop),
        detector=DummyDetector(smooth_crossing_script(), loop=True),
        infer_lock=infer_lock,
        tracker=CentroidIoUTracker(max_age=3),
        counter=_make_counter(store, cam),
        store=store,
        health=health,
        stop_event=stop,
    )
    pipeline.start()
    try:
        assert _wait_until(
            lambda: len(store.events) >= 1, timeout=5.0
        )
        code, payload = build_health_payload(health)
        assert code == 200
        assert payload["status"] == "ok"
        assert payload["frames_flowing"] is True
        cam_payload = payload["cameras"][0]
        assert cam_payload["frames_processed"] > 0
        assert cam_payload["last_inference_ts"] is not None
        assert cam_payload["healthy"] is True
    finally:
        stop.set()
        pipeline.stop()
        pipeline.join(timeout=2.0)

    # El conteo cruza la línea por el camino real (DummyDetector -> tracker -> count).
    assert len(store.events) >= 1


def test_healthz_degraded_when_camera_responds_but_frames_zero() -> None:
    """Una cámara que responde pero NO procesa frames (frames=0) -> 503 degradado."""
    cam = f"{DEVICE}-cam0"
    health = HealthRegistry([cam])
    # Nunca se registra un frame: frames_processed=0 (distinguible de salud real).
    code, payload = build_health_payload(health)
    assert code == 503
    assert payload["status"] == "degraded"
    assert payload["frames_flowing"] is False
    assert payload["cameras"][0]["frames_processed"] == 0
    assert payload["cameras"][0]["healthy"] is False


def test_supervisor_restarts_individual_crashed_pipeline() -> None:
    """Un pipeline caído se reinicia INDIVIDUALMENTE; los demás siguen vivos."""
    cam = f"{DEVICE}-cam0"
    health = HealthRegistry([cam])
    store = _FakeStore()
    stop = threading.Event()
    infer_lock = threading.Lock()
    builds = {"n": 0}

    class _BoomDetector:
        def detect(self, frame):
            raise RuntimeError("infer boom (pipeline debe caer)")

    def build_pipeline(camera_id: str) -> CameraPipeline:
        builds["n"] += 1
        # La PRIMERA encarnación revienta en infer; las siguientes están sanas.
        detector = _BoomDetector() if builds["n"] == 1 else DummyDetector(
            default_crossing_script(), loop=True
        )
        return CameraPipeline(
            camera_id,
            frame_source=_FakeSource(stop),
            detector=detector,
            infer_lock=infer_lock,
            tracker=CentroidIoUTracker(max_age=15),
            counter=_make_counter(store, camera_id),
            store=store,
            health=health,
            stop_event=stop,
        )

    supervisor = Supervisor(
        camera_ids=[cam],
        build_pipeline=build_pipeline,
        health=health,
        stop_event=stop,
    )
    supervisor.start()
    try:
        # El pipeline inicial cae (infer revienta).
        assert _wait_until(
            lambda: not supervisor.pipelines[cam].is_alive(), timeout=5.0
        )
        # El supervisor lo reinicia INDIVIDUALMENTE.
        restarted = supervisor.supervise_once()
        assert cam in restarted
        # Tras el reinicio, la cámara vuelve a procesar frames.
        assert _wait_until(
            lambda: health.snapshot()[0].frames_processed > 0, timeout=5.0
        )
    finally:
        supervisor.stop()

    assert builds["n"] >= 2
    assert health.snapshot()[0].restarts >= 1
