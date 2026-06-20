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
from datetime import datetime, timezone
from typing import Any

from .identifiers import (
    validate_camera_id,
    validate_device_id,
    validate_site_id,
)
from .types import CROSSING_EVENT_FIELDS, LineConfig

# Versión de esquema de la base local. Se incrementa SÓLO si cambia el DDL; cada salto
# añade un bloque de migración idempotente en ``migrate()``.
#   v1: events / counters / crossing_seq (PR07).
#   v2: camera_config (config de línea por cámara, config_version monótono) + clip_uploads
#       (cola de subida de clips), añadidos por PR08.
SCHEMA_USER_VERSION = 2

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


class StaleConfigVersionError(RuntimeError):
    """Conflicto de **concurrencia optimista** al escribir la config de línea.

    Lo lanza :meth:`Store.set_line_config` cuando el ``expected_version`` del escritor NO
    coincide con el ``config_version`` actualmente persistido para la cámara: otro escritor
    ganó la carrera y el cambio se RECHAZA (no se aplica, no se incrementa la versión). El
    caller debe releer la config y reintentar sobre la versión nueva.
    """

    def __init__(self, camera_id: str, expected: int, actual: int) -> None:
        self.camera_id = camera_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"config de línea desactualizada para {camera_id!r}: "
            f"expected_version={expected} pero el actual es {actual}"
        )


