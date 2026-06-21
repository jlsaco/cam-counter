"""Tests del supervisor multi-cámara (``app.Supervisor`` / ``CameraPipeline``).

Ejercitan en x86 sin Hailo ni cámara (detectores y fuentes fake):

- UN lock CORTO compartido serializa el "VDevice" entre pipelines (la inferencia
  de dos cámaras NUNCA se solapa),
- ``/healthz`` reporta salud DE PRODUCTO por-cámara (``frames_processed``,
  ``last_inference_ts``) y distingue "responde pero frames=0" como DEGRADADO,
- un pipeline caído se REINICIA INDIVIDUALMENTE sin tumbar a los demás.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from cam_counter_edge.app import CameraSpec, Supervisor
from cam_counter_edge.store import Store
from cam_counter_edge.types import Detection

A = (0.5, 0.0)
B = (0.5, 1.0)


def _spec(camera_id: str, device_id: str = "rpi-001") -> CameraSpec:
    return CameraSpec(
        site_id="sitio-demo",
        device_id=device_id,
        camera_id=camera_id,
        line_a=A,
        line_b=B,
        positive_side=1,
        positive_label="subieron",
        negative_label="bajaron",
    )


def _wait_until(predicate: Any, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Sondea ``predicate`` hasta que sea verdadero o expire el timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _ForeverSource:
    """Fuente fake: entrega un frame placeholder en cada lectura (sin fin)."""

    def __init__(self) -> None:
        self._frame = object()

    def read(self) -> Any | None:
        return self._frame

    def close(self) -> None:
        return None


class _NoneSource:
    """Fuente fake agotada: la primera lectura ya devuelve ``None`` (frames=0)."""

    def read(self) -> Any | None:
        return None

    def close(self) -> None:
        return None


class _ConcurrencyDetector:
    """Detector que detecta SOLAPAMIENTO de inferencias entre pipelines.

    Comparte un contador ``active``: si el lock del supervisor serializa el
    VDevice, ``active`` nunca supera 1. Si dos pipelines infirieran a la vez,
    ``max`` llegaría a 2 (delataría la ausencia del lock corto compartido).
    """

    def __init__(self, shared: dict[str, Any]) -> None:
        self._shared = shared

    def detect(self, frame_bgr: Any) -> list[Detection]:
        s = self._shared
        with s["guard"]:
            s["active"] += 1
            s["max"] = max(s["max"], s["active"])
        time.sleep(0.003)  # ventana en la que se vería un solapamiento
        with s["guard"]:
            s["active"] -= 1
        return []


class _BoomDetector:
    """Detector que SIEMPRE lanza: tumba SOLO su pipeline (reinicio individual)."""

    def detect(self, frame_bgr: Any) -> list[Detection]:
        raise RuntimeError("fallo de inferencia simulado")


def test_shared_lock_serializes_inference_across_cameras(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    shared = {"active": 0, "max": 0, "guard": threading.Lock()}
    specs = [_spec("rpi-001-cam0"), _spec("rpi-001-cam1")]

    sup = Supervisor(
        specs,
        store,
        detector_factory=lambda _cid: _ConcurrencyDetector(shared),
        source_factory=lambda _cid: _ForeverSource(),
    )
    sup.start(monitor=False)
    try:
        ok = _wait_until(
            lambda: all(
                sup._health[c.camera_id].frames_processed >= 5 for c in specs
            )
        )
        assert ok, "ambas cámaras deben procesar frames"
    finally:
        sup.stop()
        store.close()

    # Con el lock CORTO compartido, las inferencias jamás se solaparon.
    assert shared["max"] == 1


def test_healthz_reports_per_camera_product_metrics(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    shared = {"active": 0, "max": 0, "guard": threading.Lock()}
    specs = [_spec("rpi-001-cam0")]
    sup = Supervisor(
        specs,
        store,
        detector_factory=lambda _cid: _ConcurrencyDetector(shared),
        source_factory=lambda _cid: _ForeverSource(),
    )
    sup.start(monitor=False)
    try:
        assert _wait_until(
            lambda: sup._health["rpi-001-cam0"].frames_processed >= 3
        )
        code, body = sup.health_report()
    finally:
        sup.stop()
        store.close()

    assert code == 200 and body["status"] == "ok"
    cam = body["cameras"]["rpi-001-cam0"]
    assert cam["frames_processed"] >= 3
    assert cam["last_inference_ts"] is not None
    assert cam["healthy"] is True
    assert "hailo_busy" in cam and "latency_ms" in cam and "fps" in cam


def test_health_200_but_frames_zero_is_degraded(tmp_path: Any) -> None:
    """Una cámara VIVA pero que NO procesa frames (frames=0) => 503 degradado."""
    store = Store(str(tmp_path / "c.db"))
    specs = [_spec("rpi-001-cam0")]
    sup = Supervisor(
        specs,
        store,
        detector_factory=lambda _cid: _ConcurrencyDetector(
            {"active": 0, "max": 0, "guard": threading.Lock()}
        ),
        source_factory=lambda _cid: _NoneSource(),  # fuente agotada: frames=0
    )
    sup.start(monitor=False)
    try:
        # El worker está VIVO (responde) pero no hay frames que procesar.
        assert sup._health["rpi-001-cam0"].alive is True
        code, body = sup.health_report()
    finally:
        sup.stop()
        store.close()

    assert code == 503 and body["status"] == "degraded"
    cam = body["cameras"]["rpi-001-cam0"]
    assert cam["frames_processed"] == 0
    assert cam["healthy"] is False
    assert cam["stale"] is True


def test_individual_pipeline_restart_does_not_take_down_others(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    shared = {"active": 0, "max": 0, "guard": threading.Lock()}
    good_id, bad_id = "rpi-001-cam0", "rpi-001-cam1"
    specs = [_spec(good_id), _spec(bad_id)]

    def detector_factory(camera_id: str) -> Any:
        return _BoomDetector() if camera_id == bad_id else _ConcurrencyDetector(shared)

    sup = Supervisor(
        specs,
        store,
        detector_factory=detector_factory,
        source_factory=lambda _cid: _ForeverSource(),
        monitor_interval_s=0.05,
    )
    sup.start(monitor=False)
    try:
        # El pipeline malo muere; el bueno sigue procesando.
        assert _wait_until(lambda: sup._health[bad_id].alive is False)
        good_before = sup._health[good_id].frames_processed
        assert sup._health[good_id].alive is True

        # Reinicio INDIVIDUAL del caído: sólo sube su contador de restarts.
        restarted = sup.reap_once()
        assert restarted == 1
        assert sup._health[bad_id].restarts == 1
        assert sup._health[good_id].restarts == 0

        # El bueno siguió vivo y procesando durante el reinicio del otro.
        assert _wait_until(
            lambda: sup._health[good_id].frames_processed > good_before
        )
        assert sup._health[good_id].alive is True
    finally:
        sup.stop()
        store.close()
