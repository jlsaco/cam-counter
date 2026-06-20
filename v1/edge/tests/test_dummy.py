"""Contrato de DummyDetector: secuencia programada, determinista, en orden."""

from __future__ import annotations

from cam_counter_edge.dummy import DummyDetector
from cam_counter_edge.types import Detection


def _script() -> list[list[Detection]]:
    return [
        [Detection(bbox_norm=[0.10, 0.20, 0.30, 0.40], class_id=0, confidence=0.91)],
        [],  # frame sin detecciones
        [
            Detection(bbox_norm=[0.50, 0.50, 0.60, 0.70], class_id=0, confidence=0.80),
            Detection(bbox_norm=[0.05, 0.05, 0.15, 0.25], class_id=0, confidence=0.72),
        ],
    ]


def test_dummy_replays_scripted_sequence_in_order():
    script = _script()
    det = DummyDetector(script)
    # detect() devuelve cada frame programado, en orden, exacto.
    assert det.detect(None) == script[0]
    assert det.detect(None) == script[1]
    assert det.detect(None) == script[2]
    # Agotada la secuencia (loop=False), devuelve [] indefinidamente.
    assert det.detect(None) == []
    assert det.detect(None) == []


def test_dummy_is_deterministic_and_frame_independent():
    script = _script()
    a = DummyDetector(_script())
    b = DummyDetector(_script())
    # Mismo script -> misma salida, sin importar el "frame" pasado.
    for i in range(len(script)):
        out_a = a.detect(frame_bgr=object())
        out_b = b.detect(frame_bgr="otro-frame-totalmente-distinto")
        assert out_a == out_b == script[i]


def test_dummy_loop_cycles():
    script = [[Detection(bbox_norm=[0.0, 0.0, 0.1, 0.1], class_id=0, confidence=0.99)]]
    det = DummyDetector(script, loop=True)
    # Con loop=True la secuencia se repite.
    assert det.detect(None) == script[0]
    assert det.detect(None) == script[0]
    assert det.detect(None) == script[0]


def test_dummy_returns_independent_copies():
    script = [[Detection(bbox_norm=[0.1, 0.1, 0.2, 0.2], class_id=0, confidence=0.9)]]
    det = DummyDetector(script)
    out = det.detect(None)
    out.append("mutado")  # mutar la salida no debe afectar a la secuencia programada
    det.reset()
    assert det.detect(None) == script[0]


def test_dummy_from_bboxes_builds_detections():
    det = DummyDetector.from_bboxes([[[0.2, 0.3, 0.4, 0.5]]], conf=0.6)
    out = det.detect(None)
    assert len(out) == 1
    assert out[0].bbox_norm == [0.2, 0.3, 0.4, 0.5]
    assert out[0].class_id == 0
    assert out[0].confidence == 0.6
