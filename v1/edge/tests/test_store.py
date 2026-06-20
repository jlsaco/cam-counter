"""Contrato de ``store.py``: SQLite WAL, migración idempotente, ``crossing_seq`` monótono,
``insert_event`` idempotente y contadores transaccionales.

Todo corre en x86 sin hardware sobre una DB real en ``tmp_path`` (no ``:memory:``, que no
comparte estado entre conexiones y no ejercitaría WAL). Sin dependencia del reloj real:
``ts_event_ms``/``ts_event_iso`` se pasan explícitos y deterministas.
"""

from __future__ import annotations

import sqlite3

import pytest

from cam_counter_edge.identifiers import InvalidSlugError
from cam_counter_edge.store import SCHEMA_USER_VERSION, Store
from cam_counter_edge.types import CrossingEvent

SITE = "site-a"
DEVICE = "pi-001"
CAM = "pi-001-cam0"
CAM2 = "pi-001-cam1"


def _event(
    *,
    track_id: str,
    crossing_seq: int,
    direction: str = "in",
    camera_id: str = CAM,
    ts_event_ms: int = 1_700_000_000_000,
) -> CrossingEvent:
    """CrossingEvent determinista con ``event_id`` ya derivado del ``crossing_seq``."""
    event_id = f"evt-{camera_id}-{track_id}-{crossing_seq}"  # id explícito y estable
    ts_iso = "2023-11-14T22:13:20Z"
    return CrossingEvent(
        event_id=event_id,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=camera_id,
        track_id=str(track_id),
        crossing_seq=crossing_seq,
        direction=direction,
        label="subieron" if direction == "in" else "bajaron",
        line_version=1,
        ts_event_ms=ts_event_ms,
        ts_event_iso=ts_iso,
        confidence=0.9,
    )


def _open(tmp_path) -> Store:
    return Store(str(tmp_path / "counter.db"))


# ─────────────────────────────── migración / WAL ───────────────────────────────
def test_wal_mode_and_user_version(tmp_path):
    """La base abre en modo WAL y queda en la ``user_version`` del esquema actual."""
    store = _open(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_USER_VERSION
    store.close()


def test_migrate_idempotent(tmp_path):
    """Re-ejecutar ``migrate()`` no duplica tablas ni datos ni cambia ``user_version``.

    Inserta un evento, vuelve a migrar varias veces y comprueba que la fila sigue siendo
    única, que las tablas no se recrean y que el esquema permanece estable.
    """
    store = _open(tmp_path)
    assert store.insert_event(_event(track_id="1", crossing_seq=1)) is True

    def table_names() -> set[str]:
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}

    before = table_names()
    for _ in range(3):
        store.migrate()  # idempotente: no debe lanzar ni recrear nada
    after = table_names()

    assert before == after
    assert {"events", "counters", "crossing_seq"} <= after
    assert store.count_events() == 1  # el evento no se duplicó al re-migrar
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_USER_VERSION
    store.close()


def test_migrate_idempotent_across_reopen(tmp_path):
    """Reabrir la base (que vuelve a llamar a ``migrate()``) preserva los datos."""
    db = str(tmp_path / "counter.db")
    s1 = Store(db)
    s1.insert_event(_event(track_id="1", crossing_seq=1))
    s1.close()

    s2 = Store(db)  # __init__ vuelve a migrar; debe ser idempotente
    assert s2.count_events() == 1
    assert s2.get_event("evt-%s-1-1" % CAM) is not None
    s2.close()


# ─────────────────────────────── crossing_seq monótono ─────────────────────────
def test_next_crossing_seq_is_monotonic_per_camera(tmp_path):
    """``next_crossing_seq`` arranca en 1 e incrementa de uno en uno, por cámara."""
    store = _open(tmp_path)
    assert [store.next_crossing_seq(CAM) for _ in range(4)] == [1, 2, 3, 4]
    # Otra cámara lleva su propio contador independiente.
    assert store.next_crossing_seq(CAM2) == 1
    assert store.next_crossing_seq(CAM) == 5  # la primera cámara no se ve afectada
    assert store.peek_crossing_seq(CAM) == 5
    assert store.peek_crossing_seq(CAM2) == 1
    store.close()


def test_next_crossing_seq_persists_and_never_resets(tmp_path):
    """El contador es PERSISTIDO y NUNCA se reinicia al reabrir la base."""
    db = str(tmp_path / "counter.db")
    s1 = Store(db)
    assert s1.next_crossing_seq(CAM) == 1
    assert s1.next_crossing_seq(CAM) == 2
    s1.close()

    s2 = Store(db)  # reabre: el contador continúa donde quedó, no reinicia
    assert s2.peek_crossing_seq(CAM) == 2
    assert s2.next_crossing_seq(CAM) == 3
    s2.close()


def test_next_crossing_seq_validates_slug(tmp_path):
    """``camera_id`` inválido se rechaza ANTES de tocar la fila del contador."""
    store = _open(tmp_path)
    with pytest.raises(InvalidSlugError):
        store.next_crossing_seq("Bad/Camera#id")
    store.close()


