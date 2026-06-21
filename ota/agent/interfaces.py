"""Interfaces inyectables del agente (S3, systemd, HTTP health, reloj, registry).

Abstraer las dependencias externas detrás de Protocols permite testear TODO el flujo del
agente en x86 sin Pi/AWS/red (con fakes deterministas), y mantener el código de producción
delgado (boto3 / systemctl / urllib se aíslan en implementaciones concretas).
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    """Lectura de objetos del bucket de releases vía SigV4/IAM (NUNCA presigned)."""

    def get_bytes(self, key: str) -> bytes:
        """Descarga el objeto `key`. Lanza si no existe / error de red."""
        ...


@runtime_checkable
class ServiceController(Protocol):
    """Control del servicio de producto (systemd)."""

    def restart(self, name: str) -> None: ...

    def is_active(self, name: str) -> bool: ...

    def n_restarts(self, name: str) -> int:
        """Contador `NRestarts` de systemd (para detectar crash-loop durante el soak)."""
        ...


@runtime_checkable
class HealthProbe(Protocol):
    """Cliente del endpoint de salud de PRODUCTO (`/api/health`)."""

    def get(self) -> dict:
        """Devuelve el JSON de salud. Lanza `HealthUnavailable` si no-200/no-conecta."""
        ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def now_iso(self) -> str:
        """Instante actual ISO-8601 UTC (para heartbeats `last_seen_at`)."""
        ...


@runtime_checkable
class Registry(Protocol):
    """Heartbeat al device-registry (espejo/observabilidad). NUNCA decide la actualización."""

    def heartbeat(self, **fields) -> None: ...


class HealthUnavailable(Exception):
    """El endpoint de salud no respondió 200 (o no se pudo contactar)."""
