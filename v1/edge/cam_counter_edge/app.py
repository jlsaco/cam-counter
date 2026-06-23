"""Supervisor multi-cámara del borde: entrypoint ``cam-counter-edge``.

Cierra el lazo de ejecución en el Pi: carga la config de N cámaras, crea **UN
solo Hailo VDevice COMPARTIDO** con un **lock corto alrededor de ``infer()``**, y
lanza un ``CameraPipeline`` por cámara
(``capture -> detect -> track -> count -> present``) reusando colas ``maxsize=2``
que **descartan el frame viejo** (ir siempre "en vivo"). Un pipeline caído se
**reinicia INDIVIDUALMENTE** sin tumbar a los demás. Expone salud DE PRODUCTO
por-cámara en ``/healthz`` (no mera liveness): ``fps``, ``latency_ms``,
``hailo_busy``, ``frames_processed`` (creciente) y ``last_inference_ts``
(reciente). Distingue una cámara que responde pero NO procesa frames
(``frames_processed=0``) de una sana.

Presupuesto del VDevice compartido (ver doc de smoke EN-PI): ``4 cámaras *
~6.6ms/inferencia < 66ms`` deja margen para 15fps por cámara con un único
acelerador.

**Sin hardware (CI/x86):** toda la lógica se ejercita con ``DummyDetector`` y una
fuente de frames falsa; ``import cam_counter_edge.app`` NO requiere Hailo ni
cámara (igual que el ``Detector``: import perezoso). El ``Detector`` real y la
captura RTSP (cv2) se importan perezosamente sólo en el Pi.

**Coexistencia:** el servicio ``cam-counter-edge.service`` COEXISTE con el legacy
``hailo-personas`` (sin cutover; rollback = re-habilitar ``hailo-personas``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from .identifiers import make_camera_id, validate_camera_id
from .types import CrossingEvent

__all__ = [
    "CameraHealthState",
    "CameraPipeline",
    "HealthRegistry",
    "HealthServer",
    "Supervisor",
    "build_health_payload",
    "main",
]

_log = logging.getLogger(__name__)

# Tras cuántos segundos sin un nuevo frame se considera "stale" una cámara que
# antes procesaba (salud de producto: 200-pero-congelada es DEGRADADO).
DEFAULT_STALE_AFTER_S = 10.0
# Ventana (nº de frames) para estimar fps por cámara.
_FPS_WINDOW = 30


def _now_ms() -> int:
    """Epoch en milisegundos (para ``last_inference_ts``, reportado al exterior)."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Buffer "último frame" con descarte del viejo (cola maxsize=2 drop-old)
# --------------------------------------------------------------------------- #


class _LatestFrame:
    """Buffer acotado de frames que DESCARTA el más viejo al llenarse.

    ``deque(maxlen=n).append`` descarta el extremo opuesto automáticamente, así
    que la captura nunca bloquea y el consumidor siempre lee el frame MÁS reciente
    disponible (ir "en vivo"). ``get`` bloquea hasta ``timeout`` esperando frame.
    """

    def __init__(self, maxsize: int = 2) -> None:
        self._buf: deque[Any] = deque(maxlen=max(1, maxsize))
        self._cv = threading.Condition()
        self._closed = False

    def put(self, item: Any) -> None:
        with self._cv:
            self._buf.append(item)  # drop-old vía maxlen
            self._cv.notify()

    def get(self, timeout: float) -> Any | None:
        with self._cv:
            if not self._buf and not self._closed:
                self._cv.wait(timeout)
            return self._buf.popleft() if self._buf else None

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


# --------------------------------------------------------------------------- #
# Salud DE PRODUCTO por cámara
# --------------------------------------------------------------------------- #


@dataclass
class CameraHealthState:
    """Métricas de salud de PRODUCTO de UNA cámara (las lee ``/healthz``)."""

    camera_id: str
    frames_processed: int = 0
    last_inference_ts: int | None = None  # epoch ms; "reciente" si sana
    last_latency_ms: float | None = None
    fps: float = 0.0
    hailo_busy: bool = False
    alive: bool = True
    restarts: int = 0
    error: str | None = None
    _last_mono: float | None = field(default=None, repr=False)
    _ticks: deque[float] = field(default_factory=lambda: deque(maxlen=_FPS_WINDOW), repr=False)


