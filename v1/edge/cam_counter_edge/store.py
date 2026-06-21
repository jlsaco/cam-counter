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
from .types import CrossingEvent, Line, LineConfig, Point

__all__ = ["SCHEMA_USER_VERSION", "StaleConfigVersionError", "Store"]

# Versión de esquema física de SQLite (PRAGMA user_version). Es independiente de
# CrossingEvent.schema_version (versión del contrato lógico).
#
# Historial de migraciones (todas ADITIVAS; DDL ``IF NOT EXISTS``):
#   v1 (PR07): events, counters, crossing_seq.
#   v2 (PR08): camera_config (config de línea por cámara, config_version
#              monótono) + clip_uploads (cola de subidas a S3).
SCHEMA_USER_VERSION = 2


class StaleConfigVersionError(RuntimeError):
    """``set_line_config`` con ``expected_version`` desactualizada (CAS fallido).

    Concurrencia optimista: un escritor sólo gana si su ``expected_version``
    coincide con el ``config_version`` ACTUAL en la DB. Si otro escritor ya
    bumpeó la versión, el ``expected_version`` queda stale y se rechaza el
    cambio (el caller debe releer la config y reintentar).
    """

    def __init__(self, camera_id: str, expected: int, current: int) -> None:
        self.camera_id = camera_id
        self.expected = int(expected)
        self.current = int(current)
        super().__init__(
            f"config_version stale para {camera_id!r}: "
            f"expected={self.expected}, actual={self.current}"
        )

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

-- v2 (PR08): config de línea por cámara. ``config_version`` es un entero
-- MONÓTONO por cámara (concurrencia optimista vía CAS en set_line_config).
-- Geometría en floats normalizados 0..1 (extremos A=(line_ax,line_ay),
-- B=(line_bx,line_by)); NUNCA píxeles.
CREATE TABLE IF NOT EXISTS camera_config (
    camera_id       TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    line_ax         REAL NOT NULL,
    line_ay         REAL NOT NULL,
    line_bx         REAL NOT NULL,
    line_by         REAL NOT NULL,
    positive_side   INTEGER NOT NULL,
    positive_label  TEXT,
    negative_label  TEXT,
    config_version  INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT
);

-- v2 (PR08): cola local de subidas de clips a S3 (la subida real es PR10).
-- ``s3_key_planned`` se construye desde la plantilla de media SÓLO con slugs
-- ya validados. ``status`` ∈ {'pending','uploading','uploaded','failed'}.
CREATE TABLE IF NOT EXISTS clip_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    local_path      TEXT NOT NULL,
    s3_key_planned  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_clip_uploads_status
    ON clip_uploads (status, id);
