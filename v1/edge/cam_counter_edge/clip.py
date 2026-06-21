"""Etapa ``clip`` del pipeline de borde: buffer circular + grabador asíncrono.

Alrededor de cada CRUCE de línea se captura un clip (pre-roll + post-roll) que
más tarde (PR10) se sube a S3. Este módulo provee:

- ``ClipBuffer`` / el buffer circular interno del ``ClipRecorder``: un
  ``collections.deque(maxlen=...)`` por cámara de **JPEGs YA codificados**
  (pre-roll), dimensionado por una duración objetivo y FPS.
- ``ClipRecorder``: grabador ASÍNCRONO (hilo de trabajo) que, al recibir la orden
  no bloqueante ``request_clip(event)``, toma el pre-roll del buffer, sigue
  acumulando post-roll vía ``add_frame`` hasta completar la ventana, ensambla un
  **MP4** (``imageio-ffmpeg`` o ``cv2.VideoWriter``) con **fallback a GIF**
  (Pillow) si el encoder MP4 no está disponible, escribe a un fichero temporal y
  hace **rename atómico** (``os.replace``) a la ruta final en ``shared/``, y por
  último **encola una fila ``pending``** en ``clip_uploads`` con la ``s3_key_planned``
  construida desde la plantilla de media SÓLO con slugs validados.

El CAMINO DE CONTEO NUNCA BLOQUEA: ``add_frame`` y ``request_clip`` sólo mutan
estructuras EN MEMORIA bajo un lock de microsegundos (deque/listas) y entregan el
trabajo a una cola; toda la IO de fichero (encode + escritura) y la escritura en
SQLite ocurren EXCLUSIVAMENTE en el hilo de trabajo, fuera del camino crítico.

Subida idempotente y a prueba de reintentos (la IMPLEMENTA PR10, aquí se
DOCUMENTA el contrato): el worker de subida DEBE evitar sobrescribir un objeto
parcial cuando reintenta el MISMO ``event_id``. Dos estrategias válidas:

  1. ``PutObject`` con cabecera ``If-None-Match: *`` (la put falla si la clave ya
     existe), o
  2. subir a una clave TEMPORAL y luego ``CopyObject`` a la clave final.

Como ``event_id`` es DETERMINISTA (sha1 de la tupla de identidad — sha1 aquí NO es
criptográfico, sólo deduplica), la ``s3_key_planned`` es estable entre reintentos
y la subida idempotente nunca duplica ni corrompe el objeto final.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import numpy as np

from .identifiers import (
    media_clip_key,
    validate_camera_id,
    validate_device_id,
    validate_site_id,
)
from .types import CrossingEvent

__all__ = [
    "ClipEncodeError",
    "ClipRecorder",
    "ClipResult",
    "decode_jpeg",
    "encode_jpeg",
    "write_clip",
]

_log = logging.getLogger(__name__)


class ClipEncodeError(RuntimeError):
    """No hay ningún encoder de vídeo disponible (ni MP4 ni GIF)."""


# -- helpers de imagen (Pillow, importado de forma PEREZOSA) --------------


def _as_uint8_rgb(frame: object) -> np.ndarray:
    """Normaliza un frame a ``np.uint8`` HxWx3 RGB contiguo."""
    arr = np.asarray(frame)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    return np.ascontiguousarray(arr)


def encode_jpeg(frame: object, quality: int = 80) -> bytes:
    """Codifica un frame (np.ndarray RGB) a bytes JPEG (para el buffer pre-roll)."""
    from PIL import Image  # noqa: PLC0415  (perezoso; encoder de imagen)

    img = Image.fromarray(_as_uint8_rgb(frame))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=int(quality))
    return buf.getvalue()


def decode_jpeg(data: bytes) -> np.ndarray:
    """Decodifica bytes JPEG a ``np.ndarray`` RGB uint8."""
    from PIL import Image  # noqa: PLC0415  (perezoso; decoder de imagen)

    img = Image.open(BytesIO(data))
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    return np.asarray(rgb)


# -- encoders de vídeo (MP4 preferido; fallback GIF) ----------------------


def _mp4_via_imageio(frames_rgb: list[np.ndarray], tmp: Path, fps: float) -> None:
    """Escribe MP4 con ``imageio`` + ``imageio-ffmpeg`` (si están instalados)."""
    import imageio.v2 as imageio  # noqa: PLC0415  (perezoso; encoder MP4 opcional)

    writer = imageio.get_writer(
        str(tmp),
        format="FFMPEG",
        mode="I",
        fps=float(fps),
        codec="libx264",
        macro_block_size=1,  # no forzar resize a múltiplos de 16
    )
    try:
        for frame in frames_rgb:
            writer.append_data(_as_uint8_rgb(frame))
    finally:
        writer.close()


def _mp4_via_cv2(frames_rgb: list[np.ndarray], tmp: Path, fps: float) -> None:
    """Escribe MP4 con ``cv2.VideoWriter`` (códec ``mp4v``; sólo si cv2 está)."""
    import cv2  # noqa: PLC0415  (perezoso; sólo en el Pi / si cv2 está instalado)

    height, width = _as_uint8_rgb(frames_rgb[0]).shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp), fourcc, float(fps), (width, height))
    if not writer.isOpened():
        writer.release()
        raise ClipEncodeError("cv2.VideoWriter no pudo abrir el contenedor MP4")
    try:
        for frame in frames_rgb:
            bgr = cv2.cvtColor(_as_uint8_rgb(frame), cv2.COLOR_RGB2BGR)
            writer.write(bgr)
    finally:
        writer.release()


def _encode_mp4(frames_rgb: list[np.ndarray], out_dir: Path, event_id: str, fps: float) -> Path:
    """Intenta MP4 (imageio, luego cv2); devuelve el fichero temporal o lanza."""
    tmp = out_dir / f"{event_id}.part.mp4"
    for writer in (_mp4_via_imageio, _mp4_via_cv2):
        with suppress(FileNotFoundError):
            tmp.unlink()
        try:
            writer(frames_rgb, tmp, fps)
        except Exception as exc:  # probamos el siguiente encoder MP4
            _log.debug("encoder MP4 %s no disponible: %r", writer.__name__, exc)
            continue
        if tmp.exists() and tmp.stat().st_size > 0:
            return tmp
    with suppress(FileNotFoundError):
        tmp.unlink()
    raise ClipEncodeError("ningún encoder MP4 disponible")


def _encode_gif(frames_rgb: list[np.ndarray], out_dir: Path, event_id: str, fps: float) -> Path:
    """Fallback GIF con Pillow; devuelve el fichero temporal."""
    from PIL import Image  # noqa: PLC0415  (perezoso; fallback GIF)

    tmp = out_dir / f"{event_id}.part.gif"
    images = [Image.fromarray(_as_uint8_rgb(frame)) for frame in frames_rgb]
    duration_ms = max(1, round(1000.0 / float(fps)))
    images[0].save(
        str(tmp),
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
    )
    return tmp


def write_clip(
    frames_rgb: list[np.ndarray], out_dir: str | Path, event_id: str, fps: float
) -> tuple[str, Path]:
    """Ensambla los frames en un clip y lo escribe de forma ATÓMICA.

    Prueba MP4 y, si no hay encoder MP4, cae a GIF. Escribe a un fichero temporal
    ``{event_id}.part.{ext}`` y hace ``os.replace`` (rename atómico) a
    ``{event_id}.{ext}`` dentro de ``out_dir``. Devuelve ``(ext, ruta_final)``.

    Lanza ``ClipEncodeError`` si no hay frames o ningún encoder funciona.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not frames_rgb:
        raise ClipEncodeError("no hay frames que codificar")

    last_err: Exception | None = None
    for ext, encoder in (("mp4", _encode_mp4), ("gif", _encode_gif)):
        tmp: Path | None = None
        try:
            tmp = encoder(frames_rgb, out_dir, event_id, fps)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise ClipEncodeError(f"el encoder {ext} no produjo salida")
            final = out_dir / f"{event_id}.{ext}"
            os.replace(tmp, final)  # rename ATÓMICO dentro del mismo dir
            return ext, final
        except Exception as exc:  # cae al siguiente formato (MP4 -> GIF)
            last_err = exc
            if tmp is not None:
                with suppress(FileNotFoundError):
                    tmp.unlink()
    raise ClipEncodeError(
        f"no hay encoder de vídeo disponible (último error: {last_err!r})"
    )


