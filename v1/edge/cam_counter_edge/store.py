"""Etapa ``sink`` del pipeline de borde: repositorio SQLite local.

Persistencia 100% LOCAL y offline-tolerante (edge-first): el Pi cuenta y guarda
aunque no haya internet; la nube sólo recibe sincronización/histórico más tarde.
SQLite es el ÚNICO contrato de persistencia local de este subsistema.

Diseño y garantías (ver CLAUDE.md):

- **WAL + una sola conexión de escritura**: ``PRAGMA journal_mode=WAL`` y
  ``PRAGMA busy_timeout`` para tolerar lectores concurrentes (la API local) sin
  bloquear al proceso de conteo. Transacciones CORTAS con ``BEGIN IMMEDIATE``.
- **Migraciones idempotentes por ``PRAGMA user_version``**: re-ejecutar
  ``migrate()`` no recrea ni duplica nada (todo el DDL es ``IF NOT EXISTS`` y el
  ``user_version`` actúa de guarda explícita).
- **Idempotencia de eventos**: ``event_id`` es DETERMINISTA (sha1 de la tupla de
  identidad, ver ``line_counter``); ``insert_event`` usa ``INSERT OR IGNORE`` con
  ``UNIQUE(event_id)`` para que un reintento del MISMO evento no duplique ni
  lance. Esto es lo que hace IDEMPOTENTE la sincronización edge->cloud.
- **``crossing_seq`` MONÓTONO POR CÁMARA**: contador persistido por cámara que
  NUNCA se reinicia (ni entre arranques ni por track). Cierra el hueco de
  idempotencia: dos cruces distintos jamás colisionan en ``event_id`` aunque
  compartan ``track_id`` (porque cada uno toma un ``crossing_seq`` único).

Multi-cámara: ``crossing_seq`` y ``counters`` están indexados por cámara; una
sola DB soporta varias cámaras del mismo Pi.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from .identifiers import validate_camera_id, validate_device_id, validate_site_id
from .types import CrossingEvent

__all__ = ["SCHEMA_USER_VERSION", "Store"]

# Versión de esquema física de SQLite (PRAGMA user_version). Es independiente de
# CrossingEvent.schema_version (versión del contrato lógico).
SCHEMA_USER_VERSION = 1

# Columnas de ``events`` = campos de CrossingEvent (orden estable para INSERT).
_EVENT_COLUMNS = (
    "event_id",
    "site_id",
    "device_id",
    "camera_id",
    "track_id",
    "crossing_seq",
    "direction",
    "positive_label",
    "negative_label",
    "label",
    "line_version",
    "ts_event_ms",
    "ts_event_iso",
    "confidence",
    "clip_key",
    "clip_status",
    "schema_version",
    "synced",
    "created_at",
)

# DDL idempotente: todo ``IF NOT EXISTS``. Re-ejecutarlo no recrea nada.
_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    track_id        TEXT NOT NULL,
    crossing_seq    INTEGER NOT NULL,
    direction       TEXT NOT NULL,
    positive_label  TEXT,
    negative_label  TEXT,
    label           TEXT,
    line_version    INTEGER,
    ts_event_ms     INTEGER NOT NULL,
    ts_event_iso    TEXT NOT NULL,
    confidence      REAL,
    clip_key        TEXT,
    clip_status     TEXT,
    schema_version  INTEGER NOT NULL,
    synced          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_camera_ts
    ON events (camera_id, ts_event_ms DESC);

CREATE TABLE IF NOT EXISTS counters (
    camera_id  TEXT NOT NULL,
    day_utc    TEXT NOT NULL,
    direction  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera_id, day_utc, direction)
);

CREATE TABLE IF NOT EXISTS crossing_seq (
    camera_id  TEXT PRIMARY KEY,
    seq        INTEGER NOT NULL
);
"""


