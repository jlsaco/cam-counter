"""Tests x86 del reconciliador de Device Shadow (WP15) — sin broker ni red.

Cubren la lógica de reconciliación nube<->SQLite y el canal de comandos con un
cliente MQTT FAKE (captura subscribe/publish) y un ``Store`` SQLite en ``tmp_path``:

- **reconciliación**: un ``desired`` válido con versión mayor se aplica en SQLite e
  incrementa ``config_version``; el ``ConfigWatcher`` existente lo recargaría en
  caliente (se verifica que la línea aplicada es la de la nube).
- **conflicto gana la versión mayor**: un ``desired`` con ``config_version`` <= local
  se IGNORA (no muta SQLite) y se RE-REPORTA el estado local.
- **fail-closed**: un ``desired`` que NO casa el contrato ``line_config`` VERBATIM
  NO se aplica.
- **multi-cámara**: shadows ``line-config-{camera_id}`` enrutados por cámara, sin
  cruce de estado entre cámaras.
- **comando idempotente**: el MISMO ``command_id`` ejecuta el handler UNA vez; la
  repetición re-acka sin re-ejecutar.
- **coexistencia UI local**: una edición local (``set_line_config``) se refleja como
  ``reported``.
"""

from __future__ import annotations

import json

from cam_counter_edge.config import ConfigWatcher
from cam_counter_edge.line_counter import LineCounter
from cam_counter_edge.shadow_reconciler import (
    ShadowReconciler,
    line_config_doc,
    line_config_shadow_base,
    validate_line_config,
)
from cam_counter_edge.store import Store
from cam_counter_edge.types import Line, LineConfig, Point

SITE = "site-a"
DEVICE = "pi-001"
THING = "cam-counter-site-a-pi-001"
CAM0 = "pi-001-cam0"
CAM1 = "pi-001-cam1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _Msg:
    """Forma mínima de un ``MQTTMessage`` de paho (topic + payload)."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Cliente MQTT fake: captura ``subscribe``/``publish`` y guarda ``on_message``."""

    def __init__(self) -> None:
        self.on_message = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes]] = []

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append(topic)

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False) -> None:
        body = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        self.published.append((topic, bytes(body)))

    # Helpers de inspección -------------------------------------------------

    def publishes_to(self, topic: str) -> list[dict]:
        out = []
        for t, body in self.published:
            if t == topic and body:
                try:
                    out.append(json.loads(body.decode("utf-8")))
                except ValueError:
                    pass
        return out

    def last_to(self, topic: str) -> dict | None:
        msgs = self.publishes_to(topic)
        return msgs[-1] if msgs else None


def _desired_doc(
    camera_id: str,
    *,
    config_version: int,
    ax: float = 0.5,
    ay: float = 0.0,
    bx: float = 0.5,
    by: float = 1.0,
    positive_side: int = 1,
) -> dict:
    """Documento ``line_config`` válido (espejo del desired que escribe la nube)."""
    return {
        "site_id": SITE,
        "device_id": DEVICE,
        "camera_id": camera_id,
        "config_version": config_version,
        "line": {"a": {"x": ax, "y": ay}, "b": {"x": bx, "y": by}},
        "positive_side": positive_side,
        "positive_label": "subieron",
        "negative_label": "bajaron",
        "schema_version": 1,
    }


def _delta_payload(desired: dict) -> bytes:
    """update/delta: el ``state`` ES el desired que cambió."""
    return json.dumps({"state": desired, "version": 1}).encode("utf-8")


def _get_accepted_payload(desired: dict) -> bytes:
    """get/accepted: el desired vive en ``state.desired``."""
    return json.dumps({"state": {"desired": desired}}).encode("utf-8")


def _make(tmp_path, camera_ids=(CAM0,), client=None, handlers=None):
    store = Store(str(tmp_path / "events.db"))
    rec = ShadowReconciler(
        store,
        thing_name=THING,
        device_id=DEVICE,
        camera_ids=list(camera_ids),
        client=client,
        command_handlers=handlers,
    )
    return store, rec


# --------------------------------------------------------------------------- #
# Validación VERBATIM del contrato
# --------------------------------------------------------------------------- #


def test_validate_line_config_accepts_valid_doc() -> None:
    assert validate_line_config(_desired_doc(CAM0, config_version=3)) == []


