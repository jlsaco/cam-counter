"""Import perezoso de ``hailo_platform``: el módulo se importa en x86 sin Hailo."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_detector_module_imports_without_hailo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fuerza la AUSENCIA de hailo_platform (setear a None hace que `import` falle).
    monkeypatch.setitem(sys.modules, "hailo_platform", None)
    # Re-importa el submódulo desde cero para ejecutar su cuerpo sin Hailo.
    monkeypatch.delitem(sys.modules, "cam_counter_edge.detector", raising=False)

    mod = importlib.import_module("cam_counter_edge.detector")

    assert hasattr(mod, "Detector")
    assert hasattr(mod, "parse_nms_class")


def test_constructing_detector_does_not_open_hardware() -> None:
    # Construir un Detector NO debe importar hailo_platform ni abrir hardware.
    from cam_counter_edge.detector import CONF, Detector  # noqa: PLC0415

    det = Detector()
    assert det.conf == CONF
    assert det.person_id == 0


def test_hailo_not_importable_at_module_level() -> None:
    # Garantiza que importar el detector no metió hailo_platform en sys.modules.
    sys.modules.pop("hailo_platform", None)
    importlib.import_module("cam_counter_edge.detector")
    assert "hailo_platform" not in sys.modules
