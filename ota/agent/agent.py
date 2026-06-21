"""Orquestador del update-agent: reconcilia desired (manifiesto S3) vs current e instala.

Flujo (manifiesto = ÚNICA fuente de la versión deseada):
  0) al arranque: descarta ficheros `.part` (descargas/extracciones a medias).
  1) lee el canal de config LOCAL; GET `channels/<channel>/manifest.json` vía SigV4.
  2) si desired == current -> NOOP (heartbeat healthy).
  3) si desired tiene failed-marker -> SKIP (no se reintenta esa versión).
  4) descarga artefacto + `.sha256` + `.minisig`; verifica **sha256 PRIMERO**, **firma
     minisign DESPUÉS** contra la pubkey fijada. Falla -> NO instala.
  5) instala atómico (temp-extract + rename) + swap de symlink; reinicia el servicio.
  6) health-check de PRODUCTO con SOAK. Éxito -> last_good=desired, heartbeat healthy.
  7) fallo -> rollback a last_good, failed-marker por versión, heartbeat rolled_back.

NUNCA lee `desired_version` del registry. NUNCA usa presigned URLs. NUNCA borra last_good.
Offline-tolerante: un solo salto a la versión current del canal (sin cola de versiones).
"""
import hashlib
import json

from . import minisign
from .health import run_soak
from .installer import Installer
from .interfaces import HealthUnavailable  # noqa: F401 (documenta el contrato)


class Outcome:
    NOOP = "noop"
    UPDATED = "updated"
    ROLLED_BACK = "rolled_back"
    SKIPPED_FAILED = "skipped_failed_marker"
    SKIPPED_MIN_AGENT = "skipped_min_agent_version"
    VERIFY_FAILED = "verify_failed"
    ERROR = "error"


class AgentResult:
    def __init__(self, outcome, version=None, reason=""):
        self.outcome = outcome
        self.version = version
        self.reason = reason

    def __repr__(self):
        return f"AgentResult({self.outcome!r}, version={self.version!r}, reason={self.reason!r})"


def _semver_key(v):
    """Clave de orden SemVer simplificada (core x.y.z; prerelease < release)."""
    core = v.lstrip("v").split("+", 1)[0]
    main, _, pre = core.partition("-")
    parts = []
    for p in main.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    # release (sin prerelease) ordena por encima del prerelease del mismo core.
    return (parts[0], parts[1], parts[2], 1 if pre == "" else 0, pre)


