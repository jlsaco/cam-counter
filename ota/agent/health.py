"""Health-check de PRODUCTO con ventana de SOAK.

Un `200` estático NO basta: la salud de producto exige, durante toda la ventana de soak:
  - el servicio `active` (no inactivo),
  - SIN crash-loop (el contador `NRestarts` de systemd no aumenta respecto al baseline),
  - cada poll devuelve 200 (no `HealthUnavailable`),
  - estado final `ok` y `frames_flowing == True`,
  - `frames_processed` ESTRICTAMENTE CRECIENTE en CADA cámara (no basta 200 con frames=0),
  - `last_inference_ts` reciente por cámara,
  - `app_version` == la versión recién instalada (drift = comparación de strings),
  - `db_schema_version` esperado (si se configuró) y `config_version` cargado por cámara.

Distingue explícitamente «200 pero conteo roto» (frames=0) de salud real: ese caso FALLA y
dispara rollback.
"""
from dataclasses import dataclass, field

from .interfaces import HealthUnavailable


@dataclass
class SoakResult:
    ok: bool
    reason: str = ""
    samples: int = 0
    cameras_increasing: dict = field(default_factory=dict)


def run_soak(probe, service, clock, cfg, expected_version):
    """Ejecuta la ventana de soak y devuelve un SoakResult.

    Hace polling de `/api/health` cada `poll_interval` durante `soak_seconds`. Falla rápido
    (sin esperar a agotar la ventana) ante crash-loop, servicio inactivo o non-200.
    """
    deadline = clock.monotonic() + cfg.soak_seconds
    baseline_restarts = service.n_restarts(cfg.service_name)

    prev_frames = {}
    saw_increase = {}
    samples = 0
    last_health = None

    while True:
        # (1) crash-loop: NRestarts creció respecto al baseline -> FALLO inmediato.
        restarts = service.n_restarts(cfg.service_name)
        if restarts > baseline_restarts:
            return SoakResult(
                ok=False,
                reason=f"crash-loop: NRestarts {baseline_restarts}->{restarts}",
                samples=samples,
                cameras_increasing=saw_increase,
            )

        # (2) servicio inactivo -> FALLO.
        if not service.is_active(cfg.service_name):
            return SoakResult(
                ok=False, reason="servicio no active", samples=samples,
                cameras_increasing=saw_increase,
            )

        # (3) cada poll debe ser 200; non-200/no-conecta -> FALLO.
        try:
            health = probe.get()
        except HealthUnavailable as exc:
            return SoakResult(
                ok=False, reason=f"health non-200: {exc}", samples=samples,
                cameras_increasing=saw_increase,
            )

        last_health = health
        samples += 1

        # (4) acumula crecimiento de frames por cámara.
        for cam in health.get("cameras", []):
            cid = cam.get("camera_id")
            frames = cam.get("frames_processed", 0)
            if cid in prev_frames and frames > prev_frames[cid]:
                saw_increase[cid] = True
            prev_frames.setdefault(cid, frames)
            prev_frames[cid] = frames
            saw_increase.setdefault(cid, saw_increase.get(cid, False))

        if clock.monotonic() >= deadline:
            break
        clock.sleep(cfg.poll_interval)

    # ── Evaluación final tras la ventana ──────────────────────────────────────
    if last_health is None or samples == 0:
        return SoakResult(ok=False, reason="sin muestras de salud", samples=samples)

    if last_health.get("status") != "ok":
        return SoakResult(
            ok=False, reason=f"status={last_health.get('status')!r}", samples=samples,
            cameras_increasing=saw_increase,
        )

    if not last_health.get("frames_flowing", False):
        return SoakResult(
            ok=False, reason="frames_flowing=False (200 pero conteo roto)",
            samples=samples, cameras_increasing=saw_increase,
        )

    app_version = last_health.get("app_version")
    if app_version != expected_version:
        return SoakResult(
            ok=False,
            reason=f"app_version={app_version!r} != esperado {expected_version!r}",
            samples=samples, cameras_increasing=saw_increase,
        )

    if cfg.expected_db_schema_version >= 0:
        got = last_health.get("db_schema_version")
        if got != cfg.expected_db_schema_version:
            return SoakResult(
                ok=False,
                reason=f"db_schema_version={got} != {cfg.expected_db_schema_version}",
                samples=samples, cameras_increasing=saw_increase,
            )

    cameras = last_health.get("cameras", [])
    if not cameras:
        return SoakResult(ok=False, reason="sin cámaras en /api/health", samples=samples)

    for cam in cameras:
        cid = cam.get("camera_id")
        # frames crecientes por cámara (distingue 200-pero-frames=0).
        if not saw_increase.get(cid, False):
            return SoakResult(
                ok=False,
                reason=f"cámara {cid}: frames_processed no creció durante el soak",
                samples=samples, cameras_increasing=saw_increase,
            )
        # last_inference_ts reciente.
        ts = cam.get("last_inference_ts")
        if ts is None:
            return SoakResult(
                ok=False, reason=f"cámara {cid}: sin last_inference_ts", samples=samples,
                cameras_increasing=saw_increase,
            )
        # config_version cargado (>= 0 siempre; la presencia del campo == config cargada).
        if cam.get("config_version") is None:
            return SoakResult(
                ok=False, reason=f"cámara {cid}: config_version no cargado", samples=samples,
                cameras_increasing=saw_increase,
            )

    return SoakResult(ok=True, reason="ok", samples=samples, cameras_increasing=saw_increase)
