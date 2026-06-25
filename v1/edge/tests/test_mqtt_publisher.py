"""Tests del publicador MQTT (modo ``iot``) con un cliente MQTT FAKE (sin broker/red).

Cubre los criterios de aceptación del WP14:
- ``client_id == thing_name``; ``clean_session`` lo fija el factory (paho real).
- topic DERIVA del ``device_id`` (mismo canon que la device-policy de WP06); thing y
  topic que no casen -> ABORTA (fail-closed).
- drenado QoS1 ACK-DRIVEN: ``synced=1`` SÓLO tras el PUBACK (``on_publish``), nunca al
  publicar; si el device cae entre publish y PUBACK el evento sigue ``synced=0``.
- offline: si el cliente rechaza el publish (rc != 0) se DETIENE el batch sin perder
  eventos; al reconectar el backlog drena sin duplicar.
- idempotencia: un evento ya ``synced=1`` no se re-publica; el mismo ``event_id`` no
  duplica (mapeo determinista).
- fail-closed: un payload que no casa el contrato (anti-spoof) NO se publica.
- LWT (will) offline retained + status online al conectar.
- clips: la subida usa el provider de credenciales del IoT Credential Provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cam_counter_edge import compute_event_id, media_clip_key, ms_to_iso_utc
from cam_counter_edge.mqtt_publisher import (
    MqttPublisher,
    derive_topic_prefix,
    validate_thing_topic,
)
from cam_counter_edge.store import Store
from cam_counter_edge.sync import AwsClients
from cam_counter_edge.types import CrossingEvent

SITE = "demo-site"
DEVICE = "demo-pi"
CAMERA = "demo-pi-cam0"
THING = "cam-counter-demo-site-demo-pi"  # cam-counter-{site}-{device}
MEDIA_BUCKET = "cam-counter-media-950639281773"


# --------------------------------------------------------------------------- #
# Cliente MQTT fake (paho-compatible): publish() devuelve rc/mid; los PUBACK se
# entregan manualmente con flush_acks() para ejercer el drenado ack-driven.
# --------------------------------------------------------------------------- #


class _FakeMsgInfo:
    def __init__(self, rc: int, mid: int) -> None:
        self.rc = rc
        self.mid = mid


class _FakeMqttClient:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.on_connect = None
        self.on_disconnect = None
        self.on_publish = None
        self.tls: dict | None = None
        self.will: tuple | None = None
        self.connected = False
        self.loop_running = False
        self.published: list[tuple[str, bytes, int, bool, int]] = []
        self._next_mid = 0
        self._pending_acks: list[int] = []
        self.fail_publish = False  # simula offline: rc != 0, no encola

    def tls_set(self, **kwargs: object) -> None:
        self.tls = dict(kwargs)

    def will_set(self, topic: str, payload: object, qos: int, retain: bool) -> None:
        self.will = (topic, payload, qos, retain)

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.connected = True

    def loop_start(self) -> None:
        self.loop_running = True

    def loop_stop(self) -> None:
        self.loop_running = False

    def disconnect(self) -> None:
        self.connected = False

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> _FakeMsgInfo:
        self._next_mid += 1
        mid = self._next_mid
        if self.fail_publish:
            return _FakeMsgInfo(rc=1, mid=mid)  # offline: NO encola, NO ack
        self.published.append((topic, payload, qos, retain, mid))
        self._pending_acks.append(mid)
        return _FakeMsgInfo(rc=0, mid=mid)

    def flush_acks(self) -> None:
        """Simula el hilo de red de paho entregando los PUBACK (QoS1)."""
        acks, self._pending_acks = self._pending_acks, []
        for mid in acks:
            if self.on_publish is not None:
                self.on_publish(self, None, mid)

    def event_publishes(self, events_topic: str) -> list[dict]:
        """Payloads decodificados publicados en el topic de eventos."""
        return [
            json.loads(payload)
            for (topic, payload, _qos, _retain, _mid) in self.published
            if topic == events_topic
        ]


def _make_event(track_id: str, crossing_seq: int, ts_ms: int = 1_700_000_000_000):
    event_id = compute_event_id(SITE, DEVICE, CAMERA, track_id, crossing_seq)
    return CrossingEvent(
        event_id=event_id,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=CAMERA,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_ms,
        ts_event_iso=ms_to_iso_utc(ts_ms),
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=1,
        confidence=0.9,
        clip_status="pending",
        synced=0,
        created_at=ms_to_iso_utc(ts_ms),
    )


def _publisher(store: Store, client: _FakeMqttClient, **kwargs: object) -> MqttPublisher:
    return MqttPublisher(
        store,
        thing_name=THING,
        device_id=DEVICE,
        endpoint="example-ats.iot.us-east-1.amazonaws.com",
        client=client,
        media_bucket=MEDIA_BUCKET,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Topics: derivan del device_id; thing/topic incoherentes -> fail-closed
# --------------------------------------------------------------------------- #


def test_topic_derives_from_device_id() -> None:
    assert derive_topic_prefix(DEVICE) == "cam-counter/demo-pi"


def test_validate_thing_topic_ok() -> None:
    assert validate_thing_topic(THING, DEVICE) == "cam-counter/demo-pi"


def test_validate_thing_topic_mismatch_aborts() -> None:
    """Un thing que NO termina en ``-{device_id}`` aborta (la policy denegaría en silencio)."""
    with pytest.raises(ValueError, match="no deriva"):
        validate_thing_topic("cam-counter-other-site-other-pi", DEVICE)


def test_publisher_events_topic(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "edge.db"))
    pub = _publisher(store, _FakeMqttClient(THING))
    assert pub.events_topic == "cam-counter/demo-pi/events/crossing"
    assert pub.status_topic == "cam-counter/demo-pi/status"
    assert pub.telemetry_topic == "cam-counter/demo-pi/telemetry"
    store.close()


def test_publisher_rejects_incoherent_thing(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "edge.db"))
    with pytest.raises(ValueError, match="no deriva"):
        MqttPublisher(
            store,
            thing_name="cam-counter-foo-bar",
            device_id=DEVICE,
            endpoint="x",
            client=_FakeMqttClient("cam-counter-foo-bar"),
        )
    store.close()


# --------------------------------------------------------------------------- #
# client_id == thing_name; connect configura TLS/LWT/callbacks
# --------------------------------------------------------------------------- #


def test_client_id_equals_thing_name(tmp_path: Path) -> None:
    """El factory recibe el thing_name como client_id (ata client-id al Thing, WP06)."""
    store = Store(str(tmp_path / "edge.db"))
    seen: dict[str, str] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        seen["client_id"] = client_id
        return _FakeMqttClient(client_id)

    pub = MqttPublisher(
        store,
        thing_name=THING,
        device_id=DEVICE,
        endpoint="x",
        client_factory=factory,
    )
    pub.publish_status(online=True)  # fuerza la construcción del cliente
    assert seen["client_id"] == THING
    store.close()


def test_connect_sets_lwt_and_callbacks(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    pub = _publisher(
        store, client, cert_path="/dev/null", key_path="/dev/null", ca_path="/dev/null"
    )
    pub.connect()
    # LWT: status offline retained, QoS1.
    assert client.will is not None
    will_topic, will_payload, will_qos, will_retain = client.will
    assert will_topic == pub.status_topic
    assert will_qos == 1 and will_retain is True
    assert json.loads(will_payload)["status"] == "offline"
    # TLS configurado (cert/key/ca) y callbacks enganchados.
    assert client.tls is not None
    assert client.on_publish is not None
    assert client.connected and client.loop_running
    # _on_connect publica status online retained.
    client.on_connect(client, None, None, 0)
    online = [
        json.loads(p)
        for (t, p, _q, retain, _m) in client.published
        if t == pub.status_topic and retain
    ]
    assert any(s["status"] == "online" for s in online)
    store.close()


# --------------------------------------------------------------------------- #
# Drenado ack-driven + idempotencia + offline
# --------------------------------------------------------------------------- #


def test_synced_only_after_puback(tmp_path: Path) -> None:
    """``synced=1`` SÓLO tras el PUBACK; tras publicar (sin ack) sigue ``synced=0``."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    pub = _publisher(store, client)

    store.insert_event(_make_event("t1", 1))
    out = pub.drain_once()
    assert out.published == 1
    assert out.acked == 0  # el PUBACK aún no llegó
    # El evento sigue synced=0 hasta el PUBACK (edge-first: la cola es la verdad).
    assert len(store.get_unsynced_events(10)) == 1

    client.flush_acks()  # llega el PUBACK -> on_publish marca synced=1
    assert store.get_unsynced_events(10) == []
    # El payload publicado es el contrato verbatim.
    payloads = client.event_publishes(pub.events_topic)
    assert len(payloads) == 1
    assert payloads[0]["event_id"] == compute_event_id(SITE, DEVICE, CAMERA, "t1", 1)
    assert "synced" not in payloads[0]
    store.close()


