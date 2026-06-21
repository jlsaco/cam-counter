"""Tests de la etapa ``sink``: repositorio SQLite (``store.Store``).

Cubren en x86 sin hardware (DB SQLite temporal en ``tmp_path``, WAL real):
- migración idempotente vía ``PRAGMA user_version``,
- ``insert_event`` idempotente / dedupe por ``event_id`` (segunda inserción del
  mismo evento no duplica ni lanza),
- ``crossing_seq`` monótono persistido POR CÁMARA (nunca reiniciado),
- ``record_event`` que sólo bumpea el contador cuando el evento es nuevo,
- ``bump_counter`` upsert y lecturas (``get_counters``, ``get_recent_events``).
"""

from __future__ import annotations

import sqlite3

from cam_counter_edge.store import SCHEMA_USER_VERSION, Store
from cam_counter_edge.types import CrossingEvent


def _event(event_id: str, *, camera_id: str = "pi-001-cam0", ts_event_ms: int = 1_700_000_000_000,
           direction: str = "in", crossing_seq: int = 1, track_id: str = "1") -> CrossingEvent:
    """``CrossingEvent`` mínimo y válido para los tests del store."""
    return CrossingEvent(
        event_id=event_id,
        site_id="site-a",
        device_id="pi-001",
        camera_id=camera_id,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction=direction,
        ts_event_ms=ts_event_ms,
        ts_event_iso="2023-11-14T22:13:20.000Z",
        line_version=1,
        confidence=0.9,
        clip_key=None,
        clip_status="pending",
        synced=0,
        created_at="2023-11-14T22:13:20.000Z",
        schema_version=1,
    )


def _open(tmp_path) -> Store:
    return Store(str(tmp_path / "events.db"))


def test_store_uses_wal(tmp_path) -> None:
    """El store abre la DB en modo WAL."""
    store = _open(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    store.close()
    assert mode.lower() == "wal"


def test_migrate_is_idempotent(tmp_path) -> None:
    """Re-ejecutar ``migrate()`` no recrea/duplica y deja el ``user_version`` fijo."""
    store = _open(tmp_path)
    assert store.user_version == SCHEMA_USER_VERSION
    # Insertamos algo y re-migramos varias veces: ni se borra ni se duplica nada.
    assert store.insert_event(_event("a" * 40)) is True
    for _ in range(3):
        store.migrate()
    assert store.user_version == SCHEMA_USER_VERSION
    # Las tablas esperadas existen exactamente una vez.
    names = {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"events", "counters", "crossing_seq"} <= names
    assert len(store.get_recent_events("pi-001-cam0")) == 1
    store.close()


def test_insert_event_is_idempotent_dedup(tmp_path) -> None:
    """Insertar el MISMO ``event_id`` dos veces deja UN solo registro (dedupe)."""
    store = _open(tmp_path)
    ev = _event("b" * 40)
    assert store.insert_event(ev) is True   # nuevo
    assert store.insert_event(ev) is False  # duplicado: ni lanza ni duplica
    rows = store.get_recent_events("pi-001-cam0")
    assert len(rows) == 1
    assert rows[0]["event_id"] == "b" * 40
    store.close()


def test_next_crossing_seq_is_monotonic_per_camera(tmp_path) -> None:
    """``crossing_seq`` arranca en 1, es monótono y es INDEPENDIENTE por cámara."""
    store = _open(tmp_path)
    assert [store.next_crossing_seq("pi-001-cam0") for _ in range(3)] == [1, 2, 3]
    # Otra cámara lleva su propia secuencia desde 1 (no comparte contador).
    assert store.next_crossing_seq("pi-001-cam1") == 1
    assert store.next_crossing_seq("pi-001-cam0") == 4
    store.close()


def test_crossing_seq_is_persistent_and_never_resets(tmp_path) -> None:
    """El contador persiste tras reabrir la DB (no se reinicia entre arranques)."""
    path = str(tmp_path / "events.db")
    store = Store(path)
    assert store.next_crossing_seq("pi-001-cam0") == 1
    assert store.next_crossing_seq("pi-001-cam0") == 2
    store.close()
    # Reabrimos: el contador continúa donde estaba, nunca vuelve a 1.
    store2 = Store(path)
    assert store2.next_crossing_seq("pi-001-cam0") == 3
    store2.close()


def test_record_event_counter_idempotent(tmp_path) -> None:
    """``record_event`` bumpea el contador sólo en eventos nuevos (idempotente)."""
    store = _open(tmp_path)
    ev = _event("c" * 40, direction="in")
    assert store.record_event(ev) is True   # nuevo -> contador sube
    assert store.record_event(ev) is False  # duplicado -> contador NO sube
    counters = store.get_counters("pi-001-cam0", "2023-11-14")
    assert counters == [
        {"camera_id": "pi-001-cam0", "day_utc": "2023-11-14", "direction": "in", "count": 1}
    ]
    store.close()


def test_bump_counter_upsert(tmp_path) -> None:
    """``bump_counter`` hace upsert acumulando por ``(camera, day, direction)``."""
    store = _open(tmp_path)
    store.bump_counter("pi-001-cam0", "2026-06-20", "in", 1)
    store.bump_counter("pi-001-cam0", "2026-06-20", "in", 2)
    store.bump_counter("pi-001-cam0", "2026-06-20", "out", 1)
    rows = {(r["direction"]): r["count"] for r in store.get_counters("pi-001-cam0", "2026-06-20")}
    assert rows == {"in": 3, "out": 1}
    store.close()


def test_get_recent_events_order(tmp_path) -> None:
    """``get_recent_events`` ordena por ``ts_event_ms`` descendente."""
    store = _open(tmp_path)
    store.insert_event(_event("1" * 40, ts_event_ms=1000, crossing_seq=1))
    store.insert_event(_event("3" * 40, ts_event_ms=3000, crossing_seq=3))
    store.insert_event(_event("2" * 40, ts_event_ms=2000, crossing_seq=2))
    ts = [r["ts_event_ms"] for r in store.get_recent_events("pi-001-cam0")]
    assert ts == [3000, 2000, 1000]
    # El límite recorta a los más recientes.
    top2 = [r["ts_event_ms"] for r in store.get_recent_events("pi-001-cam0", limit=2)]
    assert top2 == [3000, 2000]
    store.close()


def test_events_table_unique_event_id_constraint(tmp_path) -> None:
    """La columna ``event_id`` tiene restricción UNIQUE a nivel de esquema."""
    store = _open(tmp_path)
    store.insert_event(_event("d" * 40))
    # Inserción cruda saltándose la capa idempotente: debe violar el UNIQUE.
    raised = False
    try:
        store._conn.execute(
            "INSERT INTO events (event_id, site_id, device_id, camera_id, track_id, "
            "crossing_seq, direction, ts_event_ms, ts_event_iso, schema_version) "
            "VALUES (?, 'site-a', 'pi-001', 'pi-001-cam0', '1', 1, 'in', 1, 'x', 1)",
            ("d" * 40,),
        )
    except sqlite3.IntegrityError:
        raised = True
    store.close()
    assert raised
