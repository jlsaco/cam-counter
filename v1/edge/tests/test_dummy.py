"""Contrato del DummyDetector: secuencia programada determinista."""

from __future__ import annotations

from cam_counter_edge.dummy import DummyDetector, default_crossing_script
from cam_counter_edge.types import Detection


def test_dummy_replays_scripted_sequence_in_order() -> None:
    script = [
        [Detection(bbox_norm=[0.10, 0.40, 0.20, 0.60], class_id=0, confidence=0.91)],
        [Detection(bbox_norm=[0.45, 0.40, 0.55, 0.60], class_id=0, confidence=0.88)],
        [
            Detection(bbox_norm=[0.70, 0.40, 0.80, 0.60], class_id=0, confidence=0.80),
            Detection(bbox_norm=[0.10, 0.10, 0.20, 0.30], class_id=0, confidence=0.50),
        ],
    ]
    det = DummyDetector(script=script)

    frame0 = det.detect(None)
    assert len(frame0) == 1
    assert frame0[0].bbox_norm == [0.10, 0.40, 0.20, 0.60]
    assert frame0[0].confidence == 0.91

    frame1 = det.detect(None)
    assert frame1[0].bbox_norm == [0.45, 0.40, 0.55, 0.60]

    frame2 = det.detect(None)
    assert len(frame2) == 2

    # Agotada la secuencia, devuelve listas vacías (sin loop).
    assert det.detect(None) == []
    assert det.detect(None) == []


def test_dummy_is_deterministic_across_instances() -> None:
    a = [d for _ in range(6) for d in [DummyDetector().detect(None)]]
    b = [d for _ in range(6) for d in [DummyDetector().detect(None)]]
    # Misma instancia recién creada repite la primera detección de forma idéntica.
    assert a[0][0].bbox_norm == b[0][0].bbox_norm
    assert a[0][0].confidence == b[0][0].confidence


def test_dummy_ignores_frame_content() -> None:
    det1 = DummyDetector()
    det2 = DummyDetector()
    # Distinto "frame" => misma salida, porque la secuencia es independiente del frame.
    assert det1.detect("frame-A")[0].bbox_norm == det2.detect(object())[0].bbox_norm


def test_dummy_reset_and_loop() -> None:
    det = DummyDetector()
    first = det.detect(None)
    det.reset()
    assert det.detect(None)[0].bbox_norm == first[0].bbox_norm

    looped = DummyDetector(loop=True)
    n = len(default_crossing_script())
    seq = [looped.detect(None) for _ in range(n + 1)]
    # Tras n frames, el frame n+1 vuelve a ser el primero (loop).
    assert seq[n][0].bbox_norm == seq[0][0].bbox_norm


def test_default_script_crosses_midline() -> None:
    centers = [d[0].centroid[0] for d in default_crossing_script()]
    # La persona empieza a la izquierda de 0.5 y termina a la derecha (cruza).
    assert centers[0] < 0.5 < centers[-1]
