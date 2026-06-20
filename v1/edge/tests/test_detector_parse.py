"""Reorden/normalización de coordenadas: salida NMS Hailo -> Detection del sistema.

Testea SOLO la función pura ``parse_nms_class`` (sin abrir Hailo ni OpenCV)."""

from __future__ import annotations

from cam_counter_edge.types import Detection, parse_nms_class


def test_parse_reorders_hailo_to_system_order():
    # Fila Hailo: [ymin, xmin, ymax, xmax, score]
    ymin, xmin, ymax, xmax, score = 0.10, 0.20, 0.50, 0.60, 0.90
    dets = parse_nms_class([[ymin, xmin, ymax, xmax, score]], conf=0.45)
    assert len(dets) == 1
    det = dets[0]
    assert isinstance(det, Detection)
    # Reorden al orden del sistema [xmin, ymin, xmax, ymax].
    assert det.bbox_norm == [xmin, ymin, xmax, ymax]
    assert det.bbox_norm == [0.20, 0.10, 0.60, 0.50]
    assert det.class_id == 0
    assert det.confidence == score


def test_parse_all_coords_within_0_1():
    dets = parse_nms_class([[0.0, 0.0, 1.0, 1.0, 0.99]], conf=0.45)
    assert len(dets) == 1
    for coord in dets[0].bbox_norm:
        assert 0.0 <= coord <= 1.0


def test_parse_clamps_out_of_range_defensively():
    # Valores fuera de 0..1 deben recortarse a [0,1] (invariante de coordenadas).
    dets = parse_nms_class([[-0.10, -0.20, 1.30, 1.50, 0.90]], conf=0.45)
    assert dets[0].bbox_norm == [0.0, 0.0, 1.0, 1.0]


def test_parse_filters_below_confidence():
    rows = [
        [0.1, 0.2, 0.3, 0.4, 0.40],  # < 0.45 -> descartada
        [0.1, 0.2, 0.3, 0.4, 0.45],  # == 0.45 -> conservada (sc < CONF descarta)
        [0.1, 0.2, 0.3, 0.4, 0.95],  # > 0.45 -> conservada
    ]
    dets = parse_nms_class(rows, conf=0.45)
    assert len(dets) == 2
    assert [round(d.confidence, 2) for d in dets] == [0.45, 0.95]


def test_parse_empty_and_none():
    assert parse_nms_class([]) == []
    assert parse_nms_class(None) == []


def test_parse_labels_person_class_id():
    dets = parse_nms_class([[0.1, 0.2, 0.3, 0.4, 0.9]], conf=0.45, class_id=0)
    assert dets[0].class_id == 0
