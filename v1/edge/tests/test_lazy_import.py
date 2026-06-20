"""Import perezoso: cam_counter_edge.detector se importa SIN hailo_platform."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_detector_module_imports_without_hailo():
    """Forzar la AUSENCIA de hailo_platform y (re)importar el módulo del detector.

    Poner ``sys.modules['hailo_platform'] = None`` hace que cualquier
    ``import hailo_platform`` lance ImportError. Si el módulo tuviera un import de Hailo
    a NIVEL DE MÓDULO, el reload de abajo fallaría. Como es perezoso, debe funcionar.
    """
    saved = sys.modules.get("hailo_platform", "MISSING")
    sys.modules["hailo_platform"] = None  # fuerza ImportError en cualquier import lazy
    try:
        mod = importlib.import_module("cam_counter_edge.detector")
        importlib.reload(mod)  # re-ejecuta el cuerpo del módulo con Hailo ausente
        assert hasattr(mod, "Detector")
        # Construir el Detector NO debe importar Hailo ni abrir hardware.
        det = mod.Detector()
        assert det is not None
    finally:
        if saved == "MISSING":
            sys.modules.pop("hailo_platform", None)
        else:
            sys.modules["hailo_platform"] = saved


def test_open_raises_when_hailo_absent():
    """La pereza empuja el import a la llamada: open() falla con Hailo ausente."""
    import cam_counter_edge.detector as detector_mod

    saved = sys.modules.get("hailo_platform", "MISSING")
    sys.modules["hailo_platform"] = None
    try:
        det = detector_mod.Detector()  # construcción OK sin Hailo
        with pytest.raises(ImportError):
            det.open()  # aquí sí se intenta importar hailo_platform
    finally:
        if saved == "MISSING":
            sys.modules.pop("hailo_platform", None)
        else:
            sys.modules["hailo_platform"] = saved


def test_package_imports_without_hailo():
    """Importar el paquete raíz tampoco requiere hailo_platform."""
    saved = sys.modules.get("hailo_platform", "MISSING")
    sys.modules["hailo_platform"] = None
    try:
        pkg = importlib.import_module("cam_counter_edge")
        assert hasattr(pkg, "Detector")
        assert hasattr(pkg, "DummyDetector")
    finally:
        if saved == "MISSING":
            sys.modules.pop("hailo_platform", None)
        else:
            sys.modules["hailo_platform"] = saved
