"""Reorden + normalización de la salida NMS de Hailo (función pura, sin Hailo).

La NMS de Hailo entrega cajas ``[ymin, xmin, ymax, xmax, score]`` normalizadas
0..1; ``parse_nms_class`` debe reordenarlas a ``[xmin, ymin, xmax, ymax]`` del
sistema, filtrar por confianza y etiquetar la clase persona.
"""

from __future__ import annotations

from cam_counter_edge.detector import parse_nms_class


def test_parse_reorders_hailo_box_to_system_order() -> None:
    # Salida sintética de Hailo: [ymin, xmin, ymax, xmax, score].
    ymin, xmin, ymax, xmax, score = 0.20, 0.10, 0.80, 0.40, 0.90
    nms = [[ymin, xmin, ymax, xmax, score]]

    dets = parse_nms_class(nms, conf=0.45, class_id=0)

    assert len(dets) == 1
    det = dets[0]
    # Orden del sistema: [xmin, ymin, xmax, ymax].
    assert det.bbox_norm == [xmin, ymin, xmax, ymax]
    assert det.bbox_norm == [0.10, 0.20, 0.40, 0.80]
    assert det.class_id == 0
    assert det.confidence == score


def test_parse_outputs_are_within_0_1() -> None:
    nms = [
        [0.0, 0.0, 1.0, 1.0, 0.99],
        [0.25, 0.25, 0.75, 0.75, 0.60],
    ]
    dets = parse_nms_class(nms, conf=0.45)
    assert len(dets) == 2
    for det in dets:
        assert all(0.0 <= c <= 1.0 for c in det.bbox_norm)


def test_parse_clamps_out_of_range_values() -> None:
    # Valores ligeramente fuera de [0,1] deben recortarse defensivamente.
    nms = [[-0.05, -0.10, 1.10, 1.20, 0.95]]
    dets = parse_nms_class(nms)
    assert dets[0].bbox_norm == [0.0, 0.0, 1.0, 1.0]


def test_parse_filters_below_confidence_threshold() -> None:
    nms = [
        [0.10, 0.10, 0.20, 0.20, 0.44],  # por debajo de 0.45 -> descartada
        [0.30, 0.30, 0.40, 0.40, 0.45],  # exactamente 0.45 -> aceptada
    ]
    dets = parse_nms_class(nms, conf=0.45)
    assert len(dets) == 1
    assert dets[0].confidence == 0.45


def test_parse_empty_and_none() -> None:
    assert parse_nms_class([]) == []
    assert parse_nms_class(None) == []


def test_parse_default_class_is_person() -> None:
    dets = parse_nms_class([[0.1, 0.1, 0.2, 0.2, 0.9]])
    assert dets[0].class_id == 0
