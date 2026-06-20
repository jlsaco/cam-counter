"""Capa de repositorio sobre SQLite: persistencia LOCAL del subsistema de conteo.

SQLite es el **contrato único de persistencia local** del borde (edge-first / tolerante a
offline): el Pi cuenta y persiste en LOCAL aunque no haya red; la nube sólo recibe la
sincronización posterior. Este módulo abre la base en modo **WAL**, con ``busy_timeout``,
transacciones cortas y **una sola conexión de escritura** por proceso, y gestiona las
migraciones de forma **idempotente** vía ``PRAGMA user_version``.

Tablas (ver CLAUDE.md §8 contrato CrossingEvent):
  - ``events``      : una fila por cruce contado; columnas = campos de CrossingEvent, con
                      ``UNIQUE(event_id)`` y flag ``synced`` (0/1, sólo-local).
  - ``counters``    : contador agregado por ``(camera_id, day_utc, direction)``.
  - ``crossing_seq``: contador **MONÓTONO PERSISTIDO POR CÁMARA** (no por track, no
                      reiniciable jamás). Cada cruce contado obtiene un ``seq`` único
                      global-por-cámara aunque el ``track_id`` se reuse: esto cierra el
                      hueco de idempotencia (dos cruces distintos nunca colisionan en
                      ``event_id`` aunque compartan ``track_id``).

Todo aquí es **stdlib pura** (``sqlite3``): sin red, sin nube, sin hardware. Corre en CI
x86 con una DB temporal (``tmp_path``); ``:memory:`` NO comparte estado entre conexiones,
así que para ejercitar WAL real se usa un archivo en disco.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, is_dataclass
from typing import Any

from .identifiers import validate_camera_id
from .types import CROSSING_EVENT_FIELDS

# Versión de esquema de la base local. Se incrementa SÓLO si cambia el DDL; cada salto
# añade un bloque de migración idempotente en ``migrate()``.
SCHEMA_USER_VERSION = 1

# Timeout por defecto (ms) que espera un escritor ante un lock antes de fallar con
# ``database is locked``. Con una sola conexión de escritura los locks son raros, pero el
# WAL permite lectores concurrentes; un timeout holgado evita falsos negativos en CI.
DEFAULT_BUSY_TIMEOUT_MS = 5000

# Subconjunto de campos de CrossingEvent que son columnas físicas de la tabla ``events``.
# ``positive_label``/``negative_label`` son etiquetas de presentación derivables de la
# config de línea; el contrato persistido guarda la ya-resuelta ``label`` (más
# ``direction``), así que NO se materializan como columnas para no duplicar estado.
_EVENT_COLUMNS = tuple(
    f for f in CROSSING_EVENT_FIELDS if f not in ("positive_label", "negative_label")
)


class Store:
    """Repositorio SQLite (WAL) del subsistema de conteo, con una conexión de escritura.

    Una instancia por proceso de escritura. Es **multi-cámara**: ``crossing_seq`` y
    ``counters`` están indexados por ``camera_id``, de modo que varias cámaras comparten la
    misma base sin colisionar. Las transacciones son cortas y explícitas (``with
    self._conn`` hace commit/rollback atómico).
    """

    def __init__(self, db_path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        """Abre (o crea) la base en ``db_path`` y aplica los PRAGMAs y migraciones.

        Args:
            db_path: ruta del fichero SQLite. Se recomienda un fichero real (no
                ``:memory:``) para que WAL y la concurrencia de lectura sean efectivos.
            busy_timeout_ms: espera ante un lock antes de fallar (``database is locked``).
        """
        self.db_path = str(db_path)
        # ``isolation_level=None`` -> autocommit; controlamos las transacciones a mano con
        # bloques BEGIN/COMMIT explícitos para que sean CORTAS y atómicas.
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        # Integridad referencial defensiva (no hay FKs hoy, pero deja la puerta abierta).
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # ────────────────────────────── ciclo de vida ──────────────────────────────
    def close(self) -> None:
        """Cierra la conexión de escritura (idempotente: cerrar dos veces no lanza)."""
        try:
            self._conn.close()
        except sqlite3.ProgrammingError:
            pass

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ─────────────────────────────── migraciones ───────────────────────────────
    def migrate(self) -> None:
        """Crea/actualiza el esquema de forma IDEMPOTENTE vía ``PRAGMA user_version``.

        Re-ejecutar ``migrate()`` no duplica tablas ni datos: si ``user_version`` ya está
        al día no hace nada; los ``CREATE TABLE`` usan ``IF NOT EXISTS`` como segunda
        barrera. La transacción es atómica (todo o nada).
        """
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current >= SCHEMA_USER_VERSION:
            return
        with _transaction(self._conn):
            if current < 1:
                self._migrate_to_v1()
            # Fija la versión SÓLO tras aplicar el DDL (dentro de la misma transacción).
            self._conn.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")

    def _migrate_to_v1(self) -> None:
        """DDL de la versión 1 del esquema (events / counters / crossing_seq)."""
        # events: columnas = campos de CrossingEvent (sin las etiquetas de presentación).
        # UNIQUE(event_id) es el cierre de idempotencia del sync: un reintento del MISMO
        # event_id no inserta una segunda fila.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id       TEXT NOT NULL,
                site_id        TEXT NOT NULL,
                device_id      TEXT NOT NULL,
                camera_id      TEXT NOT NULL,
                track_id       TEXT NOT NULL,
                crossing_seq   INTEGER NOT NULL,
                direction      TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                label          TEXT,
                line_version   INTEGER NOT NULL,
                ts_event_ms    INTEGER NOT NULL,
                ts_event_iso   TEXT NOT NULL,
                confidence     REAL,
                clip_key       TEXT,
                clip_status    TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (clip_status IN ('pending','uploading','uploaded','failed')),
                schema_version INTEGER NOT NULL,
                synced         INTEGER NOT NULL DEFAULT 0 CHECK (synced IN (0, 1)),
                created_at     TEXT NOT NULL,
                UNIQUE (event_id)
            )
            """
        )
        # Lecturas frecuentes: eventos recientes por cámara ordenados por ts descendente.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_cam_ts "
            "ON events (camera_id, ts_event_ms DESC)"
        )
        # counters: agregado por (camera_id, day_utc, direction). PK lógica compuesta.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counters (
                camera_id TEXT NOT NULL,
                day_utc   TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (camera_id, day_utc, direction)
            )
            """
        )
        # crossing_seq: contador monótono por cámara. Una fila por camera_id.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crossing_seq (
                camera_id TEXT PRIMARY KEY,
                seq       INTEGER NOT NULL
            )
            """
        )

    # ──────────────────────────── crossing_seq (monótono) ───────────────────────
    def next_crossing_seq(self, camera_id: str) -> int:
        """Devuelve el siguiente ``crossing_seq`` de la cámara: TRANSACCIONAL y MONÓTONO.

        Crea la fila si no existe partiendo de 0, incrementa **atómicamente** y devuelve el
        NUEVO valor (>= 1 en la primera llamada). NUNCA se reinicia ni decrece, ni siquiera
        si un ``track_id`` se reusa: por eso dos cruces distintos de la misma cámara obtienen
        ``crossing_seq`` distintos y, por tanto, ``event_id`` distintos.

        Args:
            camera_id: slug de cámara (se valida antes de tocar la fila).

        Returns:
            El nuevo valor del contador para esa cámara.
        """
        validate_camera_id(camera_id)
        with _transaction(self._conn):
            # Upsert atómico: inserta seq=1 si es la primera vez, o incrementa el existente.
            self._conn.execute(
                """
                INSERT INTO crossing_seq (camera_id, seq) VALUES (?, 1)
                ON CONFLICT (camera_id) DO UPDATE SET seq = seq + 1
                """,
                (camera_id,),
            )
            row = self._conn.execute(
                "SELECT seq FROM crossing_seq WHERE camera_id = ?", (camera_id,)
            ).fetchone()
        return int(row["seq"])

    def peek_crossing_seq(self, camera_id: str) -> int:
        """Lee el ``crossing_seq`` actual de la cámara SIN incrementarlo (0 si no existe)."""
        validate_camera_id(camera_id)
        row = self._conn.execute(
            "SELECT seq FROM crossing_seq WHERE camera_id = ?", (camera_id,)
        ).fetchone()
        return int(row["seq"]) if row is not None else 0

    # ─────────────────────────────────── events ────────────────────────────────
    def insert_event(self, event: Any) -> bool:
        """Inserta un CrossingEvent de forma IDEMPOTENTE. Devuelve True si fue nuevo.

        Si ``event_id`` ya existe (``UNIQUE``), NO lanza y devuelve ``False`` sin duplicar;
        en una inserción nueva devuelve ``True``. Esto hace el sync seguro ante reintentos.

        Args:
            event: un ``CrossingEvent`` (dataclass) o un dict con sus campos.
        """
        data = _event_as_dict(event)
        camera_id = data.get("camera_id")
        validate_camera_id(camera_id)
        columns = [c for c in _EVENT_COLUMNS if c in data]
        placeholders = ", ".join("?" for _ in columns)
        col_sql = ", ".join(columns)
        values = [data[c] for c in columns]
        with _transaction(self._conn):
            cursor = self._conn.execute(
                f"INSERT OR IGNORE INTO events ({col_sql}) VALUES ({placeholders})",
                values,
            )
            inserted = cursor.rowcount > 0
        return inserted

    def get_recent_events(self, camera_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Devuelve los eventos recientes de la cámara, ``ts_event_ms`` DESC.

        Args:
            camera_id: cámara cuyas filas se leen.
            limit: nº máximo de filas a devolver.
        """
        validate_camera_id(camera_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE camera_id = ? "
            "ORDER BY ts_event_ms DESC, event_id DESC LIMIT ?",
            (camera_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Devuelve un evento por ``event_id`` (o None si no existe)."""
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def count_events(self, camera_id: str | None = None) -> int:
        """Cuenta filas de ``events`` (de una cámara si se indica, o de toda la base)."""
        if camera_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        else:
            validate_camera_id(camera_id)
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE camera_id = ?", (camera_id,)
            ).fetchone()
        return int(row["n"])

    # ────────────────────────────────── counters ───────────────────────────────
    def bump_counter(
        self, camera_id: str, day_utc: str, direction: str, delta: int = 1
    ) -> int:
        """Upsert TRANSACCIONAL del contador ``(camera_id, day_utc, direction)``.

        Crea la fila con ``count=delta`` si no existe, o le suma ``delta``. Devuelve el
        nuevo ``count``. El caller debe llamar a esto SÓLO cuando el evento es genuinamente
        nuevo (``insert_event`` devolvió True), para no inflar el contador en reintentos.

        Args:
            camera_id: cámara del contador.
            day_utc: día UTC (``YYYY-MM-DD``) al que se imputa el cruce.
            direction: ``'in'`` o ``'out'``.
            delta: incremento (por defecto 1).
        """
        validate_camera_id(camera_id)
        if direction not in ("in", "out"):
            raise ValueError(f"direction inválida: {direction!r} (se espera 'in' u 'out')")
        with _transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO counters (camera_id, day_utc, direction, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (camera_id, day_utc, direction)
                    DO UPDATE SET count = count + excluded.count
                """,
                (camera_id, day_utc, direction, int(delta)),
            )
            row = self._conn.execute(
                "SELECT count FROM counters "
                "WHERE camera_id = ? AND day_utc = ? AND direction = ?",
                (camera_id, day_utc, direction),
            ).fetchone()
        return int(row["count"])

    def get_counters(
        self, camera_id: str, day_utc: str | None = None
    ) -> list[dict[str, Any]]:
        """Lee los contadores de una cámara (de un día si se indica ``day_utc``).

        Returns:
            Lista de filas ``{camera_id, day_utc, direction, count}`` ordenadas de forma
            estable por ``(day_utc, direction)``.
        """
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

    # ─────────────────────────── helper de alto nivel ──────────────────────────
    def record_event(self, event: Any) -> bool:
        """Persiste un cruce de forma IDEMPOTENTE: inserta el evento y, SÓLO si fue nuevo,
        bumpea el contador del día/dirección.

        Devuelve True si el evento era nuevo (se insertó y se contó), False si ya existía
        (no se duplica ni se infla el contador). El ``day_utc`` se deriva del
        ``ts_event_iso`` del propio evento (primeros 10 chars, ``YYYY-MM-DD``) para no
        depender del reloj real.
        """
        inserted = self.insert_event(event)
        if not inserted:
            return False
        data = _event_as_dict(event)
        day_utc = str(data["ts_event_iso"])[:10]
        self.bump_counter(data["camera_id"], day_utc, data["direction"], delta=1)
        return True


# ─────────────────────────────── utilidades internas ───────────────────────────
def _event_as_dict(event: Any) -> dict[str, Any]:
    """Normaliza un CrossingEvent (dataclass o dict) a un dict plano de campos."""
    if is_dataclass(event) and not isinstance(event, type):
        return asdict(event)
    if isinstance(event, dict):
        return dict(event)
    raise TypeError(f"evento no soportado: {type(event)!r} (se espera dataclass o dict)")


class _transaction:
    """Context manager de transacción CORTA y atómica sobre una conexión en autocommit.

    Emite ``BEGIN`` al entrar y ``COMMIT`` al salir sin excepción; ante excepción hace
    ``ROLLBACK`` y re-lanza. Mantiene las transacciones explícitas y acotadas (CLAUDE.md:
    "transacciones cortas, una sola conexión de escritura").
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type: object, *_rest: object) -> bool:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False


def open_store(db_path: str, **kwargs: Any) -> Store:
    """Azúcar: abre y migra un :class:`Store` en ``db_path``."""
    return Store(db_path, **kwargs)


__all__ = [
    "Store",
    "open_store",
    "SCHEMA_USER_VERSION",
    "DEFAULT_BUSY_TIMEOUT_MS",
]