class UpdateAgent:
    def __init__(self, cfg, store, service, probe, clock, registry=None,
                 installer=None, verify_fn=None):
        self.cfg = cfg
        self.store = store
        self.service = service
        self.probe = probe
        self.clock = clock
        self.registry = registry
        self.installer = installer or Installer(cfg)
        self._verify = verify_fn or minisign.verify
        with open(cfg.resolved_pubkey_path, encoding="utf-8") as fh:
            self._pubkey_text = fh.read()

    # ── heartbeat (best-effort; nunca tumba la actualización) ──────────────────
    def _beat(self, **fields):
        if self.registry is None:
            return
        fields.setdefault("device_id", self.cfg.device_id)
        fields.setdefault("agent_version", self.cfg.agent_version)
        fields.setdefault("site_id", self.cfg.site_id)
        fields.setdefault("last_seen_at", self.clock.now_iso())
        try:
            self.registry.heartbeat(**fields)
        except Exception:  # noqa: BLE001 - el registry es espejo; jamás bloquea el update
            pass

    def _fetch_manifest(self):
        raw = self.store.get_bytes(self.cfg.channel_manifest_key())
        return json.loads(raw)

    def run_once(self):
        # (0) limpieza de .part al arranque del agente.
        self.installer.discard_part_files()
        current = self.installer.current_version()

        try:
            manifest = self._fetch_manifest()
        except Exception as exc:  # noqa: BLE001 - offline: heartbeat y salir sin error duro
            self._beat(reported_version=current, status="offline", last_update_status="idle")
            return AgentResult(Outcome.ERROR, version=current,
                               reason=f"no se pudo leer el manifiesto: {exc}")

        desired = manifest.get("version")
        if not desired:
            return AgentResult(Outcome.ERROR, reason="manifiesto sin 'version'")

        # (2) ya en la versión deseada.
        if desired == current:
            self._beat(reported_version=current, status="online",
                       last_update_status="healthy", last_good_version=self.installer.last_good())
            return AgentResult(Outcome.NOOP, version=current, reason="ya en desired")

        # min_agent_version gate.
        min_agent = manifest.get("min_agent_version")
        if min_agent and _semver_key(self.cfg.agent_version) < _semver_key(min_agent):
            self._beat(reported_version=current, status="degraded",
                       last_update_status="failed",
                       last_update_error=f"agent {self.cfg.agent_version} < min {min_agent}")
            return AgentResult(Outcome.SKIPPED_MIN_AGENT, version=desired,
                               reason=f"agent_version < min_agent_version {min_agent}")

        # (3) failed-marker: no se reintenta esa versión.
        if self.installer.is_failed(desired):
            self._beat(reported_version=current, status="degraded",
                       last_update_status="failed",
                       last_update_error=f"versión {desired} marcada como fallida")
            return AgentResult(Outcome.SKIPPED_FAILED, version=desired,
                               reason="failed-marker presente")

        # (4) descarga + verificación sha256 LUEGO firma.
        self._beat(reported_version=current, status="updating",
                   last_update_status="downloading")
        artifact_meta = manifest["artifact"]
        try:
            blob = self.store.get_bytes(artifact_meta["key"])
            sig_text = self.store.get_bytes(artifact_meta["sig_key"]).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            self._beat(reported_version=current, status="degraded",
                       last_update_status="failed", last_update_error=str(exc))
            return AgentResult(Outcome.ERROR, version=desired,
                               reason=f"descarga fallida: {exc}")

        self._beat(reported_version=current, status="updating",
                   last_update_status="verifying")
        digest = hashlib.sha256(blob).hexdigest()
        if digest != artifact_meta["sha256"]:
            self._beat(reported_version=current, status="degraded",
                       last_update_status="failed",
                       last_update_error="sha256 mismatch")
            self.installer.mark_failed(desired, "sha256 mismatch")
            return AgentResult(Outcome.VERIFY_FAILED, version=desired,
                               reason="sha256 no coincide")

        if not self._verify(blob, sig_text, self._pubkey_text):
            self._beat(reported_version=current, status="degraded",
                       last_update_status="failed",
                       last_update_error="minisign verify failed")
            self.installer.mark_failed(desired, "minisign verify failed")
            return AgentResult(Outcome.VERIFY_FAILED, version=desired,
                               reason="firma minisign inválida")

        # (5) instalación atómica + activación + restart.
        self._beat(reported_version=current, status="updating",
                   last_update_status="activating")
        try:
            self.installer.install_atomic(desired, blob)
            self.installer.activate(desired)
        except Exception as exc:  # noqa: BLE001
            self._rollback(current, desired, f"instalación fallida: {exc}")
            return AgentResult(Outcome.ROLLED_BACK, version=desired,
                               reason=f"instalación fallida: {exc}")

        self.service.restart(self.cfg.service_name)

        # (6) soak health-check de PRODUCTO.
        result = run_soak(self.probe, self.service, self.clock, self.cfg, desired)
        if result.ok:
            self.installer.set_last_good(desired)
            self.installer.prune_old_releases()
            self._beat(reported_version=desired, status="online",
                       last_update_status="healthy", last_good_version=desired,
                       last_update_error=None)
            return AgentResult(Outcome.UPDATED, version=desired, reason="soak ok")

        # (7) fallo del soak -> rollback a last_good.
        self._rollback(current, desired, result.reason)
        return AgentResult(Outcome.ROLLED_BACK, version=desired, reason=result.reason)

    def _rollback(self, current, failed_version, reason):
        """Revierte el symlink a last_good, reinicia, marca la versión fallida. Nunca borra last_good."""
        target = self.installer.last_good() or current
        if target and target != failed_version:
            try:
                self.installer.rollback(target)
                self.service.restart(self.cfg.service_name)
            except Exception:  # noqa: BLE001
                pass
        self.installer.mark_failed(failed_version, reason)
        self._beat(reported_version=target, status="degraded",
                   last_update_status="rolled_back", last_good_version=target,
                   last_update_error=reason)
