"""Pone el directorio del paquete de la Lambda en ``sys.path``.

La Lambda se empaqueta PLANA (handler.py, validation.py, ... en la raíz del zip), así
que los imports son de nivel superior (``import handler``). Los tests replican ese
layout insertando el directorio del paquete (padre de ``tests/``) en el path.
"""

import os
import sys

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