def test_validate_line_config_rejects_unknown_field() -> None:
    """``additionalProperties:false``: un campo extra (p.ej. min_confidence) se rechaza."""
    doc = _desired_doc(CAM0, config_version=1)
    doc["min_confidence"] = 0.5  # campo que NO existe en el contrato
    reasons = validate_line_config(doc)
    assert any("min_confidence" in r for r in reasons)


def test_validate_line_config_rejects_missing_required_and_bad_geometry() -> None:
    doc = _desired_doc(CAM0, config_version=1)
    del doc["positive_side"]  # requerido
    doc["line"]["a"]["x"] = 1.5  # fuera de 0..1 (maximum)
    reasons = validate_line_config(doc)
    assert any("positive_side" in r for r in reasons)
    assert any("máximo" in r or "maximum" in r for r in reasons)


def test_validate_line_config_rejects_bad_positive_side_enum() -> None:
    doc = _desired_doc(CAM0, config_version=1, positive_side=0)
    reasons = validate_line_config(doc)
    assert any("positive_side" in r for r in reasons)


# --------------------------------------------------------------------------- #
# Reconciliación nube -> SQLite (delta y get/accepted)
# --------------------------------------------------------------------------- #


def test_delta_applies_higher_version_to_sqlite(tmp_path) -> None:
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    assert store.get_config_version(CAM0) == 0

    base = line_config_shadow_base(THING, CAM0)
    rec.handle_message(f"{base}/update/delta", _delta_payload(_desired_doc(CAM0, config_version=5)))

    # Aplicado en SQLite con la versión EXACTA de la nube (gana versión mayor).
    assert store.get_config_version(CAM0) == 5
    cfg = store.get_line_config(CAM0)
    assert cfg is not None and cfg.config_version == 5
    # Se reportó la nueva config (reported == desired -> el delta se cierra).
    reported = client.last_to(f"{base}/update")
    assert reported is not None
    assert reported["state"]["reported"]["config_version"] == 5
    store.close()


def test_boot_get_accepted_syncs_offline_desired(tmp_path) -> None:
    """Boot: shadow get/accepted sincroniza un desired que cambió OFFLINE."""
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    base = line_config_shadow_base(THING, CAM0)

    rec.handle_message(
        f"{base}/get/accepted", _get_accepted_payload(_desired_doc(CAM0, config_version=2))
    )
    assert store.get_config_version(CAM0) == 2
    store.close()


def test_conflict_higher_version_wins_ignores_stale(tmp_path) -> None:
    """Un desired con versión <= local se IGNORA y se re-reporta el estado local."""
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    base = line_config_shadow_base(THING, CAM0)

    # Local ya en v4 (la UI local, offline, adelantó la versión).
    store.apply_remote_line_config(
        LineConfig(
            site_id=SITE, device_id=DEVICE, camera_id=CAM0, config_version=4,
            line=Line(a=Point(0.9, 0.0), b=Point(0.9, 1.0)), positive_side=1,
        )
    )
    assert store.get_config_version(CAM0) == 4

    # Llega un desired stale (v3 < 4): NO debe mutar SQLite.
    out = rec._reconcile_line_config(CAM0, _desired_doc(CAM0, config_version=3), source="delta")
    assert out.ignored_stale is True and out.applied is False
    assert store.get_config_version(CAM0) == 4
    cfg = store.get_line_config(CAM0)
    assert cfg is not None and (cfg.line.a.x, cfg.line.a.y) == (0.9, 0.0)  # geometría intacta
    # Re-reporta lo local (v4) para que la nube se ponga al día.
    reported = client.last_to(f"{base}/update")
    assert reported is not None and reported["state"]["reported"]["config_version"] == 4

    # Un desired con versión IGUAL tampoco gana (monótono estricto).
    out_eq = rec._reconcile_line_config(CAM0, _desired_doc(CAM0, config_version=4), source="delta")
    assert out_eq.ignored_stale is True
    assert store.get_config_version(CAM0) == 4
    store.close()


def test_invalid_desired_is_fail_closed(tmp_path) -> None:
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)

    bad = _desired_doc(CAM0, config_version=7)
    bad["positive_side"] = 0  # fuera del enum {-1, 1}
    out = rec._reconcile_line_config(CAM0, bad, source="delta")
    assert out.rejected_contract is True and out.applied is False
    assert store.get_config_version(CAM0) == 0  # NO se aplicó nada
    store.close()


