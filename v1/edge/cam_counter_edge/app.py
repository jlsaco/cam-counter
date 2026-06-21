"""Supervisor multi-cámara: UN Hailo VDevice COMPARTIDO sirve N pipelines.

Cierra el lazo de ejecución multi-cámara del borde. El proceso ``cam-counter-edge``
crea **un solo** Hailo VDevice y un ``threading.Lock`` CORTO alrededor de la
inferencia (``detect``), y lanza un ``CameraPipeline`` por cámara
(``capture -> detect -> track -> count -> present``). Cada pipeline reusa colas
``maxsize=2`` que DESCARTAN el frame viejo (ir siempre "en vivo", nunca acumular
latencia). Un pipeline que muere se REINICIA INDIVIDUALMENTE sin tumbar a los
demás.

Presupuesto del VDevice compartido (ver doc de smoke): con ~6.6 ms de inferencia
por cámara, 4 cámaras serializadas por el lock caben en
``4 * 6.6 ms = 26.4 ms < 66 ms`` (margen para 15 fps por cámara).

``/healthz`` reporta salud DE PRODUCTO por-cámara (no mera liveness): ``fps``,
``latency_ms``, ``hailo_busy``, ``frames_processed`` (creciente) y
``last_inference_ts`` (reciente). Distingue una cámara que responde 200 pero NO
procesa frames (``frames_processed == 0`` o ``last_inference_ts`` rancio) de una
sana, y agrega el estado (200 si todas sanas; 503/``degraded`` si alguna no
procesa).

Sin hardware (CI x86): se inyecta ``DummyDetector`` (o cualquier detector que
exponga ``detect(frame) -> list[Detection]``) y una fuente de frames fake; TODA la
lógica del supervisor (lock compartido, drop-old, reinicio, ``/healthz``) se
ejercita sin Hailo ni cámara. El import de ``hailo_platform``/``cv2`` es PEREZOSO
(sólo en el Pi).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .line_counter import LineCounter
from .store import Store
from .tracker import CentroidIoUTracker, Tracker
from .types import Detection

__all__ = [
    "CameraPipeline",
    "CameraHealth",
    "CameraSpec",
    "Supervisor",
    "build_dummy_supervisor",
    "main",
]

_log = logging.getLogger("cam_counter_edge.app")

# Una cámara se considera "rancia" (degradada) si no procesa un frame en este
# intervalo. Holgado frente a 15 fps; ajustable por config en el futuro.
DEFAULT_STALE_AFTER_S = 5.0


class _Detector(Protocol):
    """Forma mínima de un detector: ``detect(frame) -> list[Detection]``."""

    def detect(self, frame_bgr: Any) -> list[Detection]: ...


class FrameSource(Protocol):
    """Fuente de frames de una cámara (RTSP en el Pi; fake en CI).

    ``read`` devuelve el siguiente frame o ``None`` cuando la fuente se agota
    (en cuyo caso el pipeline deja de capturar y queda inactivo, no muere).
    """

    def read(self) -> Any | None: ...
    def close(self) -> None: ...


@dataclass
class CameraSpec:
    """Especificación de una cámara para el supervisor (multi-cámara/multi-sitio).

    Los identificadores son slugs validados aguas abajo (por ``LineCounter`` y el
    store). La geometría de la línea va en floats normalizados 0..1.
    """

    site_id: str
    device_id: str
    camera_id: str
    line_a: tuple[float, float]
    line_b: tuple[float, float]
    positive_side: int
    positive_label: str | None = None
    negative_label: str | None = None
    line_version: int = 1
    min_frames: int = 2
    rtsp_url: str | None = None


@dataclass
class CameraHealth:
    """Métricas de salud DE PRODUCTO por cámara (las sirve ``/healthz``).

    Mutada por el hilo de proceso del pipeline; leída por ``/healthz``. Los campos
    son tipos atómicos: en CPython las lecturas/escrituras simples son seguras
    para el caso de un único escritor + lectores.
    """

    camera_id: str
    frames_processed: int = 0
    last_inference_ts_ms: int | None = None
    last_inference_monotonic: float | None = None
    latency_ms: float = 0.0
    fps: float = 0.0
    hailo_busy: bool = False
    alive: bool = False
    restarts: int = 0
    events_total: int = 0
    last_error: str | None = None

    def snapshot(self, now_monotonic: float, stale_after_s: float) -> dict[str, Any]:
        """Vista serializable + bandera ``healthy`` (procesa frames y no rancio)."""
        last = self.last_inference_monotonic
        fresh = last is not None and (now_monotonic - last) <= stale_after_s
        healthy = self.alive and self.frames_processed > 0 and fresh
        return {
            "camera_id": self.camera_id,
            "frames_processed": self.frames_processed,
            "last_inference_ts": self.last_inference_ts_ms,
            "latency_ms": round(self.latency_ms, 2),
            "fps": round(self.fps, 2),
            "hailo_busy": self.hailo_busy,
            "alive": self.alive,
            "restarts": self.restarts,
            "events_total": self.events_total,
            "stale": not fresh,
            "healthy": healthy,
            "last_error": self.last_error,
        }


def _put_drop_old(q: queue.Queue[Any], item: Any) -> None:
    """Encola ``item`` descartando el MÁS VIEJO si la cola (``maxsize=2``) llena.

    Mantiene el pipeline "en vivo": nunca acumula backlog de frames (la latencia
    no crece sin control); siempre se procesa el frame más reciente disponible.
    """
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()  # descarta el frame viejo
            except queue.Empty:
                pass


class CameraPipeline:
    """Pipeline de UNA cámara: ``capture -> detect -> track -> count -> present``.

    Dos hilos por cámara: *capture* (lee la fuente y encola con drop-old) y
    *worker* (infiere bajo el LOCK COMPARTIDO, trackea, cuenta y persiste). El lock
    se comparte entre TODOS los pipelines para serializar el acceso al único
    Hailo VDevice; se mantiene SÓLO alrededor de ``detect`` (lock corto).

    Args:
        spec: identidad + línea de la cámara.
        detector: detector con ``detect(frame) -> list[Detection]`` (Dummy en CI).
        source: fuente de frames (RTSP en el Pi; fake en CI).
        store: store SQLite local (persistencia de eventos).
        infer_lock: lock COMPARTIDO del VDevice (corto, sólo en ``detect``).
        health: registro de métricas de la cámara (mutado por el worker).
        clock: reloj monotónico inyectable (tests deterministas).
        wall_clock_ms: reloj de pared en ms inyectable (timestamps de evento).
        stale_after_s: umbral de "rancio" para ``/healthz``.
    """

    def __init__(
        self,
        spec: CameraSpec,
        detector: _Detector,
        source: FrameSource,
        store: Store,
        infer_lock: threading.Lock,
        health: CameraHealth,
        *,
        tracker: Tracker | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self.spec = spec
        self._detector = detector
        self._source = source
        self._store = store
        self._lock = infer_lock
        self.health = health
        self._tracker = tracker or CentroidIoUTracker()
        self._clock = clock
        self._wall_ms = wall_clock_ms
        self._counter = LineCounter(
            store=store,
            site_id=spec.site_id,
            device_id=spec.device_id,
            camera_id=spec.camera_id,
            a=spec.line_a,
            b=spec.line_b,
            positive_side=spec.positive_side,
            positive_label=spec.positive_label,
            negative_label=spec.negative_label,
            line_version=spec.line_version,
            min_frames=spec.min_frames,
        )
        self._frames: queue.Queue[Any] = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # El frame más reciente anotado, para el stream MJPEG de present (en el Pi).
        self.latest_frame: Any | None = None

    # -- ciclo de vida ----------------------------------------------------

    def start(self) -> None:
        """Arranca los hilos *capture* y *worker* de la cámara."""
        self._stop.clear()
        self.health.alive = True
        self.health.last_error = None
        cap = threading.Thread(
            target=self._capture_loop, name=f"cap-{self.spec.camera_id}", daemon=True
        )
        work = threading.Thread(
            target=self._worker_loop, name=f"work-{self.spec.camera_id}", daemon=True
        )
        self._threads = [cap, work]
        cap.start()
        work.start()

    def stop(self) -> None:
        """Para los hilos y cierra la fuente (idempotente)."""
        self._stop.set()
        try:
            self._source.close()
        except Exception:  # noqa: BLE001 (cierre best-effort)
            pass

    def join(self, timeout: float | None = None) -> None:
        """Espera a que terminen los hilos del pipeline."""
        for t in self._threads:
            t.join(timeout)

    @property
    def alive(self) -> bool:
        """¿El pipeline sigue procesando (worker vivo)?"""
        return self.health.alive

    # -- hilos ------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Lee frames de la fuente y los encola con DROP-OLD (siempre en vivo)."""
        try:
            while not self._stop.is_set():
                frame = self._source.read()
                if frame is None:
                    # Fuente agotada (p.ej. fake de N frames): dejamos de capturar
                    # pero NO marcamos error; el worker drena lo encolado y espera.
                    return
                _put_drop_old(self._frames, frame)
        except Exception as exc:  # noqa: BLE001 (aislamos el fallo a esta cámara)
            self.health.last_error = f"capture: {exc}"

    def _worker_loop(self) -> None:
        """Infiere (lock corto), trackea, cuenta y persiste. Aísla sus fallos.

        Una excepción aquí marca ``alive=False`` y termina el worker; el monitor
        del supervisor reinicia ESTE pipeline sin afectar a los demás.
        """
        try:
            while not self._stop.is_set():
                try:
                    frame = self._frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._process_frame(frame)
        except Exception as exc:  # noqa: BLE001 (reinicio individual por el monitor)
            self.health.last_error = f"worker: {exc}"
            _log.warning("pipeline %s caído: %s", self.spec.camera_id, exc)
        finally:
            self.health.alive = False
            self.health.hailo_busy = False

    def _process_frame(self, frame: Any) -> None:
        """Un frame: detect (bajo lock) -> track -> count -> persist + métricas."""
        ts_ms = self._wall_ms()
        t0 = self._clock()
        # LOCK CORTO: sólo alrededor de la inferencia en el VDevice compartido.
        with self._lock:
            self.health.hailo_busy = True
            try:
                detections = self._detector.detect(frame)
            finally:
                self.health.hailo_busy = False
        latency_s = self._clock() - t0
        tracks = self._tracker.update(detections, ts=ts_ms / 1000.0)
        for event in self._counter.process(tracks, ts_event_ms=ts_ms):
            if self._store.record_event(event):
                self.health.events_total += 1
        self.latest_frame = frame
        # Métricas de salud (un único escritor: este worker).
        self.health.latency_ms = latency_s * 1000.0
        prev = self.health.last_inference_monotonic
        now = self._clock()
        if prev is not None and now > prev:
            # EMA suave del fps instantáneo para suavizar el jitter por-frame.
            inst = 1.0 / (now - prev)
            self.health.fps = (
                inst if self.health.fps == 0.0 else 0.7 * self.health.fps + 0.3 * inst
            )
        self.health.last_inference_monotonic = now
        self.health.last_inference_ts_ms = ts_ms
        self.health.frames_processed += 1


