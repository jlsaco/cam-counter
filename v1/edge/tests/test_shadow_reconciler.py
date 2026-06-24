"""Tests del ``ShadowReconciler`` (reconciliación Device Shadow <-> SQLite).

Cubren en x86 (SQLite WAL en ``tmp_path``, cliente MQTT FAKE, sin red ni broker):

- **Boot**: ``get/accepted`` con ``desired`` offline -> valida VERBATIM -> SQLite
  -> publica ``reported``.
- **Delta**: cambio nube -> aplica -> ``ConfigWatcher`` recarga EN CALIENTE.
- **Arbitraje**: gana la versión MAYOR (desired <= actual se ignora y re-reporta);
  desired > actual se aplica con esa versión VERBATIM (converge).
- **Multi-cámara**: cada named shadow ``line-config-{camera_id}`` resuelve a su
  cámara; un ``camera_id`` que no casa el shadow se rechaza (anti cross-camera).
- **UI local**: una edición del SQLite se refleja como ``reported`` vía ``tick()``.
- **Contrato**: un ``desired`` fuera de contrato se rechaza (fail-closed), sin
  tocar SQLite.
- **Comandos**: ``cmd/request`` -> ``cmd/ack``; persistente vía shadow ``command``;
  idempotente por ``command_id``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from cam_counter_edge.command_handler import CommandHandler
from cam_counter_edge.config import ConfigWatcher
from cam_counter_edge.line_counter import LineCounter
from cam_counter_edge.shadow_reconciler import (
    ShadowReconciler,
    command_ack_topic,
    command_request_topic,
    line_config_shadow_name,
    named_shadow_topic,
)
from cam_counter_edge.store import Store
from cam_counter_edge.types import Line, LineConfig, Point

SITE = "site-a"
DEVICE = "pi-001"
THING = "cam-counter-site-a-pi-001"
CAM0 = "pi-001-cam0"
CAM1 = "pi-001-cam1"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeMqttClient:
    """Cliente MQTT FAKE: registra publishes/subscribes y enruta mensajes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscriptions: list[str] = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self._mid = 0

    def tls_set(self, **_kwargs) -> None:  # pragma: no cover - no-op
        pass

    def connect(self, *_a, **_k) -> None:  # pragma: no cover - no-op
        pass

    def loop_start(self) -> None:  # pragma: no cover - no-op
        pass

    def loop_stop(self) -> None:  # pragma: no cover - no-op
        pass

    def disconnect(self) -> None:  # pragma: no cover - no-op
        pass

    def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append(topic)

    def publish(self, topic: str, payload, qos: int, retain: bool):
        self._mid += 1
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0, mid=self._mid)

    def feed(self, topic: str, payload) -> None:
        """Simula un mensaje entrante (invoca on_message como paho)."""
        msg = SimpleNamespace(topic=topic, payload=payload)
        assert self.on_message is not None
        self.on_message(self, None, msg)

    # -- helpers de aserción --------------------------------------------

    def reported_docs(self, camera_id: str) -> list[dict]:
        """Documentos ``reported`` publicados al shadow de una cámara."""
        update = named_shadow_topic(THING, line_config_shadow_name(camera_id), "update")
        out = []
        for topic, payload, _q, _r in self.published:
            if topic == update:
                out.append(json.loads(payload)["state"]["reported"])
        return out

    def acks(self, device_id: str) -> list[dict]:
        ack_topic = command_ack_topic(device_id)
        return [
            json.loads(p)
            for t, p, _q, _r in self.published
            if t == ack_topic
        ]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _desired(camera_id: str, *, config_version: int, ax: float = 0.5, ay: float = 0.0,
             bx: float = 0.5, by: float = 1.0, positive_side: int = 1) -> dict:
    """Documento ``desired`` (LineConfig) válido contra el contrato."""
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


def _local_config(camera_id: str, ax: float, ay: float, bx: float, by: float,
                  *, config_version: int = 1) -> LineConfig:
    return LineConfig(
        site_id=SITE, device_id=DEVICE, camera_id=camera_id,
        config_version=config_version,
        line=Line(a=Point(ax, ay), b=Point(bx, by)),
        positive_side=1, positive_label="subieron", negative_label="bajaron",
    )


def _open(tmp_path) -> Store:
    return Store(str(tmp_path / "events.db"))


