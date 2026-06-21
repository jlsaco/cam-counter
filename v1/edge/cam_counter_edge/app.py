"""Supervisor multi-cámara del borde (entrypoint ``cam-counter-edge``).

Orquesta N cámaras sobre UN solo recurso de inferencia Hailo COMPARTIDO (un único
``VDevice``), con un ``threading.Lock`` CORTO alrededor de ``infer()``. Cada cámara
corre su propio ``CameraPipeline`` (``capture -> detect -> track -> count -> present``)
con colas ``maxsize=2`` que DESCARTAN el frame viejo (ir siempre "en vivo"). Un
pipeline que se cae se reinicia INDIVIDUALMENTE sin tumbar a los demás, y ``/healthz``
expone salud DE PRODUCTO por-cámara (no mera liveness): ``frames_processed`` creciente,
``last_inference_ts`` reciente, ``fps``, ``latency_ms`` y ``hailo_busy``.

Presupuesto del VDevice compartido (ver doc de smoke): con ~6.6 ms por inferencia,
4 cámaras serializadas por el lock caben en ``4 * 6.6ms < 66ms`` (holgura para 15 fps
por cámara). El lock se mantiene SÓLO alrededor de ``infer()`` (sección crítica corta);
captura, tracking, conteo y presentación corren fuera del lock.

Edge-first: el conteo persiste en SQLite local sin depender de la red. La subida a la
nube la hace el worker desacoplado de ``sync.py`` (no este supervisor).

Testabilidad: toda la lógica corre en x86/CI con ``DummyDetector`` (sin Hailo) y una
``FrameSource`` sintética; ``infer`` es un callable inyectable, de modo que un test puede
forzar "frames=0" (degradado), comprobar que el lock serializa ``infer`` y verificar el
reinicio de un pipeline caído. Los comportamientos de hardware real (Hailo/RTSP) van en el
checklist de smoke EN-PI, nunca como gate de CI.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from .line_counter import LineCounter
from .store import Store
from .tracker import CentroidIoUTracker
from .types import Detection, Line, LineConfig, Point

__all__ = [
    "CameraHealth",
    "CameraPipeline",
    "CameraSpec",
    "DropOldestQueue",
    "FrameSource",
    "HealthServer",
    "ScriptedFrameSource",
    "Supervisor",
    "main",
]

_log = logging.getLogger("cam_counter_edge.app")

# Tras este margen sin una inferencia nueva, una cámara con frames>0 se considera
# OBSOLETA (degradada): responde pero dejó de procesar. Configurable por env.
DEFAULT_STALE_AFTER_MS = 5000.0


def _now_ms() -> int:
    """Epoch ms de pared (salud/observabilidad; NO entra en contratos)."""
    return int(time.time() * 1000)


# ── frame sources ─────────────────────────────────────────────────────────────


class FrameSource(Protocol):
    """Fuente de frames de una cámara. ``read`` devuelve el frame o ``None``.

    ``None`` señala "sin frame disponible ahora" (fin del guion en tests, o stall de
    la cámara): el pipeline NO lo procesa (no incrementa ``frames_processed``), de modo
    que una cámara que nunca entrega frames se observa como ``frames=0`` (degradada).
    En producción una fuente RTSP (cv2) implementa esta interfaz.
    """

    def read(self) -> Any | None: ...


class ScriptedFrameSource:
    """Fuente determinista para CI/dev: entrega una secuencia fija de "frames".

    Cada frame es un objeto opaco para el supervisor (en los tests suele ser la lista
    de ``Detection`` que el ``infer`` inyectado devolverá tal cual). Al agotar la
    secuencia devuelve ``None`` (o reinicia con ``loop=True``). ``read`` puede bloquear
    artificialmente con ``block_after`` para simular una cámara colgada (frames=0).
    """

    def __init__(
        self,
        frames: list[Any],
        *,
        loop: bool = False,
        block: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._loop = loop
        self._block = block
        self._i = 0

    def read(self) -> Any | None:
        if self._block:
            # Cámara colgada: nunca entrega un frame (simula stall -> frames=0).
            time.sleep(0.005)
            return None
        if self._i >= len(self._frames):
            if self._loop and self._frames:
                self._i = 0
            else:
                return None
        frame = self._frames[self._i]
        self._i += 1
        return frame


# ── cola drop-old (maxsize=2, descarta el frame viejo) ─────────────────────────


class DropOldestQueue:
    """Cola acotada que, al estar llena, DESCARTA el elemento más viejo en ``put``.

    Mantiene el pipeline "en vivo": si el consumidor (detect) va más lento que la
    captura, se procesa siempre el frame MÁS RECIENTE y se tiran los atrasados, en
    lugar de acumular latencia. ``maxsize=2`` por defecto (un frame en proceso + uno
    en espera).
    """

    def __init__(self, maxsize: int = 2) -> None:
        self._q: queue.Queue[Any] = queue.Queue(maxsize=max(1, maxsize))
        self.dropped = 0

    def put(self, item: Any) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()  # descarta el más viejo
                    self.dropped += 1
                except queue.Empty:
                    pass

    def get(self, timeout: float | None = None) -> Any:
        return self._q.get(timeout=timeout)

    def qsize(self) -> int:
        return self._q.qsize()


# ── salud por cámara ───────────────────────────────────────────────────────────


@dataclass
class CameraHealth:
    """Métricas de salud DE PRODUCTO de una cámara (las sirve ``/healthz``)."""

    camera_id: str
    frames_processed: int = 0
    last_inference_ts: int | None = None  # epoch ms de la última inferencia OK
    fps: float = 0.0
    latency_ms: float = 0.0
    hailo_busy: bool = False
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=30), repr=False)

    def record_frame(self, *, latency_ms: float) -> None:
        """Contabiliza un frame procesado: ++frames, ts, fps y latencia."""
        now = time.monotonic()
        self._recent.append(now)
        self.frames_processed += 1
        self.last_inference_ts = _now_ms()
        self.latency_ms = float(latency_ms)
        if len(self._recent) >= 2:
            span = self._recent[-1] - self._recent[0]
            self.fps = round((len(self._recent) - 1) / span, 2) if span > 0 else 0.0

    def is_healthy(self, *, stale_after_ms: float, now_ms: int | None = None) -> bool:
        """Sana = corriendo, ha procesado >=1 frame y la última inferencia es RECIENTE.

        Una cámara que responde pero con ``frames_processed == 0`` NO es sana
        (degradada): es justo el caso que ``/healthz`` distingue de una sana.
        """
        if not self.running or self.frames_processed == 0 or self.last_inference_ts is None:
            return False
        now = _now_ms() if now_ms is None else now_ms
        return (now - self.last_inference_ts) <= stale_after_ms

    def snapshot(self, *, stale_after_ms: float) -> dict:
        """Dict serializable para ``/healthz`` (incluye el flag ``healthy``)."""
        return {
            "camera_id": self.camera_id,
            "frames_processed": self.frames_processed,
            "last_inference_ts": self.last_inference_ts,
            "fps": self.fps,
            "latency_ms": round(self.latency_ms, 2),
            "hailo_busy": self.hailo_busy,
            "running": self.running,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "healthy": self.is_healthy(stale_after_ms=stale_after_ms),
        }


# ── especificación de cámara ───────────────────────────────────────────────────


@dataclass
class CameraSpec:
    """Datos para construir un ``CameraPipeline`` (identidad + línea + fuente)."""

    site_id: str
    device_id: str
    camera_id: str
    line: LineConfig
    source_factory: Callable[[], FrameSource]
    min_frames: int = 2
    cooldown: int = 0


# ── pipeline por cámara ────────────────────────────────────────────────────────


class CameraPipeline:
    """Pipeline de UNA cámara: capture -> detect (locked) -> track -> count -> present.

    Dos hilos por cámara conectados por una ``DropOldestQueue`` (maxsize=2): el hilo de
    captura empuja frames (descartando los viejos) y el hilo de proceso infiere bajo el
    lock compartido, trackea, cuenta y persiste. Si CUALQUIER hilo lanza una excepción
    no controlada, el pipeline se marca CAÍDO (``health.running=False`` + ``last_error``)
    y termina sus hilos; el supervisor lo reinicia individualmente.

    Args:
        spec: identidad + línea + fábrica de fuente.
        infer: callable de inferencia COMPARTIDO ya serializado por el lock del
            supervisor (``infer(frame) -> list[Detection]``).
        store: ``Store`` SQLite (persistencia local; ``record_event`` idempotente).
        health: registro de salud de esta cámara (lo lee ``/healthz``).
        present: callback opcional de presentación (MJPEG/overlay); por defecto no-op.
        queue_maxsize: tamaño de la cola drop-old (default 2).
    """

    def __init__(
        self,
        spec: CameraSpec,
        *,
        infer: Callable[[Any], list[Detection]],
        store: Store,
        health: CameraHealth,
        present: Callable[[str, Any, list], None] | None = None,
        queue_maxsize: int = 2,
        iou_threshold: float = 0.3,
        max_age: int = 30,
    ) -> None:
        self.spec = spec
        self._infer = infer
        self._store = store
        self.health = health
        self._present = present
        self._queue = DropOldestQueue(maxsize=queue_maxsize)
        self._tracker = CentroidIoUTracker(iou_threshold=iou_threshold, max_age=max_age)
        self._counter = LineCounter.from_config(
            store, spec.line, min_frames=spec.min_frames, cooldown=spec.cooldown
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._crashed = threading.Event()

    @property
    def camera_id(self) -> str:
        return self.spec.camera_id

    def start(self) -> None:
        """Arranca los hilos de captura y proceso de la cámara."""
        self._stop.clear()
        self._crashed.clear()
        self.health.running = True
        self.health.last_error = None
        cap = threading.Thread(
            target=self._run_capture, name=f"cap-{self.camera_id}", daemon=True
        )
        proc = threading.Thread(
            target=self._run_process, name=f"proc-{self.camera_id}", daemon=True
        )
        self._threads = [cap, proc]
        cap.start()
        proc.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Señala parada y espera a los hilos (no propaga si no terminan a tiempo)."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=join_timeout)
        self.health.running = False

    def is_alive(self) -> bool:
        """``True`` si el pipeline sigue sano (no marcado caído y con hilos vivos)."""
        if self._crashed.is_set():
            return False
        return any(t.is_alive() for t in self._threads)

    def _fail(self, where: str, exc: BaseException) -> None:
        self.health.last_error = f"{where}: {type(exc).__name__}: {exc}"
        self.health.running = False
        self._crashed.set()
        _log.exception("pipeline %s caído en %s", self.camera_id, where)

    def _run_capture(self) -> None:
        source = self.spec.source_factory()
        try:
            while not self._stop.is_set():
                frame = source.read()
                if frame is None:
                    # Sin frame disponible: no encolar (la cámara queda 'frames=0'
                    # si nunca produce). Pequeña espera para no hacer busy-loop.
                    self._stop.wait(0.005)
                    continue
                self._queue.put(frame)
        except Exception as exc:  # noqa: BLE001 — aísla el fallo a este pipeline
            self._fail("capture", exc)

    def _run_process(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    frame = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                t0 = time.monotonic()
                self.health.hailo_busy = True
                try:
                    detections = self._infer(frame)
                finally:
                    self.health.hailo_busy = False
                ts_ms = _now_ms()
                tracks = self._tracker.update(detections, ts_ms / 1000.0)
                events = self._counter.process(tracks, ts_ms)
                for event in events:
                    self._store.record_event(event)
                if self._present is not None:
                    self._present(self.camera_id, frame, tracks)
                latency_ms = (time.monotonic() - t0) * 1000.0
                self.health.record_frame(latency_ms=latency_ms)
        except Exception as exc:  # noqa: BLE001 — aísla el fallo a este pipeline
            self._fail("process", exc)


# ── supervisor ─────────────────────────────────────────────────────────────────


class Supervisor:
    """Orquestador multi-cámara sobre un único recurso de inferencia compartido.

    Crea UN lock corto que serializa ``infer()`` (modelando el VDevice Hailo
    compartido), lanza un ``CameraPipeline`` por cámara y un hilo monitor que
    REINICIA individualmente cualquier pipeline caído sin afectar a los demás.

    Args:
        specs: una ``CameraSpec`` por cámara.
        detector: objeto con ``detect(frame) -> list[Detection]`` (Detector Hailo con
            VDevice compartido, o ``DummyDetector`` en CI). Sus ``detect`` se serializan
            con el lock interno (sección crítica corta).
        store: ``Store`` SQLite compartido por todas las cámaras.
        stale_after_ms: margen para considerar una cámara obsoleta en ``/healthz``.
        monitor_interval_s: cada cuánto el monitor revisa y reinicia pipelines caídos.
    """

    def __init__(
        self,
        specs: list[CameraSpec],
        *,
        detector: Any,
        store: Store,
        stale_after_ms: float = DEFAULT_STALE_AFTER_MS,
        monitor_interval_s: float = 0.2,
        present: Callable[[str, Any, list], None] | None = None,
    ) -> None:
        self._specs = list(specs)
        self._detector = detector
        self._store = store
        self._stale_after_ms = float(stale_after_ms)
        self._monitor_interval_s = float(monitor_interval_s)
        self._present = present
        self._infer_lock = threading.Lock()
        self._health: dict[str, CameraHealth] = {
            s.camera_id: CameraHealth(camera_id=s.camera_id) for s in self._specs
        }
        self._pipelines: dict[str, CameraPipeline] = {}
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        # Diagnóstico del lock: nº máximo de inferencias concurrentes observadas.
        # Debe ser 1 SIEMPRE (el lock serializa infer); lo verifica un test.
        self._concurrent_infer = 0
        self._max_concurrent_infer = 0
        self._infer_diag_lock = threading.Lock()

    def _locked_infer(self, frame: Any) -> list[Detection]:
        """Ejecuta ``detector.detect`` bajo el lock corto del recurso compartido."""
        with self._infer_lock:
            with self._infer_diag_lock:
                self._concurrent_infer += 1
                self._max_concurrent_infer = max(
                    self._max_concurrent_infer, self._concurrent_infer
                )
            try:
                return self._detector.detect(frame)
            finally:
                with self._infer_diag_lock:
                    self._concurrent_infer -= 1

    @property
    def max_concurrent_infer(self) -> int:
        """Máximo de inferencias simultáneas observadas (debe ser 1 con el lock)."""
        return self._max_concurrent_infer

    def _build_pipeline(self, spec: CameraSpec) -> CameraPipeline:
        return CameraPipeline(
            spec,
            infer=self._locked_infer,
            store=self._store,
            health=self._health[spec.camera_id],
            present=self._present,
        )

    def start(self) -> None:
        """Arranca todos los pipelines y el hilo monitor de reinicio."""
        self._stop.clear()
        for spec in self._specs:
            pipe = self._build_pipeline(spec)
            self._pipelines[spec.camera_id] = pipe
            pipe.start()
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="supervisor-monitor", daemon=True
        )
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            for spec in self._specs:
                if self._stop.is_set():
                    break
                pipe = self._pipelines.get(spec.camera_id)
                if pipe is not None and not pipe.is_alive():
                    self._restart(spec)
            self._stop.wait(self._monitor_interval_s)

    def _restart(self, spec: CameraSpec) -> None:
        """Reinicia INDIVIDUALMENTE un pipeline caído (sin tocar los demás)."""
        old = self._pipelines.get(spec.camera_id)
        if old is not None:
            old.stop(join_timeout=0.5)
        health = self._health[spec.camera_id]
        health.restarts += 1
        _log.warning(
            "supervisor: reiniciando pipeline %s (reinicios=%d, último error=%s)",
            spec.camera_id,
            health.restarts,
            health.last_error,
        )
        pipe = self._build_pipeline(spec)
        self._pipelines[spec.camera_id] = pipe
        pipe.start()

    def stop(self) -> None:
        """Detiene el monitor y todos los pipelines."""
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        for pipe in self._pipelines.values():
            pipe.stop()

    def health_snapshot(self) -> dict:
        """Salud agregada para ``/healthz``: por-cámara + estado global.

        ``status`` ∈ {'ok','degraded'} y ``healthy`` global = todas las cámaras sanas.
        Una cámara con ``frames_processed==0`` (responde pero no procesa) hace que el
        agregado sea ``degraded`` (la capa HTTP responde 503).
        """
        now_ms = _now_ms()
        cameras = [
            h.snapshot(stale_after_ms=self._stale_after_ms)
            for h in self._health.values()
        ]
        all_healthy = bool(cameras) and all(c["healthy"] for c in cameras)
        return {
            "status": "ok" if all_healthy else "degraded",
            "healthy": all_healthy,
            "ts_ms": now_ms,
            "max_concurrent_infer": self._max_concurrent_infer,
            "cameras": cameras,
        }


# ── servidor HTTP de /healthz ──────────────────────────────────────────────────


class HealthServer:
    """Servidor HTTP mínimo (stdlib) que sirve ``/healthz`` con salud de producto.

    Responde 200 si TODAS las cámaras están sanas; 503 si alguna está degradada
    (incluida la que responde pero no procesa frames). El cuerpo es el
    ``health_snapshot`` del supervisor en JSON.
    """

    def __init__(self, supervisor: Supervisor, *, host: str = "0.0.0.0", port: int = 8081) -> None:
        self._supervisor = supervisor
        sup = supervisor

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silencia el log por request
                pass

            def do_GET(self) -> None:  # noqa: N802 (API de BaseHTTPRequestHandler)
                if self.path.rstrip("/") not in ("/healthz", "/health"):
                    self.send_response(404)
                    self.end_headers()
                    return
                snap = sup.health_snapshot()
                body = json.dumps(snap).encode("utf-8")
                self.send_response(200 if snap["healthy"] else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

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


# ── entrypoint ──────────────────────────────────────────────────────────────────


def _default_line(site_id: str, device_id: str, camera_id: str) -> LineConfig:
    """Línea vertical por defecto en x=0.5 (config inicial si no hay en SQLite)."""
    return LineConfig(
        site_id=site_id,
        device_id=device_id,
        camera_id=camera_id,
        config_version=1,
        line=Line(a=Point(0.5, 0.0), b=Point(0.5, 1.0)),
        positive_side=1,
        positive_label="subieron",
        negative_label="bajaron",
    )


def _dummy_source_factory() -> Callable[[], FrameSource]:
    """Fábrica de fuente sintética: bucle del guion de cruce canónico.

    Cada "frame" es una lista de ``Detection`` que el ``DummyDetector`` ignora (su
    secuencia es independiente del frame). Sirve para arrancar el supervisor en modo
    demo/CI sin cámara ni Hailo.
    """
    from .dummy import default_crossing_script

    def factory() -> FrameSource:
        frames: list[Any] = [list(f) for f in default_crossing_script()]
        return ScriptedFrameSource(frames, loop=True)

    return factory


def _build_supervisor_from_env(store: Store) -> tuple[Supervisor, int]:
    """Construye un ``Supervisor`` en modo dummy desde variables de entorno.

    Variables:
        CAMCOUNTER_SITE_ID (default 'sitio-demo'), CAMCOUNTER_DEVICE_ID (default
        'rpi-001'), CAMCOUNTER_NUM_CAMERAS (default 1), CAMCOUNTER_HEALTH_PORT
        (default 8081), CAMCOUNTER_STALE_AFTER_MS (default 5000).

    En el Pi real (sin CAMCOUNTER_DUMMY) se sustituiría el ``DummyDetector`` por un
    ``Detector`` con VDevice compartido y la fuente sintética por una RTSP; aquí la
    ruta tester/demo usa dummy para arrancar sin hardware.
    """
    from .dummy import DummyDetector
    from .identifiers import make_camera_id

    site_id = os.environ.get("CAMCOUNTER_SITE_ID", "sitio-demo")
    device_id = os.environ.get("CAMCOUNTER_DEVICE_ID", "rpi-001")
    num = int(os.environ.get("CAMCOUNTER_NUM_CAMERAS", "1"))
    port = int(os.environ.get("CAMCOUNTER_HEALTH_PORT", "8081"))
    stale = float(os.environ.get("CAMCOUNTER_STALE_AFTER_MS", str(DEFAULT_STALE_AFTER_MS)))

    source_factory = _dummy_source_factory()
    specs: list[CameraSpec] = []
    for n in range(num):
        camera_id = make_camera_id(device_id, n)
        line = store.get_line_config(camera_id) or _default_line(site_id, device_id, camera_id)
        specs.append(
            CameraSpec(
                site_id=site_id,
                device_id=device_id,
                camera_id=camera_id,
                line=line,
                source_factory=source_factory,
            )
        )

    # DummyDetector compartido + loop (modo demo/CI sin Hailo). El lock del supervisor
    # serializa sus llamadas igual que serializaría el VDevice real.
    detector = DummyDetector(loop=True)
    supervisor = Supervisor(specs, detector=detector, store=store, stale_after_ms=stale)
    return supervisor, port


def main() -> int:
    """Entrypoint ``cam-counter-edge``: arranca supervisor + ``/healthz`` y espera.

    Modo demo/CI (``CAMCOUNTER_DUMMY=1`` o por defecto sin Hailo): usa
    ``DummyDetector`` y una fuente sintética para ejercitar TODO el lazo sin hardware.
    El proceso corre hasta SIGINT/SIGTERM. El conteo persiste en SQLite; la subida a la
    nube la realiza por separado el worker de ``sync.py``.
    """
    logging.basicConfig(level=logging.INFO)
    db_path = os.environ.get("CAMCOUNTER_DB_PATH", "cam_counter.db")
    store = Store(db_path)
    supervisor, port = _build_supervisor_from_env(store)
    health = HealthServer(supervisor, port=port)

    supervisor.start()
    health.start()
    _log.info("cam-counter-edge arrancado; /healthz en :%d", health.port)
    stop = threading.Event()

    import signal

    def _handle(_signum: int, _frame: Any) -> None:
        _log.info("señal recibida; deteniendo cam-counter-edge")
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        health.stop()
        supervisor.stop()
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