class HealthRegistry:
    """Registro thread-safe de la salud por cámara (lo actualizan los pipelines)."""

    def __init__(self, camera_ids: list[str]) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, CameraHealthState] = {
            cam: CameraHealthState(camera_id=cam) for cam in camera_ids
        }

    def record_frame(self, camera_id: str, *, ts_ms: int, latency_ms: float) -> None:
        """Registra un frame procesado: ``frames_processed++`` + ts + latencia + fps."""
        now = time.monotonic()
        with self._lock:
            st = self._states[camera_id]
            st.frames_processed += 1
            st.last_inference_ts = ts_ms
            st.last_latency_ms = latency_ms
            st.alive = True
            st.error = None
            st._last_mono = now
            st._ticks.append(now)
            if len(st._ticks) >= 2:
                span = st._ticks[-1] - st._ticks[0]
                st.fps = (len(st._ticks) - 1) / span if span > 0 else 0.0

    def mark_busy(self, camera_id: str, busy: bool) -> None:
        """Marca si el VDevice Hailo está ocupado infiriendo para esta cámara."""
        with self._lock:
            self._states[camera_id].hailo_busy = busy

    def set_alive(self, camera_id: str, alive: bool, error: str | None = None) -> None:
        """Marca el estado vivo/caído de un pipeline (lo usa el supervisor)."""
        with self._lock:
            st = self._states[camera_id]
            st.alive = alive
            if error is not None:
                st.error = error
            if not alive:
                st.hailo_busy = False

    def inc_restart(self, camera_id: str) -> None:
        """Contabiliza un reinicio individual del pipeline de la cámara."""
        with self._lock:
            self._states[camera_id].restarts += 1

    def snapshot(self) -> list[CameraHealthState]:
        """Copia superficial de los estados (orden estable por inserción)."""
        with self._lock:
            return [
                CameraHealthState(
                    camera_id=st.camera_id,
                    frames_processed=st.frames_processed,
                    last_inference_ts=st.last_inference_ts,
                    last_latency_ms=st.last_latency_ms,
                    fps=st.fps,
                    hailo_busy=st.hailo_busy,
                    alive=st.alive,
                    restarts=st.restarts,
                    error=st.error,
                    _last_mono=st._last_mono,
                )
                for st in self._states.values()
            ]