def _utc_now_iso() -> str:
    """Instante actual en ISO-8601 UTC (sufijo ``Z``) para columnas ``*_at``."""
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


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
            # Cada salto es ADITIVO e idempotente: una DB nueva (current=0) aplica todos los
            # pasos en orden; una DB ya en v1 aplica SÓLO v2. Nunca se reescriben tablas
            # existentes (CLAUDE.md: migraciones monótonas vía PRAGMA user_version).
            if current < 1:
                self._migrate_to_v1()
            if current < 2:
                self._migrate_to_v2()
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

    def _migrate_to_v2(self) -> None:
        """DDL de la versión 2 del esquema (camera_config / clip_uploads), añadida por PR08.

        ADITIVO: sólo CREATE TABLE nuevos; no altera events/counters/crossing_seq de v1.
        """
        # camera_config: config de la línea-umbral POR CÁMARA, con ``config_version``
        # MONÓTONO. La geometría se guarda como floats normalizados 0..1 (ax,ay,bx,by); NUNCA
        # píxeles (CLAUDE.md §4). El hot-reload del pipeline se dispara al ver un
        # ``config_version`` mayor (ver config.ConfigWatcher); la escritura usa concurrencia
        # optimista contra ``config_version`` (ver set_line_config).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS camera_config (
                camera_id      TEXT PRIMARY KEY,
                site_id        TEXT NOT NULL,
                device_id      TEXT NOT NULL,
                ax             REAL NOT NULL,
                ay             REAL NOT NULL,
                bx             REAL NOT NULL,
                by             REAL NOT NULL,
                positive_side  INTEGER NOT NULL DEFAULT 1 CHECK (positive_side IN (-1, 1)),
                positive_label TEXT NOT NULL DEFAULT 'in',
                negative_label TEXT NOT NULL DEFAULT 'out',
                config_version INTEGER NOT NULL DEFAULT 1,
                updated_at     TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # clip_uploads: cola LOCAL de subidas de clips a S3. El grabador (clip.py) inserta
        # una fila ``pending`` con la clave de media YA planificada (s3_key_planned). El
        # uploader real (PR10) la consumirá de forma idempotente y a prueba de reintentos.
        # UNIQUE(event_id): un mismo evento => UN solo clip en cola (cierra el dedupe local).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clip_uploads (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id       TEXT NOT NULL,
                camera_id      TEXT NOT NULL,
                local_path     TEXT NOT NULL,
                s3_key_planned TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending','uploading','uploaded','failed')),
                attempts       INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL DEFAULT '',
                UNIQUE (event_id)
            )
            """
        )
        # Lectura típica del uploader: las filas pendientes en orden FIFO (id ascendente).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clip_uploads_status "
            "ON clip_uploads (status, id)"
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

    # ───────────────────────── config de línea por cámara ──────────────────────
    def get_config_version(self, camera_id: str) -> int:
        """Lee SÓLO ``config_version`` de la cámara (0 si no hay config). BARATO.

        Pensado para llamarse **una vez por frame** desde el camino de conteo: es una
        lectura de una sola columna por PK, lock-free desde la óptica del lector (WAL), sin
        deserializar la geometría. Si el valor cambió, el caller (ConfigWatcher) recién
        entonces hace la lectura completa con :meth:`get_line_config`.
        """
        validate_camera_id(camera_id)
        row = self._conn.execute(
            "SELECT config_version FROM camera_config WHERE camera_id = ?", (camera_id,)
        ).fetchone()
        return int(row["config_version"]) if row is not None else 0

    def get_line_config(self, camera_id: str) -> LineConfig | None:
        """Devuelve el :class:`~cam_counter_edge.types.LineConfig` de la cámara, o None.

        Reconstruye los extremos normalizados ``a=(ax,ay)`` / ``b=(bx,by)`` y las etiquetas
        de presentación. ``None`` si la cámara aún no tiene config persistida.
        """
        validate_camera_id(camera_id)
        row = self._conn.execute(
            "SELECT * FROM camera_config WHERE camera_id = ?", (camera_id,)
        ).fetchone()
        if row is None:
            return None
        return LineConfig(
            site_id=row["site_id"],
            device_id=row["device_id"],
            camera_id=row["camera_id"],
            a=(float(row["ax"]), float(row["ay"])),
            b=(float(row["bx"]), float(row["by"])),
            positive_side=int(row["positive_side"]),
            positive_label=row["positive_label"],
            negative_label=row["negative_label"],
            config_version=int(row["config_version"]),
        )

    def set_line_config(
        self, camera_id: str, config: LineConfig, expected_version: int
    ) -> int:
        """Escribe la config de línea con **concurrencia optimista**; devuelve la versión nueva.

        Contrato:
          - Lee el ``config_version`` actual (0 si la cámara no tenía config).
          - Si ``expected_version`` NO coincide con el actual, lanza
            :class:`StaleConfigVersionError` y NO aplica nada (rechazo de versión stale).
          - En éxito, escribe la geometría y **incrementa** ``config_version`` a
            ``actual + 1`` de forma monótona, actualiza ``updated_at`` y devuelve la versión
            nueva. Para CREAR la primera config de una cámara, pasa ``expected_version=0``.

        Todo dentro de una transacción CORTA (la lectura del actual y el upsert son atómicos
        respecto de otros escritores, que de todos modos comparten la única conexión de
        escritura del proceso).

        Args:
            camera_id: cámara objetivo (debe coincidir con ``config.camera_id``).
            config: nueva geometría/etiquetas (su ``config_version`` se IGNORA: la versión
                la fija el store de forma autoritativa = actual + 1).
            expected_version: versión sobre la que el escritor basó su edición.

        Returns:
            El nuevo ``config_version`` persistido.
        """
        validate_camera_id(camera_id)
        validate_site_id(config.site_id)
        validate_device_id(config.device_id)
        validate_camera_id(config.camera_id)
        if config.camera_id != camera_id:
            raise ValueError(
                f"camera_id no coincide: {camera_id!r} vs config.camera_id={config.camera_id!r}"
            )
        if int(config.positive_side) not in (-1, 1):
            raise ValueError(
                f"positive_side inválido: {config.positive_side!r} (se espera +1/-1)"
            )
        ax, ay = float(config.a[0]), float(config.a[1])
        bx, by = float(config.b[0]), float(config.b[1])
        with _transaction(self._conn):
            row = self._conn.execute(
                "SELECT config_version FROM camera_config WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            actual = int(row["config_version"]) if row is not None else 0
            if int(expected_version) != actual:
                # Rechazo de versión stale: NO se aplica el cambio (rollback al salir).
                raise StaleConfigVersionError(camera_id, int(expected_version), actual)
            new_version = actual + 1
            self._conn.execute(
                """
                INSERT INTO camera_config (
                    camera_id, site_id, device_id, ax, ay, bx, by,
                    positive_side, positive_label, negative_label,
                    config_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (camera_id) DO UPDATE SET
                    site_id        = excluded.site_id,
                    device_id      = excluded.device_id,
                    ax             = excluded.ax,
                    ay             = excluded.ay,
                    bx             = excluded.bx,
                    by             = excluded.by,
                    positive_side  = excluded.positive_side,
                    positive_label = excluded.positive_label,
                    negative_label = excluded.negative_label,
                    config_version = excluded.config_version,
                    updated_at     = excluded.updated_at
                """,
                (
                    camera_id,
                    config.site_id,
                    config.device_id,
                    ax,
                    ay,
                    bx,
                    by,
                    int(config.positive_side),
                    config.positive_label,
                    config.negative_label,
                    new_version,
                    _utc_now_iso(),
                ),
            )
        return new_version

    # ──────────────────────── cola de subida de clips ──────────────────────────
    def enqueue_clip_upload(
        self,
        *,
        event_id: str,
        camera_id: str,
        local_path: str,
        s3_key_planned: str,
        status: str = "pending",
    ) -> int:
        """Encola una subida de clip; devuelve el ``id`` de la fila (idempotente por event_id).

        Inserta una fila (por defecto ``status='pending'``, ``attempts=0``). ``UNIQUE
        (event_id)`` garantiza que un mismo evento tenga **una sola** fila en la cola: si ya
        existía, NO duplica y devuelve el ``id`` existente. El ``s3_key_planned`` ya debe venir
        construido desde slugs validados (ver ``identifiers.build_media_key``); el store no lo
        reconstruye.

        Args:
            event_id: ``event_id`` determinista del CrossingEvent (sha1 hex).
            camera_id: cámara dueña del clip (se valida).
            local_path: ruta del fichero ya escrito (MP4/GIF) en ``shared/``.
            s3_key_planned: clave de media planificada (plantilla §7), ya validada.
            status: estado inicial (por defecto ``'pending'``).
        """
        validate_camera_id(camera_id)
        if status not in ("pending", "uploading", "uploaded", "failed"):
            raise ValueError(f"status de clip inválido: {status!r}")
        now = _utc_now_iso()
        with _transaction(self._conn):
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO clip_uploads (
                    event_id, camera_id, local_path, s3_key_planned,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (event_id, camera_id, local_path, s3_key_planned, status, now, now),
            )
            if cursor.rowcount > 0:
                new_id = int(cursor.lastrowid)
            else:
                existing = self._conn.execute(
                    "SELECT id FROM clip_uploads WHERE event_id = ?", (event_id,)
                ).fetchone()
                new_id = int(existing["id"])
        return new_id

    def list_clip_uploads(
        self, *, status: str | None = None, camera_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Lista filas de ``clip_uploads`` (filtrables por ``status``/``camera_id``), FIFO."""
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if camera_id is not None:
            validate_camera_id(camera_id)
            clauses.append("camera_id = ?")
            params.append(camera_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM clip_uploads{where} ORDER BY id", params
        ).fetchall()
        return [dict(r) for r in rows]

    def count_clip_uploads(self, *, status: str | None = None) -> int:
        """Cuenta filas de ``clip_uploads`` (de un ``status`` si se indica)."""
        if status is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM clip_uploads").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM clip_uploads WHERE status = ?", (status,)
            ).fetchone()
        return int(row["n"])


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
    "StaleConfigVersionError",
    "SCHEMA_USER_VERSION",
    "DEFAULT_BUSY_TIMEOUT_MS",
]