class Supervisor:
    """Orquesta N ``CameraPipeline`` sobre un VDevice compartido + ``/healthz``.

    Crea un lock CORTO compartido por todos los pipelines (serializa el VDevice),
    los arranca, vigila su salud y REINICIA INDIVIDUALMENTE el que muera. El
    detector y la fuente de cada cámara se obtienen de *factories* inyectables, de
    modo que un reinicio reconstruye SÓLO ese pipeline (Dummy + fake en CI; Hailo
    compartido + RTSP en el Pi).

    Args:
        specs: cámaras a servir.
        store: store SQLite local compartido (multi-cámara en una sola DB).
        detector_factory: ``camera_id -> detector`` (reusa el VDevice compartido).
        source_factory: ``camera_id -> FrameSource``.
        infer_lock: lock compartido (uno nuevo por defecto).
        monitor_interval_s: cada cuánto el monitor revisa/reinicia pipelines.
        stale_after_s: umbral de "rancio" de ``/healthz``.
        clock / wall_clock_ms: relojes inyectables (tests).
    """

    def __init__(
        self,
        specs: list[CameraSpec],
        store: Store,
        detector_factory: Callable[[str], _Detector],
        source_factory: Callable[[str], FrameSource],
        *,
        infer_lock: threading.Lock | None = None,
        monitor_interval_s: float = 0.5,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._specs = {s.camera_id: s for s in specs}
        self._store = store
        self._detector_factory = detector_factory
        self._source_factory = source_factory
        self._lock = infer_lock or threading.Lock()
        self._monitor_interval = monitor_interval_s
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._wall_ms = wall_clock_ms
        self._pipelines: dict[str, CameraPipeline] = {}
        self._health: dict[str, CameraHealth] = {
            cid: CameraHealth(camera_id=cid) for cid in self._specs
        }
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def infer_lock(self) -> threading.Lock:
        """El lock CORTO compartido por todos los pipelines (serializa el VDevice)."""
        return self._lock

    def _build_pipeline(self, camera_id: str) -> CameraPipeline:
        """Construye (no arranca) el pipeline de una cámara con sus factories."""
        spec = self._specs[camera_id]
        return CameraPipeline(
            spec,
            self._detector_factory(camera_id),
            self._source_factory(camera_id),
            self._store,
            self._lock,
            self._health[camera_id],
            clock=self._clock,
            wall_clock_ms=self._wall_ms,
            stale_after_s=self._stale_after_s,
        )

    def start(self, *, monitor: bool = True) -> None:
        """Arranca todos los pipelines y (opcional) el hilo monitor de reinicio."""
        self._stop.clear()
        for cid in self._specs:
            pipe = self._build_pipeline(cid)
            self._pipelines[cid] = pipe
            pipe.start()
        if monitor:
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="supervisor-monitor", daemon=True
            )
            self._monitor.start()

    def _monitor_loop(self) -> None:
        """Vigila los pipelines y REINICIA el que haya muerto, uno a uno."""
        while not self._stop.is_set():
            self.reap_once()
            self._stop.wait(self._monitor_interval)

    def reap_once(self) -> int:
        """Reinicia los pipelines muertos (un pase). Devuelve cuántos reinició.

        Aislado y testeable: el test fuerza la caída de UN pipeline y verifica que
        SÓLO ese se reinicia (sube ``restarts``) mientras los demás siguen vivos.
        """
        restarted = 0
        for cid, pipe in list(self._pipelines.items()):
            if self._stop.is_set():
                break
            if not pipe.health.alive:
                _log.info("reiniciando pipeline %s", cid)
                pipe.stop()
                self._health[cid].restarts += 1
                new_pipe = self._build_pipeline(cid)
                self._pipelines[cid] = new_pipe
                new_pipe.start()
                restarted += 1
        return restarted

    def stop(self) -> None:
        """Para el monitor y todos los pipelines."""
        self._stop.set()
        for pipe in self._pipelines.values():
            pipe.stop()
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)

    # -- salud ------------------------------------------------------------

    def health_report(self) -> tuple[int, dict[str, Any]]:
        """Reporte de salud DE PRODUCTO: ``(status_code, body)``.

        ``200`` si TODAS las cámaras están sanas (vivas, ``frames_processed>0`` y
        no rancias); ``503`` + ``status='degraded'`` si alguna no procesa frames
        (distingue el caso "responde pero frames=0"). Sin cámaras => 503.
        """
        now = self._clock()
        cameras = {
            cid: h.snapshot(now, self._stale_after_s)
            for cid, h in self._health.items()
        }
        all_healthy = bool(cameras) and all(c["healthy"] for c in cameras.values())
        status = "ok" if all_healthy else "degraded"
        body = {"status": status, "cameras": cameras}
        return (200 if all_healthy else 503, body)

    def healthz(self) -> dict[str, Any]:
        """Cuerpo JSON de ``/healthz`` (sin el código HTTP)."""
        return self.health_report()[1]

    def serve_healthz(self, host: str = "0.0.0.0", port: int = 8081) -> Any:  # noqa: S104
        """Levanta un servidor HTTP mínimo de ``/healthz`` (stdlib, sin deps).

        Devuelve el ``HTTPServer`` (corriendo en un hilo daemon) para que el caller
        lo cierre al apagar. Sólo lo usa el entrypoint real; los tests llaman a
        ``health_report`` directamente.
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        supervisor = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (firma de BaseHTTPRequestHandler)
                if self.path.rstrip("/") not in ("/healthz", ""):
                    self.send_error(404)
                    return
                code, body = supervisor.health_report()
                payload = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: Any) -> None:
                pass  # silencioso: no contaminamos stdout del servicio

        server = HTTPServer((host, port), _Handler)
        threading.Thread(
            target=server.serve_forever, name="healthz", daemon=True
        ).start()
        return server


def build_dummy_supervisor(
    specs: list[CameraSpec],
    store: Store,
    *,
    script_factory: Callable[[str], Any] | None = None,
    **kwargs: Any,
) -> Supervisor:
    """Supervisor con ``DummyDetector`` + fuente fake (smoke x86 sin hardware).

    Cada cámara recibe su propio ``DummyDetector`` (guion por defecto: una persona
    cruzando) y una fuente que entrega frames ``None``-placeholder en bucle (el
    Dummy ignora el contenido del frame). Útil para un smoke local del supervisor.
    """
    from .dummy import DummyDetector  # noqa: PLC0415

    def detector_factory(camera_id: str) -> _Detector:
        script = script_factory(camera_id) if script_factory else None
        return DummyDetector(script=script, loop=True)

    def source_factory(_camera_id: str) -> FrameSource:
        return _LoopingFrameSource()

    return Supervisor(specs, store, detector_factory, source_factory, **kwargs)


@dataclass
class _LoopingFrameSource:
    """Fuente fake: entrega un frame placeholder en bucle (el Dummy lo ignora)."""

    _frame: object = field(default_factory=object)

    def read(self) -> Any | None:
        return self._frame

    def close(self) -> None:
        return None


def _load_specs(path: str) -> list[CameraSpec]:
    """Carga ``CameraSpec`` desde un JSON (config local; PR08 la formaliza).

    Formato: lista de objetos con ``site_id``/``device_id``/``camera_id``/``line``
    (``{a:[x,y], b:[x,y]}``)/``positive_side`` y campos opcionales. Mientras
    ``config.py`` (hot-reload por ``config_version``) no esté en esta base, el
    entrypoint lee la config de cámaras de este fichero.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    specs: list[CameraSpec] = []
    for c in raw["cameras"]:
        line = c["line"]
        specs.append(
            CameraSpec(
                site_id=c["site_id"],
                device_id=c["device_id"],
                camera_id=c["camera_id"],
                line_a=(float(line["a"][0]), float(line["a"][1])),
                line_b=(float(line["b"][0]), float(line["b"][1])),
                positive_side=int(c["positive_side"]),
                positive_label=c.get("positive_label"),
                negative_label=c.get("negative_label"),
                line_version=int(c.get("line_version", 1)),
                min_frames=int(c.get("min_frames", 2)),
                rtsp_url=c.get("rtsp_url"),
            )
        )
    return specs