# -- grabador asíncrono ----------------------------------------------------


class _ClipUploadStore(Protocol):
    """Interfaz mínima del store que el ``ClipRecorder`` necesita (encolar subida)."""

    def enqueue_clip_upload(
        self,
        *,
        event_id: str,
        camera_id: str,
        local_path: str,
        s3_key_planned: str,
    ) -> int: ...


@dataclass
class ClipResult:
    """Resultado de ensamblar y encolar un clip (observabilidad / tests)."""

    event_id: str
    camera_id: str
    local_path: str
    ext: str
    frame_count: int
    s3_key_planned: str
    upload_row_id: int


@dataclass
class _ClipJob:
    """Trabajo de encode listo para el worker: evento + frames pre+post en orden."""

    event: CrossingEvent
    frames: list[tuple[int, bytes]]


@dataclass
class _Recording:
    """Grabación abierta acumulando post-roll para un cruce concreto."""

    event: CrossingEvent
    frames: list[tuple[int, bytes]]
    remaining: int


class ClipRecorder:
    """Buffer circular de JPEGs por cámara + grabador asíncrono de clips.

    El proceso de conteo alimenta frames con ``add_frame`` (no bloqueante) y, al
    confirmarse un cruce, pide un clip con ``request_clip(event)`` (no bloqueante:
    snapshot del pre-roll + apertura de la grabación). El post-roll se completa con
    los siguientes ``add_frame``; cuando la ventana se llena, el trabajo pasa a un
    hilo que ensambla el clip (MP4/GIF), lo escribe atómicamente y encola la fila
    ``pending`` en ``clip_uploads``.

    Args:
        store: capa de persistencia (expone ``enqueue_clip_upload``).
        out_dir: directorio destino de los clips (típicamente bajo ``shared/``).
        fps: frames por segundo del clip.
        pre_seconds: segundos de pre-roll (dimensiona el buffer circular).
        post_seconds: segundos de post-roll a capturar tras el cruce.
    """

    def __init__(
        self,
        store: _ClipUploadStore,
        *,
        out_dir: str | Path,
        fps: float = 10.0,
        pre_seconds: float = 2.0,
        post_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._out_dir = Path(out_dir)
        self._fps = float(fps)
        self._pre_frames = max(1, round(float(pre_seconds) * self._fps))
        self._post_frames = max(1, round(float(post_seconds) * self._fps))

        self._buffers: dict[str, deque[tuple[int, bytes]]] = {}
        self._open: dict[str, list[_Recording]] = {}
        self._lock = threading.Lock()  # guarda SÓLO estructuras en memoria

        self._queue: queue.Queue[_ClipJob | None] = queue.Queue()
        self._idle = threading.Condition()
        self._inflight = 0  # jobs encolados + en proceso (para flush())

        self._results: list[ClipResult] = []
        self._results_by_event: dict[str, ClipResult] = {}
        self._results_lock = threading.Lock()

        self._closed = False
        self._thread = threading.Thread(
            target=self._worker, name="clip-recorder", daemon=True
        )
        self._thread.start()

    # -- camino de conteo (NO bloquea: sólo memoria) ----------------------

    def add_frame(self, camera_id: str, jpeg: bytes, ts_ms: int) -> None:
        """Empuja un JPEG al buffer de la cámara (y a las grabaciones abiertas).

        O(1) amortizado y SIN IO: append a un ``deque`` (descarta el más viejo al
        superar ``maxlen``) y, si hay grabaciones abiertas para la cámara, append
        a su post-roll. Cuando una grabación completa su ventana, su trabajo se
        encola fuera del lock.
        """
        ts = int(ts_ms)
        ready: list[_Recording] = []
        with self._lock:
            buf = self._buffers.get(camera_id)
            if buf is None:
                buf = self._buffers[camera_id] = deque(maxlen=self._pre_frames)
            buf.append((ts, jpeg))
            recs = self._open.get(camera_id)
            if recs:
                still_open: list[_Recording] = []
                for rec in recs:
                    rec.frames.append((ts, jpeg))
                    rec.remaining -= 1
                    if rec.remaining <= 0:
                        ready.append(rec)
                    else:
                        still_open.append(rec)
                if still_open:
                    self._open[camera_id] = still_open
                else:
                    del self._open[camera_id]
        for rec in ready:
            self._enqueue_job(_ClipJob(event=rec.event, frames=rec.frames))

    def request_clip(self, event: CrossingEvent) -> None:
        """Ordena grabar un clip alrededor de un cruce (NO bloqueante).

        Valida los slugs del evento (fail-fast), toma un SNAPSHOT del pre-roll
        actual del buffer y abre una grabación que el post-roll completará. No
        hace IO ni red: el trabajo pesado lo hará el hilo de trabajo.
        """
        validate_site_id(event.site_id)
        validate_device_id(event.device_id)
        validate_camera_id(event.camera_id)
        with self._lock:
            buf = self._buffers.get(event.camera_id)
            pre = list(buf) if buf is not None else []
            rec = _Recording(event=event, frames=pre, remaining=self._post_frames)
            self._open.setdefault(event.camera_id, []).append(rec)

    # -- encolado / worker -------------------------------------------------

    def _enqueue_job(self, job: _ClipJob) -> None:
        with self._idle:
            self._inflight += 1
        self._queue.put_nowait(job)  # cola sin límite: put_nowait nunca bloquea

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return  # sentinela de cierre
                self._process_job(job)
            except Exception:  # el worker NUNCA muere por un clip fallido
                _log.exception("clip-recorder: fallo procesando un clip")
            finally:
                if job is not None:
                    with self._idle:
                        self._inflight -= 1
                        self._idle.notify_all()
                self._queue.task_done()

    def _process_job(self, job: _ClipJob) -> None:
        event = job.event
        frames_rgb = [decode_jpeg(jpeg) for _ts, jpeg in job.frames]
        ext, path = write_clip(frames_rgb, self._out_dir, event.event_id, self._fps)
        # La clave S3 se construye SÓLO con slugs validados (media_clip_key valida).
        s3_key = media_clip_key(
            event.site_id,
            event.device_id,
            event.camera_id,
            event.event_id,
            ext,
            event.ts_event_ms,
        )
        row_id = self._store.enqueue_clip_upload(
            event_id=event.event_id,
            camera_id=event.camera_id,
            local_path=str(path),
            s3_key_planned=s3_key,
        )
        result = ClipResult(
            event_id=event.event_id,
            camera_id=event.camera_id,
            local_path=str(path),
            ext=ext,
            frame_count=len(frames_rgb),
            s3_key_planned=s3_key,
            upload_row_id=row_id,
        )
        with self._results_lock:
            self._results.append(result)
            self._results_by_event[event.event_id] = result

    # -- sincronización / cierre (uso de tests y apagado limpio) ----------

    def flush(self, timeout: float = 10.0) -> None:
        """Bloquea hasta que todos los clips ENCOLADOS se hayan procesado.

        Sólo espera grabaciones ya completas (encoladas). Las grabaciones aún
        abiertas (post-roll incompleto) se finalizan en ``close()``.
        """
        with self._idle:
            deadline = time.monotonic() + float(timeout)
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ClipRecorder.flush agotó el timeout")
                self._idle.wait(remaining)

    def result_for(self, event_id: str) -> ClipResult | None:
        """Devuelve el ``ClipResult`` de un ``event_id`` (o ``None`` si no listo)."""
        with self._results_lock:
            return self._results_by_event.get(event_id)

    @property
    def results(self) -> list[ClipResult]:
        """Copia de los resultados producidos hasta el momento."""
        with self._results_lock:
            return list(self._results)

    def close(self, *, flush: bool = True, timeout: float = 10.0) -> None:
        """Finaliza grabaciones abiertas, drena la cola y para el hilo."""
        if self._closed:
            return
        self._closed = True
        # Finaliza (best-effort) las grabaciones abiertas para no perder clips.
        leftover: list[_Recording] = []
        with self._lock:
            for recs in self._open.values():
                leftover.extend(recs)
            self._open.clear()
        for rec in leftover:
            if rec.frames:
                self._enqueue_job(_ClipJob(event=rec.event, frames=rec.frames))
        if flush:
            self.flush(timeout=timeout)
        self._queue.put_nowait(None)  # sentinela
        self._thread.join(timeout=timeout)

    def __enter__(self) -> ClipRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
