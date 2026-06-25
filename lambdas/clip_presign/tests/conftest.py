"""Hace importable el módulo FLAT del paquete Lambda (igual que el runtime).

En el zip de Lambda los módulos viven en la raíz (``handler.py``) y se importan de forma plana
(``import handler``). Para que los tests reproduzcan EXACTAMENTE ese modelo de import, se inserta
el directorio de la función (padre de ``tests/``) al frente de ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FUNCTION_DIR = Path(__file__).resolve().parents[1]
if str(_FUNCTION_DIR) not in sys.path:
    sys.path.insert(0, str(_FUNCTION_DIR))
