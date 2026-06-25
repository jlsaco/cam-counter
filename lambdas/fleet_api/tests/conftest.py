"""Hace importables los módulos FLAT del paquete Lambda (igual que el runtime).

En el zip de Lambda los módulos viven en la raíz (``handler.py``, ``ddb.py``, ``keys.py``) y se
importan de forma plana (``import ddb``). Para que los tests reproduzcan EXACTAMENTE ese modelo
de import, se inserta el directorio de la función (padre de ``tests/``) al frente de
``sys.path`` (también deja importable ``fakeddb`` desde el propio directorio de tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

_FUNCTION_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_FUNCTION_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