def _ms_to_day_utc(ts_event_ms: int) -> str:
    """Día UTC ``YYYY-MM-DD`` derivado del epoch ms (determinista, sin reloj)."""
    return datetime.fromtimestamp(ts_event_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d")


class Store:
    """Repositorio SQLite (WAL) del subsistema de conteo en el borde.

    Mantiene UNA conexión de escritura. Pensado para que el proceso de conteo
    escriba y la API local lea (lectores WAL no bloqueantes). Las transacciones
    de escritura son cortas y usan ``BEGIN IMMEDIATE`` para serializar al único
    escritor de forma segura.

    Args:
        path: ruta del fichero SQLite. Para WAL real usa un fichero (``:memory:``
            no comparte estado entre conexiones; sólo útil en tests de una sola
            conexión).
        busy_timeout_ms: milisegundos que SQLite espera ante un lock antes de
            lanzar ``OperationalError`` (``PRAGMA busy_timeout``).
    """

    def __init__(self, path: str, busy_timeout_ms: int = 5000) -> None:
        # isolation_level=None => autocommit: controlamos las transacciones a
        # mano con BEGIN IMMEDIATE/COMMIT (transacciones cortas y explícitas).
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # -- ciclo de vida ----------------------------------------------------

    def close(self) -> None:
        """Cierra la conexión de escritura."""
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Cursor]:
        """Transacción corta y atómica (``BEGIN IMMEDIATE`` .. ``COMMIT``)."""
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # -- migraciones ------------------------------------------------------

    def migrate(self) -> None:
        """Crea/actualiza el esquema. IDEMPOTENTE vía ``PRAGMA user_version``.

        Re-ejecutarla cuando el esquema ya está al día es un no-op; todo el DDL
        es ``IF NOT EXISTS``, así que tampoco recrea ni duplica tablas/datos.
        """
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_USER_VERSION:
            return
        self._conn.executescript(_DDL)
        # PRAGMA no admite parámetros: el valor es una constante interna entera.
        self._conn.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")
        self._conn.commit()

    @property
    def user_version(self) -> int:
        """``PRAGMA user_version`` actual de la base."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- crossing_seq monótono por cámara --------------------------------

    def next_crossing_seq(self, camera_id: str) -> int:
        """Devuelve el siguiente ``crossing_seq`` MONÓTONO de la cámara.

        Crea la fila partiendo de 0 si no existe, incrementa atómicamente y
        devuelve el NUEVO valor (la primera llamada devuelve 1). NUNCA se
        reinicia. Atómico para el único escritor gracias a ``BEGIN IMMEDIATE``.
        """
        validate_camera_id(camera_id)
        with self._immediate() as cur:
            cur.execute(
                "INSERT INTO crossing_seq (camera_id, seq) VALUES (?, 1) "
                "ON CONFLICT(camera_id) DO UPDATE SET seq = seq + 1",
                (camera_id,),
            )
            row = cur.execute(
                "SELECT seq FROM crossing_seq WHERE camera_id = ?", (camera_id,)
            ).fetchone()
        return int(row["seq"])

    # -- eventos ----------------------------------------------------------

    def insert_event(self, event: CrossingEvent) -> bool:
        """Inserta un ``CrossingEvent``. IDEMPOTENTE por ``UNIQUE(event_id)``.

        Returns:
            ``True`` si insertó una fila nueva; ``False`` si el ``event_id`` ya
            existía (``INSERT OR IGNORE``: no duplica ni lanza).
        """
        validate_site_id(event.site_id)
        validate_device_id(event.device_id)
        validate_camera_id(event.camera_id)
        values = tuple(getattr(event, col) for col in _EVENT_COLUMNS)
        placeholders = ", ".join("?" for _ in _EVENT_COLUMNS)
        columns = ", ".join(_EVENT_COLUMNS)
        with self._immediate() as cur:
            cur.execute(
                f"INSERT OR IGNORE INTO events ({columns}) VALUES ({placeholders})",
                values,
            )
            inserted = cur.rowcount == 1
        return inserted

    def record_event(self, event: CrossingEvent) -> bool:
        """Persiste un evento Y bumpea el contador del día, atómicamente.

        El contador sólo sube cuando el evento es GENUINAMENTE nuevo (coherente
        con la idempotencia de ``insert_event``), de modo que los reintentos del
        mismo ``event_id`` no inflan los contadores.

        Returns:
            ``True`` si el evento era nuevo (insertado + contador bumpeado);
            ``False`` si era un duplicado (no se tocó nada).
        """
        validate_site_id(event.site_id)
        validate_device_id(event.device_id)
        validate_camera_id(event.camera_id)
        values = tuple(getattr(event, col) for col in _EVENT_COLUMNS)
        placeholders = ", ".join("?" for _ in _EVENT_COLUMNS)
        columns = ", ".join(_EVENT_COLUMNS)
        day_utc = _ms_to_day_utc(event.ts_event_ms)
        with self._immediate() as cur:
            cur.execute(
                f"INSERT OR IGNORE INTO events ({columns}) VALUES ({placeholders})",
                values,
            )
            if cur.rowcount != 1:
                return False
            cur.execute(
                "INSERT INTO counters (camera_id, day_utc, direction, count) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(camera_id, day_utc, direction) "
                "DO UPDATE SET count = count + 1",
                (event.camera_id, day_utc, event.direction),
            )
        return True

    def get_recent_events(self, camera_id: str, limit: int = 50) -> list[dict]:
        """Eventos recientes de la cámara, ``ts_event_ms`` DESC (más nuevo primero)."""
        validate_camera_id(camera_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE camera_id = ? "
            "ORDER BY ts_event_ms DESC, event_id DESC LIMIT ?",
            (camera_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- contadores -------------------------------------------------------

    def bump_counter(
        self, camera_id: str, day_utc: str, direction: str, delta: int = 1
    ) -> None:
        """Upsert transaccional del contador ``(camera_id, day_utc, direction)``."""
        validate_camera_id(camera_id)
        with self._immediate() as cur:
            cur.execute(
                "INSERT INTO counters (camera_id, day_utc, direction, count) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(camera_id, day_utc, direction) "
                "DO UPDATE SET count = count + excluded.count",
                (camera_id, day_utc, direction, int(delta)),
            )

    def get_counters(self, camera_id: str, day_utc: str | None = None) -> list[dict]:
        """Contadores de la cámara; si ``day_utc`` se da, sólo ese día."""
        validate_camera_id(camera_id)
        if day_utc is None:
            rows = self._conn.execute(
                "SELECT camera_id, day_utc, direction, count FROM counters "
                "WHERE camera_id = ? ORDER BY day_utc, direction",
                (camera_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT camera_id, day_utc, direction, count FROM counters "
                "WHERE camera_id = ? AND day_utc = ? ORDER BY direction",
                (camera_id, day_utc),
            ).fetchall()
        return [dict(r) for r in rows]
