"""Tests del ``CommandHandler``: despacho idempotente por ``command_id``.

Cubren en x86 (sin red): ejecución única por ``command_id`` (reentregas devuelven
ack 'duplicate' SIN re-ejecutar), acciones no soportadas / no registradas
(rechazo fail-closed), fallo controlado (status 'error'), ``command_id`` ausente
(rechazo) y la siembra ``register_seen`` (no re-ejecuta tras un restart).
"""

from __future__ import annotations

from cam_counter_edge.command_handler import CommandError, CommandHandler


def _counter_action():
    """Acción que cuenta cuántas veces se EJECUTA (para verificar idempotencia)."""
    calls: list[dict] = []

    def fn(args: dict) -> dict:
        calls.append(args)
        return {"ran": len(calls)}

    return fn, calls


def test_executes_once_and_acks_ok() -> None:
    fn, calls = _counter_action()
    h = CommandHandler({"snapshot": fn})
    ack = h.handle({"command_id": "c1", "action": "snapshot", "args": {"k": 1}})
    assert ack["status"] == "ok"
    assert ack["command_id"] == "c1"
    assert ack["result"] == {"ran": 1}
    assert calls == [{"k": 1}]


def test_idempotent_redelivery_does_not_reexecute() -> None:
    fn, calls = _counter_action()
    h = CommandHandler({"snapshot": fn})
    first = h.handle({"command_id": "c1", "action": "snapshot"})
    second = h.handle({"command_id": "c1", "action": "snapshot"})
    third = h.handle({"command_id": "c1", "action": "snapshot"})
    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert third["status"] == "duplicate"
    assert len(calls) == 1  # ejecutado UNA sola vez pese a 3 entregas
    assert h.is_handled("c1")


def test_distinct_command_ids_each_execute() -> None:
    fn, calls = _counter_action()
    h = CommandHandler({"reload-config": fn})
    h.handle({"command_id": "a", "action": "reload-config"})
    h.handle({"command_id": "b", "action": "reload-config"})
    assert len(calls) == 2


def test_unsupported_action_rejected() -> None:
    h = CommandHandler({"snapshot": lambda a: {}})
    ack = h.handle({"command_id": "c1", "action": "explode"})
    assert ack["status"] == "rejected"
    assert "no soportada" in ack["error"]


def test_supported_but_unregistered_action_rejected() -> None:
    h = CommandHandler({})  # 'restart' es soportado por contrato pero no registrado
    ack = h.handle({"command_id": "c1", "action": "restart"})
    assert ack["status"] == "rejected"
    assert "no registrada" in ack["error"]


def test_missing_command_id_rejected() -> None:
    fn, calls = _counter_action()
    h = CommandHandler({"snapshot": fn})
    ack = h.handle({"action": "snapshot"})
    assert ack["status"] == "rejected"
    assert calls == []  # sin id no se ejecuta (no hay idempotencia posible)


def test_action_error_acks_error_and_caches() -> None:
    def boom(_args: dict) -> dict:
        raise CommandError("disco lleno")

    h = CommandHandler({"snapshot": boom})
    ack = h.handle({"command_id": "c1", "action": "snapshot"})
    assert ack["status"] == "error"
    assert "disco lleno" in ack["error"]
    # Reentrega: no re-ejecuta (idempotente incluso en error).
    again = h.handle({"command_id": "c1", "action": "snapshot"})
    assert again["status"] == "duplicate"


def test_register_seen_prevents_reexecution() -> None:
    fn, calls = _counter_action()
    h = CommandHandler({"restart": fn}, seen_command_ids=["boot-cmd"])
    # 'boot-cmd' ya se ejecutó antes del restart: no se vuelve a ejecutar.
    ack = h.handle({"command_id": "boot-cmd", "action": "restart"})
    assert ack["status"] == "duplicate"
    assert calls == []
    # register_seen en caliente también funciona.
    h.register_seen("later-cmd")
    ack2 = h.handle({"command_id": "later-cmd", "action": "restart"})
    assert ack2["status"] == "duplicate"
    assert calls == []
