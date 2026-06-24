"""Pone el directorio del paquete de la Lambda en ``sys.path`` (layout plano del zip)."""

import os
import sys

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