def test_desired_camera_mismatch_rejected(tmp_path) -> None:
    """Un desired cuyo camera_id no es el del shadow se descarta (fail-closed)."""
    store, rec = _make(tmp_path, client=FakeMqttClient(), camera_ids=(CAM0, CAM1))
    out = rec._reconcile_line_config(CAM0, _desired_doc(CAM1, config_version=2), source="delta")
    assert out.rejected_contract is True and out.applied is False
    assert store.get_config_version(CAM0) == 0
    store.close()


def test_multicamera_routing_is_independent(tmp_path) -> None:
    """Cada cámara tiene su named shadow; el estado NO se cruza entre cámaras."""
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client, camera_ids=(CAM0, CAM1))

    base0 = line_config_shadow_base(THING, CAM0)
    base1 = line_config_shadow_base(THING, CAM1)
    rec.handle_message(
        f"{base0}/update/delta", _delta_payload(_desired_doc(CAM0, config_version=3))
    )
    rec.handle_message(
        f"{base1}/update/delta", _delta_payload(_desired_doc(CAM1, config_version=9))
    )

    assert store.get_config_version(CAM0) == 3
    assert store.get_config_version(CAM1) == 9
    store.close()


def test_hot_reload_via_config_watcher_after_remote_apply(tmp_path) -> None:
    """Integración con ConfigWatcher: tras aplicar el desired, el detector recarga.

    Línea local v1 vertical en x=0.5; tras un desired de la nube que la mueve a
    x=0.95 (v2), el ConfigWatcher.poll() recarga la geometría EN CALIENTE.
    """
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    base = line_config_shadow_base(THING, CAM0)

    # Config local inicial (como si la dejara la UI / un desired previo): x=0.5, v1.
    store.apply_remote_line_config(
        LineConfig(
            site_id=SITE, device_id=DEVICE, camera_id=CAM0, config_version=1,
            line=Line(a=Point(0.5, 0.0), b=Point(0.5, 1.0)), positive_side=1,
        )
    )
    cfg = store.get_line_config(CAM0)
    lc = LineCounter.from_config(store, cfg, min_frames=1)
    watcher = ConfigWatcher(store, lc, CAM0)
    assert lc.line_version == 1

    # La nube mueve la línea a x=0.95 (v2) vía delta.
    rec.handle_message(f"{base}/update/delta", _delta_payload(
        _desired_doc(CAM0, config_version=2, ax=0.95, bx=0.95)))
    assert store.get_config_version(CAM0) == 2

    # El ConfigWatcher existente recarga en caliente (sin reiniciar).
    assert watcher.poll() is True
    assert lc.line_version == 2
    assert lc.a == (0.95, 0.0) and lc.b == (0.95, 1.0)
    store.close()


def test_local_ui_edit_is_reported(tmp_path) -> None:
    """Coexistencia UI local: una edición en SQLite se refleja como reported."""
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    base = line_config_shadow_base(THING, CAM0)

    # La UI local edita la línea (camino CAS de set_line_config).
    store.set_line_config(
        CAM0,
        LineConfig(
            site_id=SITE, device_id=DEVICE, camera_id=CAM0, config_version=0,
            line=Line(a=Point(0.3, 0.0), b=Point(0.3, 1.0)), positive_side=-1,
        ),
        expected_version=0,
    )
    # El borde detecta el cambio y reporta.
    assert rec.publish_reported(CAM0) is True
    reported = client.last_to(f"{base}/update")
    assert reported is not None
    state = reported["state"]["reported"]
    assert state["config_version"] == 1
    assert state["positive_side"] == -1
    assert state["camera_id"] == CAM0
    store.close()


# --------------------------------------------------------------------------- #
# Comandos idempotentes
# --------------------------------------------------------------------------- #


