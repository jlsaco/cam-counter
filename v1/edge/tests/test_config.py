"""Tests de hot-reload de la config de línea (``ConfigWatcher`` + ``store``).

Cubren en x86 sin hardware (SQLite WAL en ``tmp_path``, geometría sintética):
- ``set_line_config`` INCREMENTA ``config_version`` monótonamente,
- concurrencia OPTIMISTA: un ``expected_version`` stale es RECHAZADO
  (``StaleConfigVersionError``), sin tocar la DB,
- ``ConfigWatcher.poll()`` recarga la geometría del ``LineCounter`` EN CALIENTE
  (sin reiniciar el proceso): el conteo CAMBIA tras mover la línea.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from cam_counter_edge.config import ConfigWatcher
from cam_counter_edge.line_counter import LineCounter
from cam_counter_edge.store import StaleConfigVersionError, Store
from cam_counter_edge.types import Line, LineConfig, Point

SITE = "site-a"
DEVICE = "pi-001"
CAMERA = "pi-001-cam0"


def _config(ax: float, ay: float, bx: float, by: float, *, positive_side: int = 1,
            config_version: int = 1) -> LineConfig:
    """``LineConfig`` con una línea de extremos normalizados ``A``/``B``."""
    return LineConfig(
        site_id=SITE,
        device_id=DEVICE,
        camera_id=CAMERA,
        config_version=config_version,
        line=Line(a=Point(ax, ay), b=Point(bx, by)),
        positive_side=positive_side,
        positive_label="subieron",
        negative_label="bajaron",
    )


@dataclass
class _Track:
    """Track mínimo (duck-typing): ``track_id`` + ``centroid``."""

    track_id: object
    centroid: tuple[float, float]
    score: float = 0.9


def _feed(lc: LineCounter, xs: list[float], *, track_id: object = 1, y: float = 0.5,
          t0: int = 1_700_000_000_000, dt: int = 100) -> list:
    """Alimenta posiciones x de UN track, frame a frame; devuelve eventos."""
    events: list = []
    for k, x in enumerate(xs):
        track = _Track(track_id=track_id, centroid=(x, y))
        events.extend(lc.process([track], ts_event_ms=t0 + k * dt))
    return events


def _open(tmp_path) -> Store:
    return Store(str(tmp_path / "events.db"))


# -- migración / esquema ---------------------------------------------------


def test_migration_adds_config_and_uploads_tables(tmp_path) -> None:
    """La migración v2 añade ``camera_config`` y ``clip_uploads`` sin romper v1."""
    store = _open(tmp_path)
    assert store.user_version == 2
    names = {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"events", "counters", "crossing_seq", "camera_config", "clip_uploads"} <= names
    store.close()


def test_migration_v1_to_v2_is_additive_and_preserves_data(tmp_path) -> None:
    """Abrir una DB v1 (PR07) con el Store v2 migra ADITIVAMENTE sin perder datos."""
    path = str(tmp_path / "events.db")
    # Simula una DB de PR07 (v1): tablas v1 con datos y user_version=1.
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, camera_id TEXT, ts_event_ms INTEGER);"
        "CREATE INDEX idx_events_camera_ts ON events (camera_id, ts_event_ms DESC);"
        "CREATE TABLE counters (camera_id TEXT, day_utc TEXT, direction TEXT, count INTEGER);"
        "CREATE TABLE crossing_seq (camera_id TEXT PRIMARY KEY, seq INTEGER);"
        "INSERT INTO crossing_seq (camera_id, seq) VALUES ('pi-001-cam0', 5);"
        "PRAGMA user_version=1;"
    )
    conn.commit()
    conn.close()

    store = Store(path)  # dispara migrate(): v1 -> v2
    assert store.user_version == 2
    # Las tablas v2 nuevas existen y los datos v1 sobreviven (seq 5 -> 6).
    assert store.next_crossing_seq("pi-001-cam0") == 6
    store.set_line_config(CAMERA, _config(0.5, 0.0, 0.5, 1.0), expected_version=0)
    assert store.get_config_version(CAMERA) == 1
    store.close()


# -- config_version monótono + concurrencia optimista ----------------------


def test_set_line_config_increments_config_version(tmp_path) -> None:
    """``set_line_config`` arranca en 1 y luego incrementa monótonamente."""
    store = _open(tmp_path)
    assert store.get_config_version(CAMERA) == 0  # sin config aún
    v1 = store.set_line_config(CAMERA, _config(0.5, 0.0, 0.5, 1.0), expected_version=0)
    assert v1 == 1
    assert store.get_config_version(CAMERA) == 1
    v2 = store.set_line_config(CAMERA, _config(0.4, 0.0, 0.4, 1.0), expected_version=1)
    assert v2 == 2
    assert store.get_config_version(CAMERA) == 2
    cfg = store.get_line_config(CAMERA)
    assert cfg is not None
    assert cfg.config_version == 2
    assert (cfg.line.a.x, cfg.line.a.y) == (0.4, 0.0)
    store.close()


def test_set_line_config_rejects_stale_version(tmp_path) -> None:
    """Un ``expected_version`` desactualizado es RECHAZADO (CAS) y no muta la DB."""
    store = _open(tmp_path)
    store.set_line_config(CAMERA, _config(0.5, 0.0, 0.5, 1.0), expected_version=0)  # -> 1
    # Otro escritor cree que la versión sigue siendo 0: stale -> rechazo.
    with pytest.raises(StaleConfigVersionError) as exc:
        store.set_line_config(CAMERA, _config(0.1, 0.0, 0.1, 1.0), expected_version=0)
    assert exc.value.current == 1 and exc.value.expected == 0
    # La config NO cambió (sigue en v1 con la geometría original).
    assert store.get_config_version(CAMERA) == 1
    cfg = store.get_line_config(CAMERA)
    assert cfg is not None and (cfg.line.a.x, cfg.line.a.y) == (0.5, 0.0)
    store.close()


def test_get_line_config_missing_returns_none(tmp_path) -> None:
    """Sin fila de config, ``get_line_config`` devuelve ``None`` y versión 0."""
    store = _open(tmp_path)
    assert store.get_line_config("pi-001-cam9") is None
    assert store.get_config_version("pi-001-cam9") == 0
    store.close()


# -- hot-reload: el conteo cambia al mover la línea EN CALIENTE -------------


def test_config_watcher_hot_reload_changes_count(tmp_path) -> None:
    """``ConfigWatcher`` recarga la geometría en caliente: el conteo cambia.

    Línea VIEJA vertical en x=0.5; el movimiento 0.2 -> 0.8 la cruza (1 evento).
    Tras mover la línea EN CALIENTE a x=0.95 (vía ``set_line_config`` +
    ``poll()``), el MISMO movimiento ya no la cruza (0 eventos). Sin reiniciar el
    proceso ni reconstruir el ``LineCounter``.
    """
    store = _open(tmp_path)
    store.set_line_config(CAMERA, _config(0.5, 0.0, 0.5, 1.0), expected_version=0)  # v1
    cfg = store.get_line_config(CAMERA)
    assert cfg is not None and cfg.config_version == 1

    lc = LineCounter.from_config(store, cfg, min_frames=1)
    watcher = ConfigWatcher(store, lc, CAMERA)
    assert watcher.version == 1
    assert lc.line_version == 1

    # Línea vieja (x=0.5): 0.2 -> 0.8 la cruza -> 1 evento.
    old = _feed(lc, [0.2, 0.8], track_id=1, t0=1_700_000_000_000)
    assert len(old) == 1

    # Sin cambios, poll() es barato y NO recarga.
    assert watcher.poll() is False

    # Mueve la línea EN CALIENTE a x=0.95 (concurrencia optimista, v1 -> v2).
    new_version = store.set_line_config(
        CAMERA, _config(0.95, 0.0, 0.95, 1.0), expected_version=1
    )
    assert new_version == 2

    # poll() detecta el bump y reconfigura el LineCounter sin reiniciar.
    assert watcher.poll() is True
    assert watcher.version == 2
    assert lc.line_version == 2
    assert lc.a == (0.95, 0.0) and lc.b == (0.95, 1.0)

    # MISMO movimiento 0.2 -> 0.8: con la línea en x=0.95 NO hay cruce -> 0 eventos.
    new = _feed(lc, [0.2, 0.8], track_id=2, t0=1_700_000_002_000)
    assert len(new) == 0
    assert len(new) != len(old)  # el conteo CAMBIÓ por el hot-reload
    store.close()


def test_config_watcher_poll_no_change_is_false(tmp_path) -> None:
    """``poll()`` sin cambios devuelve False y no toca la geometría."""
    store = _open(tmp_path)
    store.set_line_config(CAMERA, _config(0.5, 0.0, 0.5, 1.0), expected_version=0)
    cfg = store.get_line_config(CAMERA)
    assert cfg is not None
    lc = LineCounter.from_config(store, cfg, min_frames=1)
    watcher = ConfigWatcher(store, lc, CAMERA)
    for _ in range(5):
        assert watcher.poll() is False
    assert lc.line_version == 1
    store.close()