def _reconciler(store, client, cameras=(CAM0,), command_handler=None) -> ShadowReconciler:
    return ShadowReconciler(
        store,
        thing_name=THING,
        device_id=DEVICE,
        camera_ids=list(cameras),
        client=client,
        command_handler=command_handler,
    )


# --------------------------------------------------------------------------- #
# Suscripciones / boot
# --------------------------------------------------------------------------- #


def test_subscriptions_cover_all_shadows_and_command(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, cameras=(CAM0, CAM1))
    subs = rec.subscriptions()
    for cam in (CAM0, CAM1):
        s = line_config_shadow_name(cam)
        assert named_shadow_topic(THING, s, "update/delta") in subs
        assert named_shadow_topic(THING, s, "get/accepted") in subs
    assert named_shadow_topic(THING, "command", "update/delta") in subs
    assert command_request_topic(DEVICE) in subs
    store.close()


def test_request_get_all_publishes_get_per_shadow(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, cameras=(CAM0, CAM1))
    rec.request_get_all()
    topics = {t for t, *_ in client.published}
    assert named_shadow_topic(THING, line_config_shadow_name(CAM0), "get") in topics
    assert named_shadow_topic(THING, line_config_shadow_name(CAM1), "get") in topics
    assert named_shadow_topic(THING, "command", "get") in topics
    store.close()


def test_boot_get_accepted_syncs_desired_offline(tmp_path) -> None:
    """Boot: ``get/accepted`` trae el ``desired`` que llegó offline -> SQLite + reported."""
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()  # arranca on_message; on_connect no se llama solo en el fake

    doc = {"state": {"desired": _desired(CAM0, config_version=5, ax=0.3)}}
    topic = named_shadow_topic(THING, line_config_shadow_name(CAM0), "get/accepted")
    client.feed(topic, json.dumps(doc).encode())

    cfg = store.get_line_config(CAM0)
    assert cfg is not None
    assert cfg.config_version == 5  # versión VERBATIM del desired (convergencia)
    assert cfg.line.a.x == 0.3
    reported = client.reported_docs(CAM0)
    assert reported and reported[-1]["config_version"] == 5
    store.close()


def test_get_accepted_without_desired_reports_local(tmp_path) -> None:
    """Boot con shadow vacío: re-reporta la config local vigente."""
    store = _open(tmp_path)
    store.set_line_config(CAM0, _local_config(CAM0, 0.5, 0.0, 0.5, 1.0), expected_version=0)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    topic = named_shadow_topic(THING, line_config_shadow_name(CAM0), "get/accepted")
    client.on_message = rec._on_message  # sin connect()
    client.feed(topic, json.dumps({"state": {}}).encode())
    reported = client.reported_docs(CAM0)
    assert reported and reported[-1]["config_version"] == 1
    store.close()


# --------------------------------------------------------------------------- #
# Delta + arbitraje por config_version
# --------------------------------------------------------------------------- #


