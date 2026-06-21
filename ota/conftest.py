"""Hace importables `agent/` y `tools/` cuando pytest corre desde `ota/` (o desde la raíz).

`python -m pytest` ya añade el cwd al sys.path, pero insertamos explícitamente el directorio
`ota/` para que `import agent` / `import tools` funcionen sin depender del cwd.
"""
import os
import sys

_OTA_DIR = os.path.dirname(os.path.abspath(__file__))
if _OTA_DIR not in sys.path:
    sys.path.insert(0, _OTA_DIR)
