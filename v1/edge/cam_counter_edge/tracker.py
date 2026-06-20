"""Etapa ``track`` del pipeline de borde: asociación de detecciones entre frames.

Recibe las ``Detection`` de cada frame (cajas NORMALIZADAS 0..1, orden del sistema
``[xmin, ymin, xmax, ymax]``; ver CLAUDE.md §4) y mantiene identidades estables
(``track_id``) a lo largo del tiempo. Es **numpy puro + stdlib**: sin Hailo, sin cámara,
sin OpenCV, sin red ni disco, y **determinista** (misma entrada -> misma salida e ids), de
modo que toda la lógica corre en CI x86 sin hardware.

Por qué la NO-reutilización de ids es un requisito DURO: aguas abajo, el contador de
cruces (PR posterior) deriva un ``event_id`` DETERMINISTA a partir, entre otras cosas, del
``track_id``. Si un id retirado se reasignara, dos cruces distintos podrían colisionar en el
mismo ``event_id`` y la sincronización idempotente a la nube descartaría un evento real como
duplicado. Por eso el asignador de ids es MONÓTONO y nunca reutiliza un id retirado dentro
de la misma instancia (la "sesión de la cámara").
"""

from __future__ import annotations

import abc
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .types import Detection

# Defaults sensatos del asociador (documentados en CentroidIoUTracker.__init__).
DEFAULT_IOU_THRESHOLD = 0.3
DEFAULT_MAX_AGE = 30
DEFAULT_MAX_HISTORY = 30


@dataclass
class Track:
    """Pista activa de una persona a través de frames (estado de runtime del tracker).

    Distinto de ``types.Track`` (snapshot mínimo orientado al contrato CrossingEvent, con
    ``track_id`` como string): aquí el ``track_id`` es el **entero monótono** del asignador
    interno y se añade el estado de asociación (edad, hits, frames sin match, historial).

    Attributes:
        track_id: id entero ESTABLE y único dentro de esta instancia de tracker; lo emite
            un contador monótono que NUNCA reutiliza un id retirado.
        bbox_norm: última caja ``[xmin, ymin, xmax, ymax]`` observada, normalizada 0..1.
        centroid: último centroide ``(cx, cy)`` normalizado 0..1.
        score: última confianza observada (0..1).
        age: nº de frames transcurridos desde la creación del track.
        hits: nº de actualizaciones con match (señal de madurez del track).
        time_since_update: nº de frames consecutivos sin match (0 si hubo match este frame).
        history: historial corto y ACOTADO de centroides (deque con ``maxlen``), más reciente
            al final.
        last_update_ts: timestamp (epoch s o ms, float) de la última actualización con match.
    """

    track_id: int
    bbox_norm: list[float]
    centroid: tuple[float, float]
    score: float = 0.0
    age: int = 0
    hits: int = 1
    time_since_update: int = 0
    history: deque[tuple[float, float]] = field(default_factory=deque)
    last_update_ts: float | None = None


def _iou_matrix(track_boxes: np.ndarray, det_boxes: np.ndarray) -> np.ndarray:
    """Matriz IoU ``(T, D)`` entre cajas de tracks y de detecciones (orden del sistema).

    Cajas en ``[xmin, ymin, xmax, ymax]``. Devuelve 0 donde la unión es nula. Vectorizado
    con broadcasting: sin bucles Python sobre los pares.
    """
    t = track_boxes.shape[0]
    d = det_boxes.shape[0]
    if t == 0 or d == 0:
        return np.zeros((t, d), dtype=float)

    # (T,1) contra (1,D) -> (T,D) por broadcasting.
    ax1 = track_boxes[:, 0][:, None]
    ay1 = track_boxes[:, 1][:, None]
    ax2 = track_boxes[:, 2][:, None]
    ay2 = track_boxes[:, 3][:, None]
    bx1 = det_boxes[:, 0][None, :]
    by1 = det_boxes[:, 1][None, :]
    bx2 = det_boxes[:, 2][None, :]
    by2 = det_boxes[:, 3][None, :]

    inter_w = np.clip(np.minimum(ax2, bx2) - np.maximum(ax1, bx1), 0.0, None)
    inter_h = np.clip(np.minimum(ay2, by2) - np.maximum(ay1, by1), 0.0, None)
    inter = inter_w * inter_h

    area_a = np.clip(ax2 - ax1, 0.0, None) * np.clip(ay2 - ay1, 0.0, None)
    area_b = np.clip(bx2 - bx1, 0.0, None) * np.clip(by2 - by1, 0.0, None)
    union = area_a + area_b - inter
    return np.where(union > 0.0, inter / np.where(union > 0.0, union, 1.0), 0.0)


