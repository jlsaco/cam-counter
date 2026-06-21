"""``DummyDetector``: detector determinista sin hardware (para CI x86).

Implementa la MISMA interfaz que ``Detector`` (``detect(frame_bgr) -> list[Detection]``)
pero reproduce una **secuencia programada (scripted) determinista** de detecciones,
independiente del contenido del frame. Permite ejercitar TODA la lógica de conteo
posterior (tracking, cruce de línea, sink) en x86 sin Hailo, sin cámara y sin red.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .types import PERSON_CLASS_ID, Detection


def default_crossing_script() -> list[list[Detection]]:
    """Secuencia por defecto: una persona cruzando de izquierda a derecha.

    Seis frames con una sola persona cuyo centroide avanza por x = 0.10 -> 0.90,
    cruzando la mitad (x = 0.5). Útil como caso canónico para el conteo de cruce
    de línea de PR06+. Geometría en floats normalizados 0..1.

    NOTA: los saltos entre frames consecutivos son grandes (sin solape de cajas),
    así que un tracker por IoU (``CentroidIoUTracker``) NO mantiene un mismo
    ``track_id`` a lo largo de la secuencia (cada frame crea un track nuevo). Es
    deliberado para los tests unitarios del ``LineCounter`` (que reciben los tracks
    ya construidos). Para una fuente END-TO-END que SÍ produzca cruces a través del
    tracker (fuente falsa / supervisor / E2E), usa ``smooth_crossing_script``.
    """
    centers_x = [0.10, 0.30, 0.45, 0.55, 0.70, 0.90]
    script: list[list[Detection]] = []
    for cx in centers_x:
        det = Detection(
            bbox_norm=[cx - 0.05, 0.40, cx + 0.05, 0.60],
            class_id=PERSON_CLASS_ID,
            confidence=0.90,
        )
        script.append([det])
    return script


def smooth_crossing_script(
    *,
    start: float = 0.20,
    end: float = 0.80,
    steps: int = 16,
    width: float = 0.16,
    height: float = 0.40,
    cy: float = 0.5,
) -> list[list[Detection]]:
    """Secuencia FINA de una persona cruzando L->R que un tracker IoU SÍ sigue.

    A diferencia de ``default_crossing_script``, los pasos son lo bastante pequeños
    para que las cajas consecutivas se SOLAPEN (IoU alto), de modo que el
    ``CentroidIoUTracker`` mantiene un ``track_id`` ESTABLE a lo largo del cruce y
    el ``LineCounter`` (con histéresis ``min_frames>=2``) confirma EXACTAMENTE un
    cruce por pasada. Con ``loop=True`` el salto del final (``end``) al inicio
    (``start``) NO solapa, así que el tracker arranca un track nuevo en cada vuelta
    (un cruce limpio por ciclo): incrementos deterministas para la fuente falsa y
    los E2E. Geometría en floats normalizados 0..1, origen arriba-izquierda.
    """
    if steps < 2:
        raise ValueError(f"steps debe ser >= 2, no {steps!r}")
    half_w = width / 2.0
    half_h = height / 2.0
    script: list[list[Detection]] = []
    for i in range(steps):
        cx = start + (end - start) * (i / (steps - 1))
        det = Detection(
            bbox_norm=[
                round(cx - half_w, 4),
                round(cy - half_h, 4),
                round(cx + half_w, 4),
                round(cy + half_h, 4),
            ],
            class_id=PERSON_CLASS_ID,
            confidence=0.90,
        )
        script.append([det])
    return script


class DummyDetector:
    """Detector de detecciones programadas, determinista y sin hardware.

    Cada llamada a ``detect`` consume el siguiente frame de la secuencia y
    devuelve su lista de ``Detection`` (siempre una copia superficial, para que
    el llamador no mute el guion). Por defecto, al agotar la secuencia devuelve
    listas vacías; con ``loop=True`` reinicia desde el principio.
    """

    def __init__(
        self,
        script: Iterable[Sequence[Detection]] | None = None,
        loop: bool = False,
    ) -> None:
        if script is None:
            script = default_crossing_script()
        # Materializa y copia cada frame para aislar el estado interno.
        self._script: list[list[Detection]] = [list(frame) for frame in script]
        self._loop = loop
        self._i = 0

    def detect(self, frame_bgr: Any = None) -> list[Detection]:
        """Devuelve el siguiente conjunto programado de ``Detection``.

        El argumento ``frame_bgr`` se acepta por compatibilidad de interfaz con
        ``Detector`` pero se IGNORA: la secuencia es independiente del frame.
        """
        if self._i >= len(self._script):
            if self._loop and self._script:
                self._i = 0
            else:
                return []
        frame = self._script[self._i]
        self._i += 1
        return list(frame)

    def reset(self) -> None:
        """Reinicia la secuencia al primer frame."""
        self._i = 0

    @property
    def remaining(self) -> int:
        """Número de frames programados aún no consumidos (sin ``loop``)."""
        return max(0, len(self._script) - self._i)