def main(argv: list[str] | None = None) -> int:
    """Entrypoint ``cam-counter-edge``: supervisor real (Hailo + RTSP) + ``/healthz``.

    Construye UN VDevice Hailo compartido (import perezoso), un ``Detector`` por
    cámara que lo reusa, y una fuente RTSP por cámara (cv2, import perezoso).
    Bloquea sirviendo ``/healthz`` hasta recibir señal. En x86/CI no se invoca:
    los tests ejercitan ``Supervisor``/``CameraPipeline`` con Dummy + fakes.
    """
    import argparse  # noqa: PLC0415
    import signal  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="cam-counter-edge")
    parser.add_argument("--config", required=True, help="JSON de cámaras")
    parser.add_argument("--db", default="cam_counter.db", help="ruta SQLite (WAL)")
    parser.add_argument("--healthz-port", type=int, default=8081)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    specs = _load_specs(args.config)
    store = Store(args.db)

    # VDevice Hailo COMPARTIDO (import perezoso: sólo existe en el Pi).
    from hailo_platform import VDevice  # type: ignore  # noqa: PLC0415

    from .detector import Detector  # noqa: PLC0415

    vdevice = VDevice()
    specs_by_id = {s.camera_id: s for s in specs}

    def detector_factory(_camera_id: str) -> _Detector:
        return Detector(vdevice=vdevice)

    def source_factory(camera_id: str) -> FrameSource:
        return _RtspSource(specs_by_id[camera_id].rtsp_url or "")

    supervisor = Supervisor(specs, store, detector_factory, source_factory)
    supervisor.start()
    server = supervisor.serve_healthz(port=args.healthz_port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        supervisor.stop()
        server.shutdown()
        store.close()
    return 0


@dataclass
class _RtspSource:
    """Fuente RTSP real (cv2, import perezoso; sólo en el Pi)."""

    url: str
    _cap: Any = None

    def read(self) -> Any | None:
        import cv2  # noqa: PLC0415

        if self._cap is None:
            self._cap = cv2.VideoCapture(self.url)
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