def test_no_duplicate_publish_when_already_synced(tmp_path: Path) -> None:
    """Un evento ya ``synced=1`` no se vuelve a publicar (idempotencia, sin duplicar)."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    pub = _publisher(store, client)

    store.insert_event(_make_event("t1", 1))
    pub.drain_once()
    client.flush_acks()
    # Segunda pasada: no hay pendientes -> no publica de nuevo.
    out2 = pub.drain_once()
    assert out2.published == 0
    assert len(client.event_publishes(pub.events_topic)) == 1
    store.close()


def test_offline_stops_batch_without_loss(tmp_path: Path) -> None:
    """Cliente offline (rc!=0): el batch se detiene; los eventos siguen ``synced=0``."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    client.fail_publish = True
    pub = _publisher(store, client)

    store.insert_event(_make_event("t1", 1))
    store.insert_event(_make_event("t2", 2))
    out = pub.drain_once()
    assert out.stopped_offline is True
    assert out.published == 0
    assert len(store.get_unsynced_events(10)) == 2  # nada perdido

    # Reconecta: drena el backlog y, tras PUBACK, marca synced sin duplicar.
    client.fail_publish = False
    out2 = pub.drain_once()
    assert out2.published == 2
    client.flush_acks()
    assert store.get_unsynced_events(10) == []
    # Cada event_id se publicó UNA vez (determinista, sin duplicar).
    ids = [p["event_id"] for p in client.event_publishes(pub.events_topic)]
    assert sorted(ids) == sorted(set(ids))
    assert len(ids) == 2
    store.close()