"""


def _ms_to_day_utc(ts_event_ms: int) -> str:
    """Día UTC ``YYYY-MM-DD`` derivado del epoch ms (determinista, sin reloj)."""
    return datetime.fromtimestamp(ts_event_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d")


def _now_iso() -> str:
    """Timestamp ISO-8601 UTC (ms) de pared para metadatos (``updated_at``).

    Sólo se usa para campos de auditoría (cuándo se cambió la config / se encoló
    una subida); NO entra en ningún contrato determinista (``event_id`` y demás
    se derivan de ``ts_event_ms``, no del reloj).
    """
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _point_xy(p: object) -> tuple[float, float]:
    """Extrae ``(x, y)`` de un ``Point`` o de un par ``(x, y)`` (robusto)."""
    if isinstance(p, Point):
        return (float(p.x), float(p.y))
    x, y = p  # type: ignore[misc]
    return (float(x), float(y))


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

    # -- sincronización edge -> cloud (consumido por sync.py) -------------

    def get_unsynced_events(self, limit: int = 200) -> list[dict]:
        """Eventos pendientes de subir a la nube (``synced=0``), FIFO por inserción.

        Orden ASCENDENTE por ``ts_event_ms`` (y desempate por ``event_id``) para
        drenar el backlog en el mismo orden en que se contó. La lectura va sobre
        WAL (no toma lock de escritura), de modo que el worker de sync no compite
        con el proceso de conteo. La nube nunca dicta este trabajo: la fuente de
        verdad de "qué falta subir" es SIEMPRE esta tabla local (manifest-no-registry).
        """
        rows = self._conn.execute(
            "SELECT * FROM events WHERE synced = 0 "
            "ORDER BY ts_event_ms ASC, event_id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_unsynced_events(self) -> int:
        """Número de eventos aún no sincronizados (``synced=0``)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE synced = 0"
        ).fetchone()
        return int(row["n"])

    def mark_event_synced(self, event_id: str) -> bool:
        """Marca un evento como sincronizado (``synced=1``). Idempotente.

        Returns:
            ``True`` si actualizó la fila (estaba en 0); ``False`` si no existía o
            ya estaba en 1. Re-marcar un evento ya sincronizado es un no-op seguro.
        """
        with self._immediate() as cur:
            cur.execute(
                "UPDATE events SET synced = 1 WHERE event_id = ? AND synced = 0",
                (event_id,),
            )
            return cur.rowcount == 1

    def set_event_clip(
        self, event_id: str, clip_key: str | None, clip_status: str | None
    ) -> None:
        """Actualiza ``clip_key``/``clip_status`` de un evento ya persistido.

        Lo usa el worker de sync para reflejar localmente que el clip quedó subido
        (``clip_status='uploaded'`` + la clave S3 real) antes/después del
        conditional-put del evento en DynamoDB.
        """
        with self._immediate() as cur:
            cur.execute(
                "UPDATE events SET clip_key = ?, clip_status = ? WHERE event_id = ?",
                (clip_key, clip_status, event_id),
            )

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

    # -- config de línea por cámara (hot-reload vía config_version) --------

    def get_config_version(self, camera_id: str) -> int:
        """``config_version`` ACTUAL de la cámara (0 si no hay config todavía).

        Lectura BARATA de una sola columna pensada para llamarse UNA VEZ POR
        FRAME desde el ``ConfigWatcher``: es un ``SELECT`` simple sobre WAL (no
        toma lock de escritura, no bloquea al único escritor). Devolver 0 ante
        la ausencia de fila permite que el primer ``set_line_config`` use
        ``expected_version=0`` y obtenga ``config_version=1``.
        """
        validate_camera_id(camera_id)
        row = self._conn.execute(
            "SELECT config_version FROM camera_config WHERE camera_id = ?",
            (camera_id,),
        ).fetchone()
        return int(row["config_version"]) if row is not None else 0

    def get_line_config(self, camera_id: str) -> LineConfig | None:
        """Devuelve el ``LineConfig`` persistido de la cámara, o ``None``."""
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
            config_version=int(row["config_version"]),
            line=Line(
                a=Point(float(row["line_ax"]), float(row["line_ay"])),
                b=Point(float(row["line_bx"]), float(row["line_by"])),
            ),
            positive_side=int(row["positive_side"]),
            positive_label=row["positive_label"],
            negative_label=row["negative_label"],
            updated_at=row["updated_at"],
            schema_version=int(row["schema_version"]),
        )

    def set_line_config(
        self, camera_id: str, config: LineConfig, expected_version: int
    ) -> int:
        """Persiste la config de línea con CONCURRENCIA OPTIMISTA (compare-and-set).

        Sólo escribe si ``expected_version`` coincide con el ``config_version``
        ACTUAL en la DB (0 si la fila no existe aún); en caso contrario lanza
        ``StaleConfigVersionError`` SIN tocar nada. En éxito INCREMENTA
        ``config_version`` monótonamente (``actual + 1``), actualiza la geometría
        y ``updated_at``, y devuelve el NUEVO ``config_version``.

        El ``config_version`` lo gobierna la DB: el valor del campo homónimo en
        ``config`` se IGNORA en la escritura (la geometría/labels/positive_side
        sí se toman de ``config``). Atómico para el único escritor vía
        ``BEGIN IMMEDIATE``.
        """
        validate_camera_id(camera_id)
        validate_site_id(config.site_id)
        validate_device_id(config.device_id)
        if config.positive_side not in (-1, 1):
            raise ValueError(
                f"positive_side debe ser -1 o +1, no {config.positive_side!r}"
            )
        ax, ay = _point_xy(config.line.a)
        bx, by = _point_xy(config.line.b)
        now = _now_iso()
        with self._immediate() as cur:
            row = cur.execute(
                "SELECT config_version FROM camera_config WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            current = int(row["config_version"]) if row is not None else 0
            if int(expected_version) != current:
                raise StaleConfigVersionError(camera_id, expected_version, current)
            new_version = current + 1
            cur.execute(
                "INSERT INTO camera_config ("
                " camera_id, site_id, device_id, line_ax, line_ay, line_bx, line_by,"
                " positive_side, positive_label, negative_label, config_version,"
                " schema_version, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(camera_id) DO UPDATE SET "
                " site_id=excluded.site_id, device_id=excluded.device_id,"
                " line_ax=excluded.line_ax, line_ay=excluded.line_ay,"
                " line_bx=excluded.line_bx, line_by=excluded.line_by,"
                " positive_side=excluded.positive_side,"
                " positive_label=excluded.positive_label,"
                " negative_label=excluded.negative_label,"
                " config_version=excluded.config_version,"
                " schema_version=excluded.schema_version,"
                " updated_at=excluded.updated_at",
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
                    int(config.schema_version),
                    now,
                ),
            )
        return new_version

    # -- cola de subidas de clips a S3 (clip_uploads) ---------------------

    def enqueue_clip_upload(
        self,
        *,
        event_id: str,
        camera_id: str,
        local_path: str,
        s3_key_planned: str,
    ) -> int:
        """Inserta una fila ``pending`` en ``clip_uploads`` y devuelve su ``id``.

        ``s3_key_planned`` debe venir YA construida desde la plantilla de media
        con slugs validados (ver ``identifiers.media_clip_key``). La subida real
        (PR10) consumirá estas filas de forma idempotente y reintentable.
        """
        validate_camera_id(camera_id)
        now = _now_iso()
        with self._immediate() as cur:
            cur.execute(
                "INSERT INTO clip_uploads ("
                " event_id, camera_id, local_path, s3_key_planned, status,"
                " attempts, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                (event_id, camera_id, local_path, s3_key_planned, now, now),
            )
            rowid = cur.lastrowid
        return int(rowid)

    def get_clip_uploads(
        self, camera_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Filas de ``clip_uploads`` (opcionalmente filtradas), ordenadas por ``id``."""
        query = (
            "SELECT id, event_id, camera_id, local_path, s3_key_planned, status, "
            "attempts, created_at, updated_at FROM clip_uploads"
        )
        conds: list[str] = []
        params: list[object] = []
        if camera_id is not None:
            validate_camera_id(camera_id)
            conds.append("camera_id = ?")
            params.append(camera_id)
        if status is not None:
            conds.append("status = ?")
            params.append(status)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY id"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def get_clip_upload_for_event(self, event_id: str) -> dict | None:
        """Última fila de ``clip_uploads`` de un ``event_id`` (o ``None`` si no hay).

        El worker de sync la usa para localizar el ``local_path`` del clip y la
        ``s3_key_planned`` ya construida (con slugs validados) al subir a S3. Toma
        la de mayor ``id`` por si hubo reintentos de grabación del mismo evento.
        """
        row = self._conn.execute(
            "SELECT id, event_id, camera_id, local_path, s3_key_planned, status, "
            "attempts, created_at, updated_at FROM clip_uploads "
            "WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_clip_upload_status(
        self, row_id: int, status: str, *, bump_attempts: bool = False
    ) -> None:
        """Actualiza el ``status`` de una fila de ``clip_uploads`` (y ``updated_at``).

        ``status`` ∈ {'pending','uploading','uploaded','failed'}. Con
        ``bump_attempts`` incrementa el contador de intentos (para el backoff del
        worker de sync). No falla si la fila no existe (UPDATE no-op).
        """
        now = _now_iso()
        attempts_sql = ", attempts = attempts + 1" if bump_attempts else ""
        with self._immediate() as cur:
            cur.execute(
                f"UPDATE clip_uploads SET status = ?, updated_at = ?{attempts_sql} "
                "WHERE id = ?",
                (status, now, int(row_id)),
            )
