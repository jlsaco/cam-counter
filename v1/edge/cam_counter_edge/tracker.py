"""Etapa ``track`` del pipeline de borde: tracker centroide+IoU.

Recibe las detecciones de un frame (``capture -> detect -> *track* -> count``) y
mantiene identidades estables (``track_id``) a lo largo del tiempo, asociando las
detecciones del frame actual con las pistas existentes.

Diseño y garantías (ver CLAUDE.md):

- **numpy puro + stdlib**: sin OpenCV, scipy, filterpy, Hailo, cámara, red ni
  disco. Determinista: misma secuencia de entradas -> misma secuencia de salidas
  e ids.
- **Geometría normalizada 0..1**: todas las cajas son ``[xmin, ymin, xmax, ymax]``
  en floats 0..1 relativos al frame de inferencia (origen arriba-izquierda).
  Nunca se manejan píxeles.
- **Asociación**: IoU con *gating* por ``iou_threshold`` (sólo se consideran pares
  con IoU suficiente) y desempate por menor distancia de centroides; matching
  *greedy* determinista (IoU descendente, luego distancia ascendente, luego
  índices) — no se usa el algoritmo húngaro a propósito, para mantenerlo simple y
  testeable.
- **Ids MONÓTONOS y NO REUTILIZABLES**: el contador de ``track_id`` sólo
  incrementa dentro de la vida del objeto (la "sesión de la cámara"). Un id
  retirado por ``max_age`` JAMÁS se reasigna; un objeto que reaparece recibe un id
  NUEVO. Esto protege el determinismo del ``event_id`` aguas abajo (PR07): si un
  id retirado se reutilizara, dos cruces distintos podrían colisionar en el mismo
  ``event_id`` y la sincronización idempotente a la nube descartaría un evento
  real como duplicado.

El tracker es **por cámara**: cada cámara usa su propia instancia, con su propio
asignador de ids. No hay estado de módulo mutable que acople instancias; dos
instancias pueden emitir el mismo valor numérico de id sin problema (la unicidad
global se garantiza aguas abajo combinando ``device_id``/``camera_id``).
"""

from __future__ import annotations

import abc
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .types import Detection

__all__ = ["CentroidIoUTracker", "Track", "Tracker"]


def _centroid(bbox: Sequence[float]) -> tuple[float, float]:
    """Centroide normalizado ``(cx, cy)`` de una caja ``[xmin,ymin,xmax,ymax]``."""
    xmin, ymin, xmax, ymax = bbox
    return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


@dataclass
class Track:
    """Pista activa: identidad estable de un objeto a lo largo de frames.

    Attributes:
        track_id: id entero estable, asignado por el contador monótono del tracker.
        bbox: última caja ``[xmin, ymin, xmax, ymax]`` en floats normalizados 0..1.
        centroid: centroide normalizado ``(cx, cy)`` de ``bbox``.
        score: score (confianza) de la última detección asociada.
        age: número de frames transcurridos desde la creación de la pista.
        hits: número de actualizaciones en las que la pista tuvo match.
        time_since_update: frames consecutivos sin match (0 si hubo match ahora).
        history: historial corto y acotado de centroides (el más reciente al final).
        last_ts: timestamp de la última actualización con match (float, epoch s/ms).
    """

    track_id: int
    bbox: list[float]
    centroid: tuple[float, float]
    score: float = 0.0
    age: int = 0
    hits: int = 1
    time_since_update: int = 0
    history: deque[tuple[float, float]] = field(default_factory=deque)
    last_ts: float | None = None


