"""Hot-reload de la configuración de línea por cámara: ``ConfigWatcher``.

La UI local (PR09) cambia la línea-umbral escribiendo en SQLite
(``store.set_line_config``), que bumpea un ``config_version`` MONÓTONO por cámara.
El proceso de conteo NO se reinicia: el ``ConfigWatcher`` se llama UNA VEZ POR
FRAME y, mediante una lectura BARATA de ``config_version`` (una sola columna,
``SELECT`` sobre WAL que no bloquea al escritor), detecta si la config cambió.
Sólo cuando cambia recarga el ``LineConfig`` completo y reconfigura la geometría
del ``LineCounter`` EN CALIENTE (``LineCounter.set_line``).

Garantía de camino crítico: el chequeo por frame es lock-free desde el punto de
vista del conteo (no toma el lock de escritura de SQLite, no hace red ni IO de
fichero pesado) y, salvo el frame en que la config realmente cambia, es un único
``SELECT`` de un entero.
"""

from __future__ import annotations

from typing import Protocol

from .identifiers import validate_camera_id
from .line_counter import LineCounter
from .types import LineConfig

__all__ = ["ConfigWatcher"]


class _ConfigSource(Protocol):
    """Interfaz mínima que ``ConfigWatcher`` necesita del ``store``.

    Coincide con ``store.Store`` pero se declara como ``Protocol`` para no
    acoplar este módulo a la implementación concreta de persistencia (un fake
    en memoria sirve igual en tests).
    """

    def get_config_version(self, camera_id: str) -> int: ...

    def get_line_config(self, camera_id: str) -> LineConfig | None: ...


class ConfigWatcher:
    """Vigila el ``config_version`` de UNA cámara y recarga la línea en caliente.

    Args:
        store: fuente de config (expone ``get_config_version`` y
            ``get_line_config``).
        counter: ``LineCounter`` cuya geometría se reconfigura al cambiar la
            config.
        camera_id: cámara vigilada (slug validado).
        initial_version: versión cacheada inicial; por defecto la
            ``line_version`` actual del ``counter`` (la config con la que se
            construyó). Pásala explícitamente si el ``counter`` no se creó desde
            la config persistida.
    """

    def __init__(
        self,
        store: _ConfigSource,
        counter: LineCounter,
        camera_id: str,
        *,
        initial_version: int | None = None,
    ) -> None:
        self._store = store
        self._counter = counter
        self._camera_id = validate_camera_id(camera_id)
        self._cached_version = (
            int(counter.line_version) if initial_version is None else int(initial_version)
        )

    @property
    def version(self) -> int:
        """Último ``config_version`` aplicado al ``LineCounter``."""
        return self._cached_version

    def poll(self) -> bool:
        """Chequea la config UNA VEZ POR FRAME; recarga la línea si cambió.

        Devuelve ``True`` si detectó un ``config_version`` distinto y reconfiguró
        el ``LineCounter`` en caliente; ``False`` si no hubo cambios (caso común,
        coste = un ``SELECT`` de un entero). NUNCA bloquea el camino de conteo:
        no toma lock de escritura ni hace red/IO pesado.
        """
        current = self._store.get_config_version(self._camera_id)
        if current == self._cached_version:
            return False
        config = self._store.get_line_config(self._camera_id)
        if config is None:
            # La fila desapareció (config borrada): sincronizamos la versión
            # cacheada pero NO tocamos la geometría vigente (degradación limpia).
            self._cached_version = current
            return False
        self._counter.set_line(config)
        self._cached_version = config.config_version
        return True