def build_health_payload(
    registry: HealthRegistry,
    *,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    extra: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Construye ``(http_status, payload)`` de ``/healthz`` (salud de PRODUCTO).

    Una cámara está SANA si está viva, ha procesado ``frames_processed>0`` y su
    último frame NO es stale (``last_inference`` reciente). Si ALGUNA no está sana
    -> estado agregado ``degraded`` y HTTP ``503``; si todas sanas -> ``ok`` /
    ``200``. Así un ``200`` con ``frames=0`` es IMPOSIBLE: se reporta ``503``.
    """
    now = time.monotonic()
    cameras: list[dict[str, Any]] = []
    all_healthy = True
    states = registry.snapshot()
    for st in states:
        stale = st._last_mono is None or (now - st._last_mono) > stale_after_s
        processing = st.frames_processed > 0
        healthy = st.alive and processing and not stale
        all_healthy = all_healthy and healthy
        cameras.append(
            {
                "camera_id": st.camera_id,
                "alive": st.alive,
                "healthy": healthy,
                "frames_processed": st.frames_processed,
                "last_inference_ts": st.last_inference_ts,
                "latency_ms": (
                    round(st.last_latency_ms, 2) if st.last_latency_ms is not None else None
                ),
                "fps": round(st.fps, 2),
                "hailo_busy": st.hailo_busy,
                "stale": stale,
                "restarts": st.restarts,
                "error": st.error,
            }
        )
    status = "ok" if cameras and all_healthy else "degraded"
    payload: dict[str, Any] = {
        "status": status,
        "frames_flowing": any(c["frames_processed"] > 0 for c in cameras),
        "cameras": cameras,
    }
    if extra:
        payload.update(extra)
    return (200 if status == "ok" else 503), payload


class HealthServer:
    """Servidor HTTP mínimo (stdlib) que sirve ``/healthz`` con salud de producto."""

    def __init__(
        self,
        registry: HealthRegistry,
        *,
        host: str = "0.0.0.0",
        port: int = 8081,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        extra_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._registry = registry
        self._stale_after_s = stale_after_s
        self._extra_provider = extra_provider
        registry_ref = registry
        stale_ref = stale_after_s
        extra_ref = extra_provider

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # silencia el log por defecto
                return

            def do_GET(self) -> None:  # noqa: N802 (firma de BaseHTTPRequestHandler)
                if self.path.rstrip("/") not in ("/healthz", ""):
                    self.send_response(404)
                    self.end_headers()
                    return
                extra = extra_ref() if extra_ref is not None else None
                code, payload = build_health_payload(
                    registry_ref, stale_after_s=stale_ref, extra=extra
                )
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="healthz", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Interfaces mínimas (Protocols) de los componentes inyectables del pipeline
# --------------------------------------------------------------------------- #


class _FrameSource(Protocol):
    """Fuente de frames de una cámara: ``read()`` devuelve un frame o ``None``."""

    def read(self) -> Any | None: ...

    def close(self) -> None: ...


class _DetectorLike(Protocol):
    def detect(self, frame: Any) -> list[Any]: ...


class _TrackerLike(Protocol):
    def update(self, detections: Any, ts: float) -> list[Any]: ...


class _CounterLike(Protocol):
    def process(self, tracks: Any, ts_event_ms: int) -> list[CrossingEvent]: ...


class _RecordStore(Protocol):
    def record_event(self, event: CrossingEvent) -> bool: ...


# --------------------------------------------------------------------------- #
# Pipeline por cámara
# --------------------------------------------------------------------------- #


class CameraPipeline:
    """Pipeline de UNA cámara: captura -> detect (lock corto) -> track -> count.

    Usa DOS hilos: uno de CAPTURA que alimenta un buffer ``maxsize=2`` drop-old, y
    el de PROCESO (``run``) que consume el frame más reciente, infiere bajo el
    ``infer_lock`` COMPARTIDO (serializa el VDevice Hailo único), trackea, cuenta y
    presenta. Si la captura o el proceso lanzan, el hilo de proceso muere y marca
    la cámara como caída; el ``Supervisor`` la reinicia INDIVIDUALMENTE.

    Args:
        camera_id: cámara (slug validado).
        frame_source: fuente de frames (RTSP real o fake).
        detector: detector COMPARTIDO (un único VDevice) o por-cámara (fake).
        infer_lock: lock COMPARTIDO; se toma SÓLO alrededor de ``detector.detect``.
        tracker/counter: etapas track/count por-cámara.
        store: sink (``record_event``); típicamente una conexión SQLite por cámara.
        health: registro de salud a actualizar por frame.
        stop_event: señal de parada cooperativa.
        on_event: callback opcional por cada cruce (clip/present/WS).
        watcher_poll: callable opcional (hot-reload de config) llamado por frame.
        get_timeout: timeout de espera de frame (s).
    """

    def __init__(
        self,
        camera_id: str,
        *,
        frame_source: _FrameSource,
        detector: _DetectorLike,
        infer_lock: threading.Lock,
        tracker: _TrackerLike,
        counter: _CounterLike,
        store: _RecordStore,
        health: HealthRegistry,
        stop_event: threading.Event,
        on_event: Callable[[CrossingEvent], None] | None = None,
        on_frame: Callable[[Any, int], None] | None = None,
        watcher_poll: Callable[[], bool] | None = None,
        get_timeout: float = 1.0,
    ) -> None:
        self.camera_id = validate_camera_id(camera_id)
        self._source = frame_source
        self._detector = detector
        self._infer_lock = infer_lock
        self._tracker = tracker
        self._counter = counter
        self._store = store
        self._health = health
        self._stop = stop_event
        self._on_event = on_event
        self._on_frame = on_frame
        self._watcher_poll = watcher_poll
        self._get_timeout = get_timeout

        self._frames = _LatestFrame(maxsize=2)
        self._capture_thread: threading.Thread | None = None
        self._proc_thread: threading.Thread | None = None

    def start(self) -> None:
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name=f"capture-{self.camera_id}", daemon=True
        )
        self._proc_thread = threading.Thread(
            target=self.run, name=f"pipeline-{self.camera_id}", daemon=True
        )
        self._capture_thread.start()
        self._proc_thread.start()

    def is_alive(self) -> bool:
        return self._proc_thread is not None and self._proc_thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        if self._proc_thread is not None:
            self._proc_thread.join(timeout)

    def stop(self) -> None:
        self._frames.close()
        with contextlib.suppress(Exception):
            self._source.close()

    # -- hilos ------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Lee frames de la fuente y los empuja al buffer drop-old (no bloquea)."""
        while not self._stop.is_set():
            try:
                frame = self._source.read()
            except Exception:  # noqa: BLE001 — una captura caída no debe matar el proceso
                _log.exception("capture %s: fallo leyendo frame", self.camera_id)
                time.sleep(0.05)
                continue
            if frame is None:
                # Fuente sin frame ahora mismo (RTSP reconectando / fake agotada).
                continue
            self._frames.put(frame)

    def run(self) -> None:
        """Bucle de proceso: consume el último frame e infiere bajo el lock corto.

        Si una etapa lanza, el hilo TERMINA limpiamente (sin propagar al runtime de
        hilos) tras marcar la cámara como caída; el ``Supervisor`` detecta el hilo
        muerto (``is_alive()`` False) y reinicia el pipeline INDIVIDUALMENTE.
        """
        try:
            while not self._stop.is_set():
                frame = self._frames.get(self._get_timeout)
                if frame is None:
                    continue
                self._process_frame(frame)
        except Exception as exc:  # noqa: BLE001 — el supervisor reinicia el pipeline
            _log.exception("pipeline %s: caído", self.camera_id)
            self._health.set_alive(self.camera_id, False, error=repr(exc))

    def _process_frame(self, frame: Any) -> None:
        if self._watcher_poll is not None:
            self._watcher_poll()
        t0 = time.perf_counter()
        self._health.mark_busy(self.camera_id, True)
        try:
            # LOCK CORTO: serializa el VDevice Hailo único SÓLO durante infer().
            with self._infer_lock:
                detections = self._detector.detect(frame)
        finally:
            self._health.mark_busy(self.camera_id, False)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ts_ms = _now_ms()
        if self._on_frame is not None:
            # Alimenta el buffer de clips (pre-roll) con CADA frame, antes de que
            # un cruce de este mismo frame pida el clip (asi el pre-roll lo incluye).
            self._on_frame(frame, ts_ms)
        tracks = self._tracker.update(detections, ts=float(ts_ms))
        events = self._counter.process(tracks, ts_event_ms=ts_ms)
        for event in events:
            self._store.record_event(event)
            if self._on_event is not None:
                self._on_event(event)
        self._health.record_frame(self.camera_id, ts_ms=ts_ms, latency_ms=latency_ms)


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #


@dataclass
class Supervisor:
    """Supervisor multi-cámara: lanza y VIGILA un ``CameraPipeline`` por cámara.

    ``build_pipeline(camera_id) -> CameraPipeline`` construye un pipeline FRESCO
    (sin arrancar) para una cámara; el supervisor lo arranca y, si muere, lo
    RECONSTRUYE y reinicia INDIVIDUALMENTE (sin tumbar a las demás). El VDevice
    Hailo y el ``infer_lock`` COMPARTIDOS los inyecta ``build_pipeline`` (todas las
    cámaras comparten el mismo lock).
    """

    camera_ids: list[str]
    build_pipeline: Callable[[str], CameraPipeline]
    health: HealthRegistry
    stop_event: threading.Event = field(default_factory=threading.Event)
    supervise_interval_s: float = 0.5
    _pipelines: dict[str, CameraPipeline] = field(default_factory=dict, init=False)

    def start(self) -> None:
        """Construye y arranca todos los pipelines."""
        for cam in self.camera_ids:
            pipeline = self.build_pipeline(cam)
            self._pipelines[cam] = pipeline
            pipeline.start()

    def supervise_once(self) -> list[str]:
        """Reinicia los pipelines caídos. Devuelve los ``camera_id`` reiniciados.

        Determinista y sincrónico (para tests): no espera; reconstruye los muertos.
        """
        restarted: list[str] = []
        if self.stop_event.is_set():
            return restarted
        for cam in self.camera_ids:
            pipeline = self._pipelines.get(cam)
            if pipeline is not None and pipeline.is_alive():
                continue
            # Pipeline caído (o nunca arrancado): reinicia INDIVIDUALMENTE.
            if pipeline is not None:
                with contextlib.suppress(Exception):
                    pipeline.stop()
            self.health.inc_restart(cam)
            self.health.set_alive(cam, True, error=None)
            fresh = self.build_pipeline(cam)
            self._pipelines[cam] = fresh
            fresh.start()
            restarted.append(cam)
        return restarted

    def run(self) -> None:
        """Arranca y vigila hasta ``stop()``. Reinicia pipelines caídos en bucle."""
        self.start()
        while not self.stop_event.is_set():
            # No reiniciar en el PRIMER ciclo recién arrancado: deja correr.
            if self.stop_event.wait(self.supervise_interval_s):
                break
            self.supervise_once()

    def stop(self) -> None:
        """Señala parada y detiene todos los pipelines."""
        self.stop_event.set()
        for pipeline in self._pipelines.values():
            with contextlib.suppress(Exception):
                pipeline.stop()
        for pipeline in self._pipelines.values():
            pipeline.join(timeout=2.0)

    @property
    def pipelines(self) -> dict[str, CameraPipeline]:
        return dict(self._pipelines)


# --------------------------------------------------------------------------- #
# entrypoint (cam-counter-edge)
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    """Entrypoint ``cam-counter-edge``: arranca el supervisor + ``/healthz``.

    Lee la configuración del entorno (sin secretos en el repo). En modo
    ``CAMCOUNTER_FAKE_SOURCE=1`` usa ``DummyDetector`` + frames sintéticos (sin
    Pi/Hailo/cámara) para dev/diagnóstico. En el Pi usa el ``Detector`` Hailo
    (VDevice compartido) y captura RTSP; ambos se importan PEREZOSAMENTE para que
    este módulo importe sin hardware.
    """
    logging.basicConfig(level=logging.INFO)
    site_id = _env("CAMCOUNTER_SITE_ID", "demo-site")
    device_id = _env("CAMCOUNTER_DEVICE_ID", "demo-pi")
    try:
        camera_count = max(1, int(_env("CAMCOUNTER_CAMERA_COUNT", "1")))
    except ValueError:
        camera_count = 1
    db_path = _env("CAMCOUNTER_DB_PATH", "cam-counter.db")
    healthz_host = _env("CAMCOUNTER_HEALTHZ_HOST", "0.0.0.0")
    try:
        healthz_port = int(_env("CAMCOUNTER_HEALTHZ_PORT", "8081"))
    except ValueError:
        healthz_port = 8081
    fake = _env_flag("CAMCOUNTER_FAKE_SOURCE")

    camera_ids = [make_camera_id(device_id, n) for n in range(camera_count)]
    health = HealthRegistry(camera_ids)

    from .store import Store  # noqa: PLC0415  (perezoso; SQLite local)

    # Detector + lock COMPARTIDOS (un único VDevice). En fake, per-cámara dummy.
    infer_lock = threading.Lock()
    shared_detector: Any = None
    if not fake:
        from .detector import Detector  # noqa: PLC0415  (perezoso; Hailo en el Pi)

        shared_detector = Detector()  # VDevice compartido por todas las cámaras

    from .config import ConfigWatcher  # noqa: PLC0415  (hot-reload de la linea)
    from .dummy import DummyDetector, smooth_crossing_script  # noqa: PLC0415
    from .line_counter import LineCounter  # noqa: PLC0415
    from .tracker import CentroidIoUTracker  # noqa: PLC0415

    stop_event = threading.Event()

    # Grabador de clips COMPARTIDO (un hilo worker, conexion SQLite propia). Captura
    # un clip MP4/GIF (pre+post-roll) por cruce y encola su subida a S3 (sync).
    clip_recorder = None if fake else _build_clip_recorder(db_path)
    clip_encode = _make_clip_frame_encoder() if clip_recorder is not None else None

    def build_pipeline(camera_id: str) -> CameraPipeline:
        store = Store(db_path)
        config = store.get_line_config(camera_id)
        if config is None:
            # Sin config persistida: línea vertical central por defecto (L->R = in).
            from .types import Line, LineConfig, Point  # noqa: PLC0415

            config = LineConfig(
                site_id=site_id,
                device_id=device_id,
                camera_id=camera_id,
                config_version=0,
                line=Line(a=Point(0.5, 0.1), b=Point(0.5, 0.9)),
                positive_side=-1,
                positive_label="subieron",
                negative_label="bajaron",
            )
        counter = LineCounter.from_config(store, config, min_frames=2)
        # Hot-reload de la linea-umbral: relee config_version barato POR FRAME y, si
        # cambio (la UI guardo una nueva linea), reconfigura el LineCounter EN CALIENTE.
        watcher = ConfigWatcher(store, counter, camera_id, initial_version=config.config_version)
        tracker = CentroidIoUTracker(max_age=15)
        if fake:
            detector: Any = DummyDetector(smooth_crossing_script(), loop=True)
            frame_source: _FrameSource = _SyntheticFrameSource(stop_event)
        else:
            detector = shared_detector
            frame_source = _rtsp_source(camera_id, stop_event)
        on_event = None
        on_frame = None
        if clip_recorder is not None and clip_encode is not None:
            def on_event(ev: CrossingEvent, _r: Any = clip_recorder) -> None:
                _r.request_clip(ev)

            def on_frame(fr: Any, ts: int, _r: Any = clip_recorder,
                         _cam: str = camera_id, _enc: Any = clip_encode) -> None:
                jpeg = _enc(fr)
                if jpeg is not None:
                    _r.add_frame(_cam, jpeg, ts)

        return CameraPipeline(
            camera_id,
            frame_source=frame_source,
            detector=detector,
            infer_lock=infer_lock,
            tracker=tracker,
            counter=counter,
            store=store,
            health=health,
            stop_event=stop_event,
            on_event=on_event,
            on_frame=on_frame,
            watcher_poll=watcher.poll,
        )

    supervisor = Supervisor(
        camera_ids=camera_ids,
        build_pipeline=build_pipeline,
        health=health,
        stop_event=stop_event,
    )
    health_server = HealthServer(
        health,
        host=healthz_host,
        port=healthz_port,
        extra_provider=lambda: {
            "site_id": site_id,
            "device_id": device_id,
            "fake_source": fake,
        },
    )
    health_server.start()
    _log.info(
        "cam-counter-edge: %d cámara(s) %s; /healthz en %s:%d (fake=%s)",
        len(camera_ids),
        camera_ids,
        healthz_host,
        health_server.port,
        fake,
    )

    def _handle_signal(_signum: int, _frame: Any) -> None:
        _log.info("cam-counter-edge: señal recibida; parando…")
        stop_event.set()

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    try:
        supervisor.run()
    finally:
        supervisor.stop()
        if clip_recorder is not None:
            with contextlib.suppress(Exception):
                clip_recorder.close(timeout=5.0)
        health_server.stop()
    return 0


class _SyntheticFrameSource:
    """Fuente de frames sintética (modo fake): produce frames negros pequeños.

    El ``DummyDetector`` IGNORA el contenido del frame (secuencia guionizada), así
    que basta con un frame placeholder a una cadencia fija. Sin numpy real en el
    camino crítico de CI: devuelve un objeto liviano marcando el frame index.
    """

    def __init__(self, stop_event: threading.Event, interval_s: float = 0.05) -> None:
        self._stop = stop_event
        self._interval = interval_s
        self._i = 0

    def read(self) -> Any | None:
        if self._stop.is_set():
            return None
        self._stop.wait(self._interval)
        self._i += 1
        return {"frame_index": self._i}

    def close(self) -> None:
        return None


def _build_clip_recorder(db_path: str) -> Any:
    """Crea el ``ClipRecorder`` compartido (o ``None`` si los clips estan off).

    Gated por ``CAMCOUNTER_CLIPS_ENABLED`` (ON por defecto). Usa una conexion
    ``Store`` PROPIA (el worker del recorder escribe ``clip_uploads`` desde su
    hilo). Los clips se escriben en ``CAMCOUNTER_CLIP_DIR`` (def ``<db>/clips``).
    """
    if os.environ.get("CAMCOUNTER_CLIPS_ENABLED", "1").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return None
    from pathlib import Path  # noqa: PLC0415

    from .clip import ClipRecorder  # noqa: PLC0415  (perezoso: numpy/PIL/cv2)
    from .store import Store  # noqa: PLC0415

    out_dir = os.environ.get("CAMCOUNTER_CLIP_DIR") or str(
        Path(db_path).resolve().parent / "clips"
    )
    try:
        fps = float(os.environ.get("CAMCOUNTER_CLIP_FPS", "15"))
        pre = float(os.environ.get("CAMCOUNTER_CLIP_PRE_S", "2"))
        post = float(os.environ.get("CAMCOUNTER_CLIP_POST_S", "2"))
    except ValueError:
        fps, pre, post = 15.0, 2.0, 2.0
    clip_store = Store(db_path)
    _log.info(
        "cam-counter-edge: clips ON -> %s (fps=%s pre=%ss post=%ss)", out_dir, fps, pre, post
    )
    return ClipRecorder(clip_store, out_dir=out_dir, fps=fps, pre_seconds=pre, post_seconds=post)


def _make_clip_frame_encoder() -> Any:
    """Devuelve un encoder ``frame(BGR) -> bytes JPEG`` (cv2, reescalado).

    Reescala a ``CAMCOUNTER_CLIP_WIDTH``x``CAMCOUNTER_CLIP_HEIGHT`` (def 640x360)
    para clips ligeros y poco coste de CPU en el camino de conteo.
    """
    import cv2  # noqa: PLC0415  (perezoso; solo en el Pi)

    try:
        w = int(os.environ.get("CAMCOUNTER_CLIP_WIDTH", "640"))
        h = int(os.environ.get("CAMCOUNTER_CLIP_HEIGHT", "360"))
        q = int(os.environ.get("CAMCOUNTER_CLIP_QUALITY", "70"))
    except ValueError:
        w, h, q = 640, 360, 70
    params = [cv2.IMWRITE_JPEG_QUALITY, q]

    def encode(frame: Any) -> bytes | None:
        try:
            small = cv2.resize(frame, (w, h))
            ok, buf = cv2.imencode(".jpg", small, params)
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001 — un frame ilegible no debe romper el conteo
            return None

    return encode


def _rtsp_source(camera_id: str, stop_event: threading.Event) -> _FrameSource:
    """Construye una fuente RTSP (cv2, import perezoso) para una cámara en el Pi.

    La URL RTSP se lee de ``CAMCOUNTER_RTSP_<camera_id_sanitized>`` o
    ``CAMCOUNTER_RTSP_URL`` (no se commitea: credenciales de cámara por entorno).
    """
    url = os.environ.get(
        f"CAMCOUNTER_RTSP_{camera_id.replace('-', '_').upper()}"
    ) or os.environ.get("CAMCOUNTER_RTSP_URL", "")
    return _RtspFrameSource(url, stop_event)


class _RtspFrameSource:
    """Captura RTSP por OpenCV (cv2 import PEREZOSO; sólo en el Pi)."""

    def __init__(self, url: str, stop_event: threading.Event) -> None:
        self._url = url
        self._stop = stop_event
        self._cap: Any = None

    def _ensure(self) -> Any:
        if self._cap is None:
            import cv2  # noqa: PLC0415  (perezoso; sólo en el Pi)

            self._cap = cv2.VideoCapture(self._url)
        return self._cap

    def read(self) -> Any | None:
        cap = self._ensure()
        ok, frame = cap.read()
        if not ok:
            return None
        return frame

    def close(self) -> None:
        if self._cap is not None:
            with contextlib.suppress(Exception):
                self._cap.release()
            self._cap = None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
