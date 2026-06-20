"""``DummyDetector``: detector sin hardware con secuencia determinista programada.

Reproduce una secuencia **scripted** de detecciones, frame a frame, **independiente del
contenido del frame**. Tiene la MISMA interfaz que ``Detector`` (``detect(frame_bgr) ->
list[Detection]``), de modo que toda la lógica de conteo posterior (tracker, línea,
histéresis, SQLite) se pueda testear en x86 sin Hailo, sin cámara y sin red.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .types import DEFAULT_CONF, PERSON_CLASS_ID, Detection


class DummyDetector:
    """Detector falso que emite una secuencia programada y determinista de detecciones.

    Cada llamada a ``detect()`` devuelve el siguiente "frame" de la secuencia (una lista
    de ``Detection``), ignorando por completo el ``frame_bgr`` recibido. La secuencia es
    determinista: dos ``DummyDetector`` con el mismo ``frames`` producen exactamente la
    misma salida en el mismo orden.
    """

    def __init__(
        self,
        frames: Iterable[Sequence[Detection]],
        *,
        loop: bool = False,
    ) -> None:
        """Programa la secuencia de detecciones.

        Args:
            frames: secuencia de frames; cada frame es una secuencia de ``Detection``
                (puede ser vacía = ningún cruce en ese frame).
            loop: si True, al agotar la secuencia vuelve a empezar (cíclico). Si False
                (por defecto), tras agotarla ``detect()`` devuelve ``[]`` indefinidamente.
        """
        self._frames: list[list[Detection]] = [list(frame) for frame in frames]
        self._loop = loop
        self._index = 0

    @classmethod
    def from_bboxes(
        cls,
        frames_bboxes: Iterable[Iterable[Sequence[float]]],
        *,
        conf: float = DEFAULT_CONF,
        class_id: int = PERSON_CLASS_ID,
        loop: bool = False,
    ) -> DummyDetector:
        """Construye desde bboxes normalizados ``[xmin, ymin, xmax, ymax]`` (orden sistema).

        Conveniencia para tests: cada frame es una lista de bboxes ya en orden del
        sistema y normalizados 0..1 (NO en orden Hailo). ``conf`` se usa como confianza
        de cada detección sintética.
        """
        frames: list[list[Detection]] = []
        for frame in frames_bboxes:
            frames.append(
                [
                    Detection(
                        bbox_norm=[float(b[0]), float(b[1]), float(b[2]), float(b[3])],
                        class_id=class_id,
                        confidence=conf,
                    )
                    for b in frame
                ]
            )
        return cls(frames, loop=loop)

    def detect(self, frame_bgr=None) -> list[Detection]:
        """Devuelve el siguiente frame programado (copia), ignorando ``frame_bgr``."""
        if not self._frames:
            return []
        if self._index >= len(self._frames):
            if not self._loop:
                return []
            self._index = 0
        frame = self._frames[self._index]
        self._index += 1
        # Copia superficial: el llamante no puede mutar la secuencia programada.
        return list(frame)

    def reset(self) -> None:
        """Reinicia la secuencia al primer frame."""
        self._index = 0

    def __len__(self) -> int:
        """Número de frames programados."""
        return len(self._frames)