def test_command_is_idempotent(tmp_path) -> None:
    """El MISMO command_id ejecuta el handler UNA vez; la repetición re-acka."""
    calls = {"n": 0}

    def _snapshot(_command: dict) -> dict:
        calls["n"] += 1
        return {"clip": "snap.jpg"}

    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client, handlers={"snapshot": _snapshot})

    cmd = {"command_id": "cmd-123", "action": "snapshot"}
    out1 = rec.handle_command(cmd)
    assert out1.status == "ok" and out1.executed is True and out1.acked is True
    assert calls["n"] == 1

    # Repetición del MISMO command_id: NO re-ejecuta, pero re-acka (idempotente).
    out2 = rec.handle_command(dict(cmd))
    assert out2.status == "duplicate" and out2.executed is False
    assert out2.acked is True
    assert calls["n"] == 1  # el efecto ocurrió UNA sola vez

    # Dos acks (uno por invocación) en el topic cmd/ack, ambos con el command_id.
    acks = client.publishes_to(rec.cmd_ack_topic)
    assert len(acks) == 2
    assert all(a["command_id"] == "cmd-123" for a in acks)
    store.close()


def test_command_via_cmd_request_topic(tmp_path) -> None:
    """Un comando fire-and-forget por cam-counter/{device}/cmd/request se procesa."""
    seen = {"n": 0}

    def _restart(_command: dict) -> dict:
        seen["n"] = 1
        return {}

    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client, handlers={"restart": _restart})

    rec.handle_message(
        rec.cmd_request_topic,
        json.dumps({"command_id": "c-1", "action": "restart"}).encode("utf-8"),
    )
    assert seen["n"] == 1
    assert client.publishes_to(rec.cmd_ack_topic)
    store.close()


def test_unsupported_action_is_acked_not_executed(tmp_path) -> None:
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    out = rec.handle_command({"command_id": "c-x", "action": "format-disk"})
    assert out.status == "unsupported" and out.executed is False and out.acked is True
    store.close()


def test_command_without_id_is_rejected(tmp_path) -> None:
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    out = rec.handle_command({"action": "snapshot"})
    assert out.status == "rejected" and out.acked is False
    assert client.publishes_to(rec.cmd_ack_topic) == []
    store.close()


def test_reload_config_default_handler_resyncs(tmp_path) -> None:
    """``reload-config`` (handler por defecto) re-pide get y re-reporta cada cámara."""
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client)
    store.apply_remote_line_config(
        LineConfig(
            site_id=SITE, device_id=DEVICE, camera_id=CAM0, config_version=1,
            line=Line(a=Point(0.5, 0.0), b=Point(0.5, 1.0)), positive_side=1,
        )
    )
    out = rec.handle_command({"command_id": "rc-1", "action": "reload-config"})
    assert out.status == "ok" and out.executed is True
    base = line_config_shadow_base(THING, CAM0)
    # Re-pidió el get del shadow y re-reportó lo local.
    assert any(t == f"{base}/get" for t, _ in client.published)
    assert client.last_to(f"{base}/update") is not None
    store.close()


# --------------------------------------------------------------------------- #
# Suscripción / arranque
# --------------------------------------------------------------------------- #


def test_attach_subscribes_and_requests_get(tmp_path) -> None:
    client = FakeMqttClient()
    store, rec = _make(tmp_path, client=client, camera_ids=(CAM0, CAM1))
    rec.attach()

    base0 = line_config_shadow_base(THING, CAM0)
    assert f"{base0}/update/delta" in client.subscriptions
    assert f"{base0}/get/accepted" in client.subscriptions
    assert rec.cmd_request_topic in client.subscriptions
    # Boot hace shadow get de cada cámara.
    assert any(t == f"{base0}/get" for t, _ in client.published)
    # on_message quedó registrado para el dispatch real de paho.
    assert client.on_message is not None
    store.close()


def test_line_config_doc_roundtrips_through_contract() -> None:
    cfg = LineConfig(
        site_id=SITE, device_id=DEVICE, camera_id=CAM0, config_version=3,
        line=Line(a=Point(0.1, 0.2), b=Point(0.3, 0.4)), positive_side=-1,
        positive_label="up", negative_label="down",
    )
    doc = line_config_doc(cfg)
    assert validate_line_config(doc) == []
    assert doc["config_version"] == 3 and doc["positive_side"] == -1


def test_non_json_payload_is_ignored(tmp_path) -> None:
    store, rec = _make(tmp_path, client=FakeMqttClient())
    base = line_config_shadow_base(THING, CAM0)
    rec.handle_message(f"{base}/update/delta", b"not-json{")  # no debe lanzar
    assert store.get_config_version(CAM0) == 0
    store.close()