def test_delta_applies_and_reports(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()
    delta = {"state": _desired(CAM0, config_version=2, ax=0.7)}
    topic = named_shadow_topic(THING, line_config_shadow_name(CAM0), "update/delta")
    client.feed(topic, json.dumps(delta).encode())
    cfg = store.get_line_config(CAM0)
    assert cfg is not None and cfg.config_version == 2 and cfg.line.a.x == 0.7
    assert client.reported_docs(CAM0)[-1]["config_version"] == 2
    store.close()


def test_conflict_greater_version_wins(tmp_path) -> None:
    """Arbitraje: desired <= actual se IGNORA y re-reporta; desired > actual gana."""
    store = _open(tmp_path)
    # Local ya en v4 (p.ej. la UI local subió la versión).
    store.set_line_config(CAM0, _local_config(CAM0, 0.5, 0.0, 0.5, 1.0), expected_version=0)
    for _ in range(3):
        cur = store.get_config_version(CAM0)
        store.set_line_config(CAM0, _local_config(CAM0, 0.5, 0.0, 0.5, 1.0), expected_version=cur)
    assert store.get_config_version(CAM0) == 4

    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()

    # desired v3 (< 4): se ignora, NO toca SQLite, re-reporta v4.
    out = rec.reconcile_desired(CAM0, _desired(CAM0, config_version=3, ax=0.1))
    assert out.ignored_stale and not out.applied
    assert store.get_config_version(CAM0) == 4
    assert store.get_line_config(CAM0).line.a.x == 0.5  # geometría intacta
    assert client.reported_docs(CAM0)[-1]["config_version"] == 4

    # desired v9 (> 4): gana, aplica con esa versión VERBATIM.
    out2 = rec.reconcile_desired(CAM0, _desired(CAM0, config_version=9, ax=0.9))
    assert out2.applied and out2.new_version == 9
    assert store.get_config_version(CAM0) == 9
    assert store.get_line_config(CAM0).line.a.x == 0.9
    store.close()


def test_equal_version_is_ignored(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()
    rec.reconcile_desired(CAM0, _desired(CAM0, config_version=2, ax=0.7))
    out = rec.reconcile_desired(CAM0, _desired(CAM0, config_version=2, ax=0.1))
    assert out.ignored_stale and not out.applied
    assert store.get_line_config(CAM0).line.a.x == 0.7  # no cambió
    store.close()


# --------------------------------------------------------------------------- #
# Multi-cámara
# --------------------------------------------------------------------------- #


def test_multi_camera_routes_to_correct_camera(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, cameras=(CAM0, CAM1))
    rec.connect()
    client.feed(
        named_shadow_topic(THING, line_config_shadow_name(CAM1), "update/delta"),
        json.dumps({"state": _desired(CAM1, config_version=2, ax=0.8)}).encode(),
    )
    assert store.get_line_config(CAM1) is not None
    assert store.get_line_config(CAM1).line.a.x == 0.8
    assert store.get_line_config(CAM0) is None  # CAM0 intacta
    store.close()


def test_camera_id_mismatch_rejected(tmp_path) -> None:
    """Un desired cuyo camera_id no casa el shadow se rechaza (anti cross-camera)."""
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, cameras=(CAM0, CAM1))
    rec.connect()
    # desired para CAM1 llega por el shadow de CAM0: rechazo, no aplica.
    out = rec.reconcile_desired(CAM0, _desired(CAM1, config_version=5))
    assert out.rejected_camera and not out.applied
    assert store.get_line_config(CAM0) is None
    store.close()


def test_unmanaged_shadow_is_ignored(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, cameras=(CAM0,))
    rec.connect()
    label = rec.dispatch(
        named_shadow_topic(THING, line_config_shadow_name(CAM1), "update/delta"),
        json.dumps({"state": _desired(CAM1, config_version=2)}).encode(),
    )
    assert label == "ignored"
    assert store.get_line_config(CAM1) is None
    store.close()


# --------------------------------------------------------------------------- #
# Contrato fail-closed
# --------------------------------------------------------------------------- #


def test_invalid_desired_rejected_fail_closed(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()
    bad = _desired(CAM0, config_version=5)
    bad["min_confidence"] = 0.3  # campo fuera de contrato
    out = rec.reconcile_desired(CAM0, bad)
    assert out.rejected_contract and not out.applied
    assert store.get_line_config(CAM0) is None  # SQLite intacto
    store.close()


# --------------------------------------------------------------------------- #
# Integración con ConfigWatcher (hot-reload sin reiniciar)
# --------------------------------------------------------------------------- #


def test_config_watcher_hot_reloads_after_remote_apply(tmp_path) -> None:
    """El delta nube -> SQLite hace que el ConfigWatcher recargue EN CALIENTE."""
    store = _open(tmp_path)
    store.set_line_config(CAM0, _local_config(CAM0, 0.5, 0.0, 0.5, 1.0), expected_version=0)
    cfg = store.get_line_config(CAM0)
    lc = LineCounter.from_config(store, cfg, min_frames=1)
    watcher = ConfigWatcher(store, lc, CAM0)
    assert lc.line_version == 1

    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()
    # La nube mueve la línea a x=0.95 con versión mayor.
    client.feed(
        named_shadow_topic(THING, line_config_shadow_name(CAM0), "update/delta"),
        json.dumps({"state": _desired(CAM0, config_version=7, ax=0.95, bx=0.95)}).encode(),
    )
    # El detector (vía ConfigWatcher por-frame) detecta el bump y recarga en caliente.
    assert watcher.poll() is True
    assert watcher.version == 7
    assert lc.line_version == 7
    assert lc.a == (0.95, 0.0) and lc.b == (0.95, 1.0)
    store.close()


# --------------------------------------------------------------------------- #
# UI local -> reported (tick)
# --------------------------------------------------------------------------- #


def test_local_ui_edit_reflected_as_reported_via_tick(tmp_path) -> None:
    """Editar el SQLite desde la UI local se refleja como ``reported`` en el tick."""
    store = _open(tmp_path)
    store.set_line_config(CAM0, _local_config(CAM0, 0.5, 0.0, 0.5, 1.0), expected_version=0)
    client = FakeMqttClient()
    rec = _reconciler(store, client)
    rec.connect()

    # Primer tick: reporta v1.
    assert rec.tick() == [CAM0]
    assert client.reported_docs(CAM0)[-1]["config_version"] == 1
    # Sin cambios, el tick NO re-reporta.
    assert rec.tick() == []

    # La UI local edita la línea (bumpea a v2) — SQLite es el único punto de aplicación.
    store.set_line_config(CAM0, _local_config(CAM0, 0.2, 0.0, 0.2, 1.0), expected_version=1)
    assert rec.tick() == [CAM0]
    last = client.reported_docs(CAM0)[-1]
    assert last["config_version"] == 2 and last["line"]["a"]["x"] == 0.2
    store.close()


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def test_command_request_acks_on_fire_and_forget(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    ran: list[dict] = []
    handler = CommandHandler({"snapshot": lambda a: ran.append(a) or {"ok": True}})
    rec = _reconciler(store, client, command_handler=handler)
    rec.connect()

    client.feed(
        command_request_topic(DEVICE),
        json.dumps({"command_id": "c1", "action": "snapshot"}).encode(),
    )
    acks = client.acks(DEVICE)
    assert acks and acks[-1]["status"] == "ok" and acks[-1]["command_id"] == "c1"
    assert len(ran) == 1

    # Reentrega idempotente: ack 'duplicate', NO re-ejecuta.
    client.feed(
        command_request_topic(DEVICE),
        json.dumps({"command_id": "c1", "action": "snapshot"}).encode(),
    )
    assert client.acks(DEVICE)[-1]["status"] == "duplicate"
    assert len(ran) == 1
    store.close()


def test_command_shadow_delta_executes_and_reports(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    ran: list[dict] = []
    handler = CommandHandler({"reload-config": lambda a: ran.append(a) or {"done": 1}})
    rec = _reconciler(store, client, command_handler=handler)
    rec.connect()

    client.feed(
        named_shadow_topic(THING, "command", "update/delta"),
        json.dumps({"state": {"command_id": "k1", "action": "reload-config"}}).encode(),
    )
    assert len(ran) == 1
    # Se reporta el ack en el shadow command.
    update = named_shadow_topic(THING, "command", "update")
    reported = [json.loads(p)["state"]["reported"] for t, p, *_ in client.published if t == update]
    assert reported and reported[-1]["command_id"] == "k1" and reported[-1]["status"] == "ok"
    store.close()


def test_command_shadow_boot_does_not_reexecute_reported(tmp_path) -> None:
    """Boot del shadow command: un comando ya reportado no se re-ejecuta tras restart."""
    store = _open(tmp_path)
    client = FakeMqttClient()
    ran: list[dict] = []
    handler = CommandHandler({"restart": lambda a: ran.append(a) or {}})
    rec = _reconciler(store, client, command_handler=handler)
    rec.connect()

    doc = {
        "state": {
            "desired": {"command_id": "boot-1", "action": "restart"},
            "reported": {"command_id": "boot-1", "status": "ok"},
        }
    }
    client.feed(named_shadow_topic(THING, "command", "get/accepted"), json.dumps(doc).encode())
    assert ran == []  # ya estaba reportado: NO se re-ejecuta (idempotente)
    store.close()


def test_command_without_handler_rejected(tmp_path) -> None:
    store = _open(tmp_path)
    client = FakeMqttClient()
    rec = _reconciler(store, client, command_handler=None)
    rec.connect()
    client.feed(
        command_request_topic(DEVICE),
        json.dumps({"command_id": "c1", "action": "snapshot"}).encode(),
    )
    assert client.acks(DEVICE)[-1]["status"] == "rejected"
    store.close()