class Tracker(abc.ABC):
    """Interfaz mínima de un tracker por cámara.

    Una instancia por cámara: cada cámara tiene su propio asignador de ids y NO comparte
    estado con otras instancias (la unicidad global se garantiza aguas abajo combinando
    ``device_id``/``camera_id``).
    """

    @abc.abstractmethod
    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Procesa las detecciones de un frame y devuelve los tracks activos resultantes."""
        raise NotImplementedError


class CentroidIoUTracker(Tracker):
    """Tracker centroide + IoU en numpy puro, determinista, con ids no reutilizables.

    Asocia las detecciones de cada frame con los tracks existentes priorizando **IoU** (con
    gating por ``iou_threshold``) y desempatando por **distancia de centroides** (menor
    distancia gana). La asignación es un greedy determinista: se consideran todos los pares
    (track, detección) con IoU >= umbral, ordenados por IoU descendente, luego por distancia
    de centroides ascendente, y como último criterio por ``(track_id, det_index)`` para que
    el orden de resolución sea TOTALMENTE determinista.
    """

    def __init__(
        self,
        iou_threshold: float = DEFAULT_IOU_THRESHOLD,
        max_age: int = DEFAULT_MAX_AGE,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        """Configura el tracker.

        Args:
            iou_threshold: IoU mínimo para considerar un match (gating). Default 0.3.
            max_age: nº de frames consecutivos sin match tras el cual un track se RETIRA.
                Al alcanzar ``time_since_update >= max_age`` el track desaparece de los
                activos. Default 30.
            max_history: longitud máxima del historial de centroides por track. Default 30.
        """
        self.iou_threshold = float(iou_threshold)
        self.max_age = int(max_age)
        self.max_history = int(max_history)
        # Contador MONÓTONO: sólo incrementa; un id retirado nunca se reasigna en esta
        # instancia. Empieza en 0 y avanza por cada track creado.
        self._next_id = 0
        self._tracks: list[Track] = []

    def _new_id(self) -> int:
        """Devuelve un id fresco del asignador monótono (jamás reutiliza ids retirados)."""
        track_id = self._next_id
        self._next_id += 1
        return track_id

    def _match(
        self,
        det_boxes: np.ndarray,
        det_centroids: list[tuple[float, float]],
    ) -> dict[int, int]:
        """Empareja tracks con detecciones (greedy determinista). Devuelve ``{t_idx: d_idx}``."""
        if not self._tracks or det_boxes.shape[0] == 0:
            return {}

        track_boxes = np.array([t.bbox_norm for t in self._tracks], dtype=float)
        iou = _iou_matrix(track_boxes, det_boxes)

        # Candidatos por encima del umbral, ordenables de forma estable y determinista.
        candidates: list[tuple[float, float, int, int, int]] = []
        for t_idx, track in enumerate(self._tracks):
            tcx, tcy = track.centroid
            for d_idx in range(det_boxes.shape[0]):
                iou_td = float(iou[t_idx, d_idx])
                if iou_td < self.iou_threshold:
                    continue
                dcx, dcy = det_centroids[d_idx]
                dist = (tcx - dcx) ** 2 + (tcy - dcy) ** 2  # cuadrado: mismo orden, más barato
                # Clave: IoU desc (-iou), distancia asc, luego track_id y d_idx para
                # romper cualquier empate restante de forma totalmente determinista.
                candidates.append((-iou_td, dist, track.track_id, t_idx, d_idx))

        candidates.sort()

        matched_t: set[int] = set()
        matched_d: set[int] = set()
        pairs: dict[int, int] = {}
        for _neg_iou, _dist, _tid, t_idx, d_idx in candidates:
            if t_idx in matched_t or d_idx in matched_d:
                continue
            matched_t.add(t_idx)
            matched_d.add(d_idx)
            pairs[t_idx] = d_idx
        return pairs

    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Actualiza el estado con las detecciones del frame y devuelve los tracks activos.

        Args:
            detections: detecciones del frame actual (cajas normalizadas + score).
            ts: timestamp del frame (epoch s o ms, float). Se guarda en los tracks con match.

        Returns:
            Lista (copia) de ``Track`` activos tras la actualización, en orden estable
            (tracks existentes en su orden + tracks nuevos al final).
        """
        det_list = list(detections)
        if det_list:
            det_boxes = np.array([d.bbox_norm for d in det_list], dtype=float).reshape(-1, 4)
        else:
            det_boxes = np.zeros((0, 4), dtype=float)
        det_centroids = [d.center for d in det_list]

        pairs = self._match(det_boxes, det_centroids)
        matched_d = set(pairs.values())

        # 1) Tracks existentes: actualizar los emparejados, envejecer los no emparejados.
        survivors: list[Track] = []
        for t_idx, track in enumerate(self._tracks):
            track.age += 1
            if t_idx in pairs:
                det = det_list[pairs[t_idx]]
                cx, cy = det.center
                track.bbox_norm = list(det.bbox_norm)
                track.centroid = (cx, cy)
                track.score = float(det.confidence)
                track.hits += 1
                track.time_since_update = 0
                track.history.append((cx, cy))
                track.last_update_ts = float(ts)
            else:
                track.time_since_update += 1
            # Retiro: tras max_age frames consecutivos sin match, el track se elimina.
            if track.time_since_update < self.max_age:
                survivors.append(track)

        # 2) Detecciones sin match: nuevos tracks con id fresco del asignador monótono.
        for d_idx, det in enumerate(det_list):
            if d_idx in matched_d:
                continue
            cx, cy = det.center
            history: deque[tuple[float, float]] = deque(maxlen=self.max_history)
            history.append((cx, cy))
            survivors.append(
                Track(
                    track_id=self._new_id(),
                    bbox_norm=list(det.bbox_norm),
                    centroid=(cx, cy),
                    score=float(det.confidence),
                    age=0,
                    hits=1,
                    time_since_update=0,
                    history=history,
                    last_update_ts=float(ts),
                )
            )

        self._tracks = survivors
        return list(self._tracks)

    @property
    def tracks(self) -> list[Track]:
        """Vista (copia) de los tracks actualmente activos."""
        return list(self._tracks)
