"""Entrypoint del worker de sincronización edge -> cloud (``cam-counter-sync``).

Cierra el lazo que faltaba: ``CloudSync`` (en ``sync.py``) sabe drenar los
``CrossingEvent`` locales ``synced=0`` hacia AWS (DynamoDB + S3), pero NADIE lo
arrancaba — por eso los eventos se contaban y persistían en SQLite pero nunca
llegaban a la nube. Este runner lo ejecuta en bucle, desacoplado del conteo:

- Lee el MISMO SQLite del borde (WAL: el edge escribe, este worker lee/marca).
- Cada ``CAMCOUNTER_SYNC_INTERVAL_S`` llama a ``CloudSync.sync_once`` (idempotente
  y offline-tolerante: si AWS no responde, los eventos quedan ``synced=0`` y se
  reintentan; los duplicados no son error).
- Hace un ``heartbeat`` best-effort al registro de dispositivos (DynamoDB).

Corre en el VENV (tiene ``boto3`` + ``cam_counter_edge``); NO necesita cv2/Hailo,
así que un fallo de red aquí jamás afecta al pipeline de detección del edge.

**Selector de transporte (WP16, corte del camino directo):** este runner ES el
camino DIRECTO (boto3 -> DynamoDB/S3 con credenciales AWS). Sólo opera cuando
``CAMCOUNTER_SYNC_TRANSPORT`` vale ``direct`` (su valor por defecto). Cuando vale
``iot`` el camino directo queda INERTE (sale sin construir clientes boto3 ni asumir
rol): en ese modo el proceso de borde sincroniza por MQTT (``mqtt_publisher``) y NO
usa credenciales AWS directas. El corte es REVERSIBLE: volver a ``direct`` reactiva
este runner sin tocar nada más.

Config por entorno (sin secretos en el repo):
  CAMCOUNTER_SYNC_ENABLED      '1' para arrancar (si no, sale sin hacer nada).
  CAMCOUNTER_SYNC_TRANSPORT    'direct' (def) corre este camino; 'iot' lo deja inerte.
  CAMCOUNTER_DB_PATH           SQLite del borde (igual que edge/api).
  CAMCOUNTER_DEVICE_ID         device del Pi (heartbeat).
  CAMCOUNTER_AWS_REGION        región AWS (def us-east-1).
  CAMCOUNTER_EDGE_ROLE_ARN     rol STS per-Pi a asumir (opcional; vacío = creds por defecto).
  CAMCOUNTER_SYNC_INTERVAL_S   periodo del drenaje (def 10s).
  CAMCOUNTER_MEDIA_BUCKET / _EVENTS_TABLE / _DEVICES_TABLE  overrides opcionales.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Any

from .store import Store
from .sync import (
    DEFAULT_DEVICES_TABLE,
    DEFAULT_EVENTS_TABLE,
    DEFAULT_MEDIA_BUCKET,
    DEFAULT_REGION,
    CloudSync,
    default_client_factory,
    is_conditional_check_failed,
    is_precondition_failed,
)

__all__ = ["TRANSPORT_DIRECT", "TRANSPORT_IOT", "main", "resolve_transport"]

_log = logging.getLogger(__name__)

# Valores canónicos del selector de transporte (CLAUDE.md / provision-device.sh).
TRANSPORT_DIRECT = "direct"
TRANSPORT_IOT = "iot"


def resolve_transport() -> str:
    """Lee ``CAMCOUNTER_SYNC_TRANSPORT`` normalizado (def ``direct``).

    Sólo ``iot`` (case-insensitive) selecciona el camino MQTT; cualquier otro valor
    (incluido vacío) cae a ``direct`` para que el corte sea explícito y reversible:
    nadie acaba en modo ``iot`` por un typo en el ``.env``.
    """
    value = os.environ.get("CAMCOUNTER_SYNC_TRANSPORT", "").strip().lower()
    return TRANSPORT_IOT if value == TRANSPORT_IOT else TRANSPORT_DIRECT


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _app_version() -> str:
    """Versión reportada en el heartbeat (best-effort; no crítica)."""
    return os.environ.get("CAMCOUNTER_APP_VERSION", "edge-dev")


def _drain_once(
    store: Any,
    sync: CloudSync,
    *,
    clips_enabled: bool,
    grace_ms: int,
    now_ms: int,
    limit: int = 200,
) -> tuple[int, int, int, bool]:
    """Drena eventos synced=0, ESPERANDO a que el clip de cada evento este listo.

    Si los clips estan activos y un evento aun no tiene fila en ``clip_uploads`` y
    es mas joven que ``grace_ms``, se SALTA esta ronda (se reintenta luego, cuando
    el clip ya este encolado) para que ``sync_event`` suba el clip y enlace
    ``clip_key`` en DynamoDB en la MISMA pasada. Pasado el ``grace``, sincroniza
    igual (sin media) para no bloquear el backlog. Idempotente y offline-tolerante.

    Devuelve ``(procesados, sincronizados, esperando_clip, parado_offline)``.
    """
    events = store.get_unsynced_events(limit)
    processed = synced = waiting = 0
    stopped = False
    for event in events:
        if clips_enabled:
            clip = store.get_clip_upload_for_event(event.event_id)
            age_ms = now_ms - int(event.ts_event_ms)
            if clip is None and age_ms < grace_ms:
                waiting += 1
                continue  # el clip aun no esta listo: esperar (se reintenta luego)
        processed += 1
        try:
            outcome = sync.sync_event(event)
        except Exception as exc:  # noqa: BLE001
            if is_conditional_check_failed(exc) or is_precondition_failed(exc):
                continue  # duplicado idempotente: no deberia llegar aqui, pero ignora
            _log.warning(
                "cam-counter-sync: fallo transitorio en %s (%r); se reintentara",
                event.event_id,
                exc,
            )
            stopped = True
            break
        if outcome.marked_synced:
            synced += 1
    return processed, synced, waiting, stopped


def main(argv: list[str] | None = None) -> int:
    """Bucle del worker de sync. Devuelve 0 al recibir señal de parada."""
    logging.basicConfig(level=logging.INFO)

    if not _env_flag("CAMCOUNTER_SYNC_ENABLED"):
        _log.info("cam-counter-sync: CAMCOUNTER_SYNC_ENABLED no está activo; nada que hacer.")
        return 0

    # WP16 — corte del camino directo. En modo 'iot' el camino directo NO corre: el
    # proceso de borde sincroniza por MQTT (mqtt_publisher) y deja de usar
    # credenciales AWS directas. Salimos ANTES de construir cualquier cliente boto3 o
    # asumir rol STS (fail-closed contra creds directas). Reversible: volver a 'direct'.
    transport = resolve_transport()
    if transport == TRANSPORT_IOT:
        _log.info(
            "cam-counter-sync: CAMCOUNTER_SYNC_TRANSPORT=iot; el camino DIRECTO queda "
            "inerte (sin boto3 ni rol STS). La sincronización va por MQTT "
            "(cam_counter_edge.mqtt_publisher). Vuelve a 'direct' para reactivarlo."
        )
        return 0

    db_path = _env("CAMCOUNTER_DB_PATH", "cam-counter.db")
    device_id = _env("CAMCOUNTER_DEVICE_ID", "demo-pi")
    region = _env("CAMCOUNTER_AWS_REGION", DEFAULT_REGION)
    role_arn = os.environ.get("CAMCOUNTER_EDGE_ROLE_ARN") or None
    media_bucket = _env("CAMCOUNTER_MEDIA_BUCKET", DEFAULT_MEDIA_BUCKET)
    events_table = _env("CAMCOUNTER_EVENTS_TABLE", DEFAULT_EVENTS_TABLE)
    devices_table = _env("CAMCOUNTER_DEVICES_TABLE", DEFAULT_DEVICES_TABLE)
    try:
        interval_s = max(2.0, float(_env("CAMCOUNTER_SYNC_INTERVAL_S", "10")))
    except ValueError:
        interval_s = 10.0
    clips_enabled = _env("CAMCOUNTER_CLIPS_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    try:
        grace_ms = int(float(_env("CAMCOUNTER_CLIP_GRACE_S", "15")) * 1000)
    except ValueError:
        grace_ms = 15000

    store = Store(db_path)

    def factory() -> Any:
        return default_client_factory(region=region, role_arn=role_arn)

    sync = CloudSync(
        store,
        device_id=device_id,
        client_factory=factory,
        media_bucket=media_bucket,
        events_table=events_table,
        devices_table=devices_table,
    )

    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        _log.info("cam-counter-sync: señal recibida; parando…")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _log.info(
        "cam-counter-sync: device=%s region=%s tabla=%s intervalo=%ss (rol=%s)",
        device_id,
        region,
        events_table,
        interval_s,
        role_arn or "creds-por-defecto",
    )

    last_heartbeat = 0.0
    while not stop.is_set():
        try:
            now_ms = int(time.time() * 1000)
            processed, done, waiting, stopped = _drain_once(
                store, sync, clips_enabled=clips_enabled, grace_ms=grace_ms, now_ms=now_ms
            )
            if processed or waiting:
                _log.info(
                    "cam-counter-sync: procesados=%d sincronizados=%d esperando_clip=%d offline=%s",
                    processed,
                    done,
                    waiting,
                    stopped,
                )
        except Exception as exc:  # noqa: BLE001 — el worker NUNCA debe morir por un fallo de sync
            _log.warning("cam-counter-sync: error en el drenaje (%r); reintento luego", exc)

        # Heartbeat best-effort cada ~60s (no bloquea ni mata el worker si falla).
        now = time.monotonic()
        if now - last_heartbeat > 60.0:
            try:
                sync.heartbeat(reported_version=_app_version(), status="online")
                last_heartbeat = now
            except Exception as exc:  # noqa: BLE001
                _log.debug("cam-counter-sync: heartbeat falló (%r)", exc)

        stop.wait(interval_s)

    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
