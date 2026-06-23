"""Fuente de vídeo REAL para la API: decodifica el RTSP de la cámara con ffmpeg.

En el Pi, la API (que corre en el venv SIN cv2/Hailo) necesita mostrar el vídeo
EN VIVO de la cámara mientras el usuario configura la línea-umbral. El pipeline de
conteo (detección Hailo) lo hace el proceso ``cam-counter-edge``; esta fuente NO
detecta nada: sólo abre una 2ª conexión RTSP de SÓLO-LECTURA con ``ffmpeg`` y
publica el último frame JPEG en ``CameraState.frame`` para el stream MJPEG.

Diseño (mismo patrón que ``FakeSource``):
- Un hilo por cámara lanza ``ffmpeg`` (transporte TCP, salida MJPEG a ``pipe:1``)
  y trocea el stream en JPEGs (delimitados por los marcadores SOI ``FFD8`` / EOI
  ``FFD9``), actualizando ``state.frame`` con el más reciente.
- Si ``ffmpeg`` muere (cámara reconectando, red), se reintenta con backoff acotado
  SIN tumbar la API.
- No usa cv2 (no está en el venv): sólo el binario ``ffmpeg`` del sistema. La
  línea-umbral NO se dibuja aquí — la pinta la UI como overlay SVG sobre el frame.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

import mjpeg
from fakes import CameraState
from settings import Settings

__all__ = ["RtspSource", "resolve_rtsp_url", "any_rtsp_configured"]

_log = logging.getLogger(__name__)

# Marcadores JPEG para trocear el stream MJPEG de ffmpeg.
_SOI = b"\xff\xd8"  # Start Of Image
_EOI = b"\xff\xd9"  # End Of Image

# Resolución de salida (16:9, coherente con mjpeg.FRAME_W/H y el aspect-video de la UI).
_OUT_W = mjpeg.FRAME_W
_OUT_H = mjpeg.FRAME_H
_OUT_FPS = 6  # preview ligero: suficiente para colocar la línea, poco coste de decode.


def resolve_rtsp_url(camera_id: str) -> str:
    """URL RTSP de una cámara: ``CAMCOUNTER_RTSP_<CAMID>`` o ``CAMCOUNTER_RTSP_URL``.

    Misma convención que el edge (``cam_counter_edge.app._rtsp_source``): permite
    una URL por cámara (multi-cámara) y una global de respaldo. El secreto (clave
    de la cámara) viaja por entorno; nunca se versiona.
    """
    per_cam = os.environ.get(f"CAMCOUNTER_RTSP_{camera_id.replace('-', '_').upper()}")
    return per_cam or os.environ.get("CAMCOUNTER_RTSP_URL", "")


def any_rtsp_configured(settings: Settings) -> bool:
    """``True`` si alguna cámara tiene URL RTSP (decide usar ``RtspSource``)."""
    return any(resolve_rtsp_url(cam) for cam in settings.camera_ids)


def _ffmpeg_cmd(url: str) -> list[str]:
    """Comando ffmpeg: RTSP/TCP -> MJPEG a stdout, escalado y a fps bajo."""
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", url,
        "-an",
        "-f", "mjpeg",
        "-q:v", "6",
        "-r", str(_OUT_FPS),
        "-vf", f"scale={_OUT_W}:{_OUT_H}",
        "pipe:1",
    ]


class RtspSource:
    """Fuente RTSP real (un hilo + un ffmpeg por cámara). Duck-types ``Source``."""

    def __init__(self, settings: Settings, states: dict[str, CameraState]) -> None:
        self._settings = settings
        self._states = states
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    def start(self) -> None:
        for cam, st in self._states.items():
            st.frame = mjpeg.placeholder_frame(cam, "conectando")
            st.online = False
            url = resolve_rtsp_url(cam)
            if not url:
                # Sin URL para esta cámara: queda en 'sin señal' (no lanza hilo).
                st.frame = mjpeg.placeholder_frame(cam)
                continue
            thread = threading.Thread(
                target=self._run_camera, args=(cam, url), name=f"rtsp-{cam}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for proc in list(self._procs.values()):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — best-effort
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    # -- bucle por cámara -------------------------------------------------

    def _run_camera(self, camera_id: str, url: str) -> None:
        """Mantiene ffmpeg vivo y publica el último JPEG; reintenta con backoff."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(  # noqa: S603 — comando fijo, URL por entorno
                    _ffmpeg_cmd(url),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except FileNotFoundError:
                _log.error("rtsp %s: 'ffmpeg' no está instalado; no hay vídeo en vivo", camera_id)
                return
            self._procs[camera_id] = proc
            got_frame = self._pump(camera_id, proc)
            # ffmpeg terminó: marca offline y reintenta.
            self._states[camera_id].online = False
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            if self._stop.is_set():
                return
            backoff = 1.0 if got_frame else min(backoff * 2.0, 10.0)
            self._states[camera_id].frame = mjpeg.placeholder_frame(camera_id, "reconectando")
            self._stop.wait(backoff)

    def _pump(self, camera_id: str, proc: subprocess.Popen[bytes]) -> bool:
        """Lee el MJPEG de ffmpeg y publica cada JPEG completo. Devuelve si hubo frame."""
        state = self._states[camera_id]
        stdout = proc.stdout
        if stdout is None:
            return False
        buf = b""
        got = False
        while not self._stop.is_set():
            chunk = stdout.read(65536)
            if not chunk:
                break  # ffmpeg cerró stdout (murió / EOF)
            buf += chunk
            # Extrae todos los JPEG completos presentes en el buffer.
            while True:
                soi = buf.find(_SOI)
                if soi < 0:
                    if len(buf) > 4_000_000:  # basura sin SOI: no crecer sin límite
                        buf = b""
                    break
                eoi = buf.find(_EOI, soi + 2)
                if eoi < 0:
                    buf = buf[soi:]  # JPEG parcial: conserva desde el SOI
                    break
                jpeg = buf[soi : eoi + 2]
                buf = buf[eoi + 2 :]
                state.frame = jpeg
                state.frames_processed += 1
                state.online = True
                got = True
        return got