# ───────────────────────────── insert_event idempotente ────────────────────────
def test_insert_event_idempotent_returns_true_then_false(tmp_path):
    """Primera inserción True; reintento del MISMO ``event_id`` devuelve False sin lanzar."""
    store = _open(tmp_path)
    ev = _event(track_id="7", crossing_seq=1)
    assert store.insert_event(ev) is True
    assert store.insert_event(ev) is False  # idempotente: no lanza, no inserta
    store.close()


def test_insert_event_dedup_keeps_single_row(tmp_path):
    """Insertar dos veces el mismo evento deja UN solo registro (dedupe por event_id)."""
    store = _open(tmp_path)
    ev = _event(track_id="7", crossing_seq=1)
    store.insert_event(ev)
    store.insert_event(ev)
    store.insert_event(ev)
    assert store.count_events(CAM) == 1
    assert len(store.get_recent_events(CAM)) == 1
    store.close()


def test_insert_event_accepts_dict_and_dedups(tmp_path):
    """``insert_event`` acepta un dict equivalente y aplica el mismo dedupe por event_id."""
    store = _open(tmp_path)
    ev = _event(track_id="7", crossing_seq=1)
    assert store.insert_event(ev) is True
    as_dict = {f: getattr(ev, f) for f in CrossingEvent.__dataclass_fields__}
    assert store.insert_event(as_dict) is False  # mismo event_id -> dedup
    assert store.count_events() == 1
    store.close()


def test_get_recent_events_ordered_by_ts_desc(tmp_path):
    """``get_recent_events`` devuelve los eventos por ``ts_event_ms`` descendente."""
    store = _open(tmp_path)
    store.insert_event(_event(track_id="1", crossing_seq=1, ts_event_ms=1000))
    store.insert_event(_event(track_id="2", crossing_seq=2, ts_event_ms=3000))
    store.insert_event(_event(track_id="3", crossing_seq=3, ts_event_ms=2000))
    rows = store.get_recent_events(CAM, limit=10)
    assert [r["ts_event_ms"] for r in rows] == [3000, 2000, 1000]
    assert store.get_recent_events(CAM, limit=2)[0]["ts_event_ms"] == 3000
    assert len(store.get_recent_events(CAM, limit=2)) == 2
    store.close()


# ───────────────────────────────── contadores ──────────────────────────────────
def test_bump_counter_upsert_transactional(tmp_path):
    """``bump_counter`` crea e incrementa el contador por (camera, day, direction)."""
    store = _open(tmp_path)
    assert store.bump_counter(CAM, "2023-11-14", "in") == 1
    assert store.bump_counter(CAM, "2023-11-14", "in") == 2
    assert store.bump_counter(CAM, "2023-11-14", "out") == 1
    assert store.bump_counter(CAM, "2023-11-15", "in", delta=5) == 5
    rows = store.get_counters(CAM, "2023-11-14")
    by_dir = {r["direction"]: r["count"] for r in rows}
    assert by_dir == {"in": 2, "out": 1}
    # Otra cámara no comparte contador.
    assert store.bump_counter(CAM2, "2023-11-14", "in") == 1
    store.close()


def test_bump_counter_rejects_bad_direction(tmp_path):
    """Una ``direction`` fuera de {'in','out'} se rechaza."""
    store = _open(tmp_path)
    with pytest.raises(ValueError):
        store.bump_counter(CAM, "2023-11-14", "up")
    store.close()


def test_record_event_idempotent_does_not_inflate_counter(tmp_path):
    """``record_event`` cuenta SÓLO cuando el evento es nuevo (no infla en reintentos)."""
    store = _open(tmp_path)
    ev = _event(track_id="7", crossing_seq=1, direction="in")
    assert store.record_event(ev) is True  # nuevo: inserta y suma 1
    assert store.record_event(ev) is False  # reintento: ni duplica ni vuelve a sumar
    assert store.record_event(ev) is False
    counters = store.get_counters(CAM, "2023-11-14")
    assert {c["direction"]: c["count"] for c in counters} == {"in": 1}
    assert store.count_events(CAM) == 1
    store.close()


def test_events_table_has_no_presentation_label_columns(tmp_path):
    """Las etiquetas de presentación NO se materializan como columnas (sólo ``label``)."""
    store = _open(tmp_path)
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "label" in cols
    assert "positive_label" not in cols
    assert "negative_label" not in cols
    store.close()


def test_unique_event_id_constraint_exists(tmp_path):
    """La restricción ``UNIQUE(event_id)`` está activa a nivel de esquema."""
    store = _open(tmp_path)
    store.insert_event(_event(track_id="7", crossing_seq=1))
    # Inserción cruda saltándose insert_event: debe violar UNIQUE(event_id).
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO events (event_id, site_id, device_id, camera_id, track_id, "
            "crossing_seq, direction, line_version, ts_event_ms, ts_event_iso, "
            "schema_version, created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evt-%s-7-1" % CAM,
                SITE,
                DEVICE,
                CAM,
                "7",
                1,
                "in",
                1,
                1_700_000_000_000,
                "2023-11-14T22:13:20Z",
                1,
                "2023-11-14T22:13:20Z",
            ),
        )
    store.close()
