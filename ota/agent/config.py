"""Configuración del update-agent.

El **canal** (`canary`/`stable`) y el resto de parámetros se leen de un fichero LOCAL
provisionado (`/opt/cam-counter/shared/agent.toml` por defecto), NUNCA de la red. Cada
campo es overridable por variable de entorno `CAM_COUNTER_OTA_*` (útil para tests y para
inyectar el `device_id`/`site_id` por dispositivo en provisioning).

Layout en dispositivo (ver README de OTA):
  install_root/releases/<version>/   inmutable, una carpeta por versión instalada
  install_root/current               symlink atómico a la release activa (MISMO FS)
  install_root/shared/               estado mutable que sobrevive upgrade/rollback (puede
                                     ser OTRO mount; nunca se cruza con un rename atómico)
"""
import os
import tomllib
from dataclasses import dataclass, field, fields

_VALID_CHANNELS = ("canary", "stable")
_ENV_PREFIX = "CAM_COUNTER_OTA_"


@dataclass
class AgentConfig:
    # Identidad (provisionada por dispositivo).
    device_id: str = "rpi-001"
    site_id: str = "sitio-demo"
    camera_ids: list = field(default_factory=lambda: ["rpi-001-cam0"])

    # Canal asignado: LOCAL, nunca de la red.
    channel: str = "canary"

    # Origen de la versión deseada (manifiesto del canal en S3).
    bucket: str = "cam-counter-fleet-releases-950639281773"
    region: str = "us-east-1"

    # Layout en dispositivo.
    install_root: str = "/opt/cam-counter"
    service_name: str = "cam-counter.service"

    # Verificación de firma: pubkey minisign fijada (pública).
    pubkey_path: str = ""  # default: <paquete>/keys/cam-counter-release.pub

    # Health-check de PRODUCTO con soak.
    health_url: str = "http://127.0.0.1:8000/api/health"
    soak_seconds: float = 90.0
    poll_interval: float = 5.0
    inference_recency_ms: int = 30000  # last_inference_ts debe ser más reciente que esto
    expected_db_schema_version: int = -1  # -1 = no se exige un valor concreto

    # Retención: nunca borrar last_good; conservar >= N releases.
    min_releases_retained: int = 2

    # Versión de este agente (gate min_agent_version del manifiesto).
    agent_version: str = "0.1.0"

    # ── rutas derivadas ─────────────────────────────────────────────
    @property
    def releases_dir(self):
        return os.path.join(self.install_root, "releases")

    @property
    def current_link(self):
        return os.path.join(self.install_root, "current")

    @property
    def shared_dir(self):
        return os.path.join(self.install_root, "shared")

    @property
    def state_dir(self):
        # Estado del agente (last_good, failed-markers): vive en shared/ (sobrevive upgrades).
        return os.path.join(self.shared_dir, "ota")

    @property
    def resolved_pubkey_path(self):
        if self.pubkey_path:
            return self.pubkey_path
        return os.path.join(os.path.dirname(__file__), "keys", "cam-counter-release.pub")

    def channel_manifest_key(self):
        return f"channels/{self.channel}/manifest.json"

    def validate(self):
        if self.channel not in _VALID_CHANNELS:
            raise ValueError(
                f"canal inválido {self.channel!r}; debe ser uno de {_VALID_CHANNELS}"
            )
        if self.soak_seconds <= 0 or self.poll_interval <= 0:
            raise ValueError("soak_seconds y poll_interval deben ser > 0")
        if self.min_releases_retained < 2:
            raise ValueError("min_releases_retained debe ser >= 2 (nunca borrar last_good)")
        return self


def _coerce(name, raw, current_value):
    """Convierte un string (de TOML-string o env) al tipo del campo."""
    if isinstance(current_value, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(raw)
    if isinstance(current_value, float):
        return float(raw)
    if isinstance(current_value, list):
        if isinstance(raw, list):
            return list(raw)
        return [p for p in str(raw).split(",") if p]
    return raw


def load_config(path=None, env=None):
    """Carga la config desde TOML local (si existe) + overrides de entorno.

    `path` por defecto = `$CAM_COUNTER_OTA_CONFIG` o `<install_root>/shared/agent.toml`.
    Los overrides de entorno tienen prioridad sobre el fichero.
    """
    env = os.environ if env is None else env
    cfg = AgentConfig()

    cfg_path = path or env.get(_ENV_PREFIX + "CONFIG")
    if cfg_path is None:
        cfg_path = os.path.join(cfg.install_root, "shared", "agent.toml")

    data = {}
    if cfg_path and os.path.isfile(cfg_path):
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

    field_names = {f.name for f in fields(cfg)}
    for key, value in data.items():
        if key in field_names:
            setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))

    # Overrides de entorno CAM_COUNTER_OTA_<FIELD>.
    for name in field_names:
        env_key = _ENV_PREFIX + name.upper()
        if env_key in env:
            setattr(cfg, name, _coerce(name, env[env_key], getattr(cfg, name)))

    return cfg.validate()
