"""Handler de comandos nube->dispositivo, **idempotente por ``command_id``**.

El canal de comandos (WP15) tiene dos sabores que comparten ESTE handler:

- **Fire-and-forget** (MQTT): ``cam-counter/{device_id}/cmd/request`` ->
  ``cam-counter/{device_id}/cmd/ack``. Útil para acciones inmediatas cuando el
  device está online.
- **Persistente** (Device Shadow ``command``): la nube pone el comando en
  ``desired``; el device lo ejecuta al recibir el delta (o al hacer ``get`` en el
  arranque, si llegó estando offline) y lo refleja en ``reported``. El delta
  converge cuando ``reported.command_id == desired.command_id``.

**Idempotencia (criterio de aceptación):** cada ``command_id`` se ejecuta UNA sola
vez. Reentregas (QoS1, re-``get`` en boot, delta re-disparado) devuelven el MISMO
ack cacheado SIN re-ejecutar la acción. Esto es lo que evita el bucle
"reinicio por comando -> boot -> get -> re-ejecuta el reinicio".

Las **acciones** (``snapshot`` / ``restart`` / ``reload-config``) se inyectan como
callables, de modo que la lógica de despacho/idempotencia/ack se ejercita en CI
x86 sin efectos reales (un fake registra que se llamó).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

__all__ = ["CommandError", "CommandHandler"]

_log = logging.getLogger(__name__)

# Acciones soportadas (canon del contrato de comando). Un comando con una acción
# fuera de esta lista se rechaza (fail-closed): ack con status='rejected'.
SUPPORTED_ACTIONS = ("snapshot", "restart", "reload-config")

# Tipo de una acción: recibe los ``args`` del comando (dict) y devuelve un dict de
# resultado (serializable). Puede lanzar para señalar fallo (-> status='error').
CommandAction = Callable[[dict[str, Any]], dict[str, Any]]


class CommandError(RuntimeError):
    """Una acción de comando falló de forma controlada (-> ack status='error')."""


class CommandHandler:
    """Despacha comandos a acciones registradas, idempotente por ``command_id``.

    Args:
        actions: mapa ``action -> callable``. Sólo las acciones presentes aquí se
            ejecutan; una acción soportada por el contrato pero NO registrada se
            trata como no disponible (ack status='rejected').
        seen_command_ids: ids ya ejecutados conocidos al arrancar (p.ej. el
            ``reported.command_id`` que llega en el ``get/accepted`` del shadow
            ``command``); evita re-ejecutar al rearrancar tras un ``restart``.
    """

    def __init__(
        self,
        actions: dict[str, CommandAction] | None = None,
        *,
        seen_command_ids: list[str] | None = None,
    ) -> None:
        self._actions: dict[str, CommandAction] = dict(actions or {})
        # command_id -> ack ya producido (cache de idempotencia).
        self._acks: dict[str, dict[str, Any]] = {}
        for cid in seen_command_ids or []:
            # Marca como visto SIN ack cacheado: si vuelve a llegar, se ignora.
            self._acks.setdefault(str(cid), {"command_id": str(cid), "status": "duplicate"})

    def register(self, action: str, fn: CommandAction) -> None:
        """Registra (o reemplaza) la acción ``action``."""
        self._actions[action] = fn

    def is_handled(self, command_id: str) -> bool:
        """``True`` si ``command_id`` ya se procesó (no se re-ejecutaría)."""
        return str(command_id) in self._acks

    def register_seen(self, command_id: str) -> None:
        """Marca ``command_id`` como YA visto sin ack real (siembra de idempotencia).

        Lo usa el reconciliador al arrancar con el ``reported.command_id`` que trae
        el ``get/accepted`` del shadow ``command``: un comando ya ejecutado antes de
        rearrancar (p.ej. el propio ``restart``) no se vuelve a ejecutar.
        """
        cid = str(command_id)
        if cid and cid not in self._acks:
            self._acks[cid] = {"command_id": cid, "status": "duplicate"}

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        """Procesa un comando y devuelve su ack (idempotente por ``command_id``).

        El ``command`` debe traer ``command_id`` (str) y ``action`` (str); los
        ``args`` (dict opcional) se pasan a la acción. Reentregas del mismo
        ``command_id`` devuelven el ack cacheado SIN re-ejecutar.

        Ack: ``{command_id, action, status, ...}`` con ``status`` ∈
        ``{'ok','error','rejected','duplicate'}``.
        """
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            # Sin id no hay idempotencia posible: rechazar (no ejecutar).
            return {
                "command_id": command_id if isinstance(command_id, str) else "",
                "action": command.get("action"),
                "status": "rejected",
                "error": "command_id ausente o inválido",
            }

        cached = self._acks.get(command_id)
        if cached is not None:
            # Reentrega: devuelve el MISMO ack, marcado como duplicado, sin actuar.
            _log.info("command-handler: %s ya procesado; devuelvo ack cacheado", command_id)
            ack = dict(cached)
            ack["status"] = "duplicate"
            return ack

        action = command.get("action")
        raw_args = command.get("args")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            ack = {
                "command_id": command_id,
                "action": action,
                "status": "rejected",
                "error": f"acción no soportada: {action!r} (permitidas {SUPPORTED_ACTIONS})",
            }
            self._acks[command_id] = ack
            return ack

        fn = self._actions.get(action)
        if fn is None:
            ack = {
                "command_id": command_id,
                "action": action,
                "status": "rejected",
                "error": f"acción {action!r} no registrada en este device",
            }
            self._acks[command_id] = ack
            return ack

        try:
            result = fn(args) or {}
            ack = {
                "command_id": command_id,
                "action": action,
                "status": "ok",
                "result": result,
            }
        except CommandError as exc:
            ack = {
                "command_id": command_id,
                "action": action,
                "status": "error",
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 — el handler nunca debe propagar y morir
            _log.warning("command-handler: acción %s lanzó %r", action, exc)
            ack = {
                "command_id": command_id,
                "action": action,
                "status": "error",
                "error": repr(exc),
            }
        # Cachea el ack ANTES de devolver: una reentrega ya no re-ejecuta. (Incluso
        # 'restart', cuyo efecto puede rearrancar el proceso: si la acción ejecuta
        # tras cachear, el boot re-get verá el command_id ya visto vía seen_command_ids.)
        self._acks[command_id] = ack
        return ack