class Tracker(abc.ABC):
    """Interfaz mínima de un tracker del pipeline de borde."""

    @abc.abstractmethod
    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Procesa las detecciones de un frame y devuelve las pistas activas.

        Args:
            detections: detecciones del frame actual (cajas normalizadas + score).
            ts: timestamp del frame (epoch en segundos o ms, float).

        Returns:
            Lista de ``Track`` activos tras la actualización.
        """
        raise NotImplementedError


def _iou_matrix(tracks_xyxy: np.ndarray, dets_xyxy: np.ndarray) -> np.ndarray:
    """Matriz IoU ``(n_tracks, n_dets)`` entre cajas ``[xmin,ymin,xmax,ymax]``.

    Vectorizado en numpy. Devuelve una matriz vacía con la forma correcta si no
    hay tracks o no hay detecciones.
    """
    n_t, n_d = tracks_xyxy.shape[0], dets_xyxy.shape[0]
    if n_t == 0 or n_d == 0:
        return np.zeros((n_t, n_d), dtype=np.float64)

    # Broadcasting: tracks -> (n_t, 1, 4), dets -> (1, n_d, 4).
    t = tracks_xyxy[:, None, :]
    d = dets_xyxy[None, :, :]

    inter_x1 = np.maximum(t[..., 0], d[..., 0])
    inter_y1 = np.maximum(t[..., 1], d[..., 1])
    inter_x2 = np.minimum(t[..., 2], d[..., 2])
    inter_y2 = np.minimum(t[..., 3], d[..., 3])

    inter_w = np.clip(inter_x2 - inter_x1, 0.0, None)
    inter_h = np.clip(inter_y2 - inter_y1, 0.0, None)
    inter = inter_w * inter_h

    area_t = np.clip(t[..., 2] - t[..., 0], 0.0, None) * np.clip(t[..., 3] - t[..., 1], 0.0, None)
    area_d = np.clip(d[..., 2] - d[..., 0], 0.0, None) * np.clip(d[..., 3] - d[..., 1], 0.0, None)
    union = area_t + area_d - inter

    # Evita 0/0; donde la unión es 0 el IoU es 0.
    return np.where(union > 0.0, inter / np.where(union > 0.0, union, 1.0), 0.0)


def _centroid_dist_matrix(tracks_xyxy: np.ndarray, dets_xyxy: np.ndarray) -> np.ndarray:
    """Matriz de distancias euclídeas entre centroides ``(n_tracks, n_dets)``."""
    n_t, n_d = tracks_xyxy.shape[0], dets_xyxy.shape[0]
    if n_t == 0 or n_d == 0:
        return np.zeros((n_t, n_d), dtype=np.float64)

    t_cx = (tracks_xyxy[:, 0] + tracks_xyxy[:, 2]) / 2.0
    t_cy = (tracks_xyxy[:, 1] + tracks_xyxy[:, 3]) / 2.0
    d_cx = (dets_xyxy[:, 0] + dets_xyxy[:, 2]) / 2.0
    d_cy = (dets_xyxy[:, 1] + dets_xyxy[:, 3]) / 2.0

    dx = t_cx[:, None] - d_cx[None, :]
    dy = t_cy[:, None] - d_cy[None, :]
    return np.sqrt(dx * dx + dy * dy)


class CentroidIoUTracker(Tracker):
    """Tracker centroide+IoU en numpy puro con ids monótonos no reutilizables.

    Asocia detecciones entre frames priorizando IoU (con *gating* por
    ``iou_threshold``) y desempatando por menor distancia de centroides. Las
    pistas sin match durante ``max_age`` frames consecutivos se retiran. Cada
    pista mantiene un historial de centroides acotado a ``max_history``.

    Args:
        iou_threshold: IoU mínimo para considerar un par (track, detección) como
            candidato a match. Default ``0.3``.
        max_age: nº de frames consecutivos sin match tras los cuales una pista se
            retira (cuando ``time_since_update`` alcanza ``max_age``). Default ``30``.
        max_history: longitud máxima del historial de centroides por pista.
            Default ``30``.
    """

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_age: int = 30,
        max_history: int = 30,
    ) -> None:
        self.iou_threshold = float(iou_threshold)
        self.max_age = int(max_age)
        self.max_history = int(max_history)
        self._tracks: list[Track] = []
        # Contador MONÓTONO: sólo incrementa; nunca reutiliza un id retirado.
        self._next_id: int = 1

    @property
    def tracks(self) -> list[Track]:
        """Pistas actualmente activas (copia de la lista interna)."""
        return list(self._tracks)

    def _new_id(self) -> int:
        """Devuelve un ``track_id`` fresco y avanza el contador monótono."""
        tid = self._next_id
        self._next_id += 1
        return tid

    def _match(
        self, tracks_xyxy: np.ndarray, dets_xyxy: np.ndarray
    ) -> tuple[list[tuple[int, int]], set[int], set[int]]:
        """Empareja tracks y detecciones de forma *greedy* y determinista.

        Returns:
            ``(matches, unmatched_tracks, unmatched_dets)`` donde ``matches`` es
            una lista de pares ``(track_idx, det_idx)``.
        """
        n_t, n_d = tracks_xyxy.shape[0], dets_xyxy.shape[0]
        unmatched_tracks = set(range(n_t))
        unmatched_dets = set(range(n_d))
        matches: list[tuple[int, int]] = []
        if n_t == 0 or n_d == 0:
            return matches, unmatched_tracks, unmatched_dets

        iou = _iou_matrix(tracks_xyxy, dets_xyxy)
        dist = _centroid_dist_matrix(tracks_xyxy, dets_xyxy)

        # Candidatos: sólo pares por encima del umbral (IoU gating).
        candidates: list[tuple[float, float, int, int]] = []
        for i in range(n_t):
            for j in range(n_d):
                if iou[i, j] >= self.iou_threshold:
                    # Orden de prioridad determinista: mayor IoU, menor distancia
                    # de centroides, y por último los índices (estable).
                    candidates.append((-float(iou[i, j]), float(dist[i, j]), i, j))
        candidates.sort()

        for _neg_iou, _d, i, j in candidates:
            if i in unmatched_tracks and j in unmatched_dets:
                matches.append((i, j))
                unmatched_tracks.discard(i)
                unmatched_dets.discard(j)

        return matches, unmatched_tracks, unmatched_dets

    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Actualiza el tracker con las detecciones de un frame.

        Ver :meth:`Tracker.update`. Incrementa la edad de todas las pistas,
        asocia detecciones, crea pistas nuevas, retira las que superan
        ``max_age`` sin match y devuelve las pistas activas resultantes.
        """
        # 1) Una llamada = un frame: toda pista existente envejece.
        for track in self._tracks:
            track.age += 1

        dets = list(detections)
        tracks_xyxy = np.array(
            [t.bbox for t in self._tracks], dtype=np.float64
        ).reshape(-1, 4)
        dets_xyxy = np.array(
            [d.bbox_norm for d in dets], dtype=np.float64
        ).reshape(-1, 4)

        matches, unmatched_tracks, unmatched_dets = self._match(tracks_xyxy, dets_xyxy)

        # 2) Pistas con match: actualizar caja/centroid/score e historial.
        for i, j in matches:
            track = self._tracks[i]
            det = dets[j]
            track.bbox = list(det.bbox_norm)
            track.centroid = _centroid(det.bbox_norm)
            track.score = float(det.confidence)
            track.hits += 1
            track.time_since_update = 0
            track.last_ts = ts
            track.history.append(track.centroid)

        # 3) Pistas sin match: acumulan ausencia.
        for i in unmatched_tracks:
            self._tracks[i].time_since_update += 1

        # 4) Detecciones sin match: nuevas pistas con id fresco (monótono).
        for j in sorted(unmatched_dets):
            det = dets[j]
            centroid = _centroid(det.bbox_norm)
            track = Track(
                track_id=self._new_id(),
                bbox=list(det.bbox_norm),
                centroid=centroid,
                score=float(det.confidence),
                age=0,
                hits=1,
                time_since_update=0,
                history=deque([centroid], maxlen=self.max_history),
                last_ts=ts,
            )
            self._tracks.append(track)

        # 5) Retiro: descartar pistas con max_age frames consecutivos sin match.
        self._tracks = [t for t in self._tracks if t.time_since_update < self.max_age]

        return list(self._tracks)