def test_contract_fail_closed_event_not_published(tmp_path: Path) -> None:
    """Un evento que no casa el contrato (anti-spoof) NO se publica (fail-closed)."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    pub = _publisher(store, client)

    bad = _make_event("t1", 1)
    bad.event_id = "f" * 40  # patrón válido pero NO deriva de la identidad (spoof)
    store.insert_event(bad)
    out = pub.drain_once()
    assert out.rejected_contract == 1
    assert out.published == 0
    assert client.event_publishes(pub.events_topic) == []
    # Sigue synced=0 (no se pierde; se podrá corregir/reintentar).
    assert len(store.get_unsynced_events(10)) == 1
    store.close()


def test_redelivery_without_ack_keeps_unsynced(tmp_path: Path) -> None:
    """Si el device cae entre publish y PUBACK, el evento se re-publica (sigue synced=0)."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    pub = _publisher(store, client)

    store.insert_event(_make_event("t1", 1))
    pub.drain_once()  # publica, pero NO llega el PUBACK (no flush)
    assert len(store.get_unsynced_events(10)) == 1
    # Reintento: se vuelve a publicar el MISMO event_id (QoS1 at-least-once).
    pub.drain_once()
    client.flush_acks()
    assert store.get_unsynced_events(10) == []
    ids = [p["event_id"] for p in client.event_publishes(pub.events_topic)]
    assert len(ids) == 2 and len(set(ids)) == 1  # mismo event_id 2 veces (dedupe en cloud)
    store.close()


# --------------------------------------------------------------------------- #
# Clips: subida vía provider de credenciales del IoT Credential Provider
# --------------------------------------------------------------------------- #


class _FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0

    def put_object(self, *, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):  # noqa: N803
        self.put_calls += 1
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise _FakeClientError("PreconditionFailed")
        self.objects[(Bucket, Key)] = Body
        return {}


def test_clip_uploaded_via_provider_and_clip_key_in_payload(tmp_path: Path) -> None:
    """El clip se sube por el provider (credenciales temporales) y el payload lleva clip_key."""
    store = Store(str(tmp_path / "edge.db"))
    client = _FakeMqttClient(THING)
    s3 = _FakeS3()
    calls = {"n": 0}

    def provider() -> AwsClients:
        calls["n"] += 1
        return AwsClients(s3=s3, dynamodb=None)  # type: ignore[arg-type]

    pub = _publisher(store, client, clip_clients_provider=provider)

    event = _make_event("t1", 1)
    store.insert_event(event)
    clip = tmp_path / f"{event.event_id}.mp4"
    clip.write_bytes(b"CLIP-BYTES")
    s3_key = media_clip_key(SITE, DEVICE, CAMERA, event.event_id, "mp4", event.ts_event_ms)
    store.enqueue_clip_upload(
        event_id=event.event_id,
        camera_id=CAMERA,
        local_path=str(clip),
        s3_key_planned=s3_key,
    )

    out = pub.drain_once()
    assert out.clips_uploaded == 1
    assert calls["n"] >= 1  # se pidieron credenciales temporales al provider
    assert s3.objects[(MEDIA_BUCKET, s3_key)] == b"CLIP-BYTES"
    payloads = client.event_publishes(pub.events_topic)
    assert payloads[0]["clip_key"] == s3_key
    assert payloads[0]["clip_status"] == "uploaded"
    store.close()
