"""Entrypoint del update-agent: `python3 -m agent` (o el wrapper del .service systemd).

Construye las implementaciones de PRODUCCIÓN (boto3 SigV4 / systemctl / urllib / DynamoDB)
desde la config local y ejecuta UN ciclo de reconciliación. El timer systemd lo dispara
periódicamente con jitter (`Persistent=true` recupera el run perdido tras estar offline).
"""
import argparse
import sys

from .agent import UpdateAgent
from .clients import (
    DynamoRegistry,
    HttpHealthProbe,
    RealClock,
    S3ObjectStore,
    SystemctlController,
)
from .config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="cam-counter OTA update-agent (un ciclo).")
    parser.add_argument("--config", help="ruta del agent.toml (default: shared/agent.toml).")
    parser.add_argument("--dry-run", action="store_true",
                        help="resuelve desired vs current y sale sin instalar.")
    parser.add_argument("--no-registry", action="store_true",
                        help="no enviar heartbeat al device-registry.")
    args = parser.parse_args(argv)

    cfg = load_config(path=args.config)

    store = S3ObjectStore(cfg.bucket, region=cfg.region)
    service = SystemctlController()
    probe = HttpHealthProbe(cfg.health_url)
    clock = RealClock()
    registry = None if args.no_registry else DynamoRegistry(
        region=cfg.region, device_id=cfg.device_id
    )

    agent = UpdateAgent(cfg, store, service, probe, clock, registry=registry)

    if args.dry_run:
        agent.installer.discard_part_files()
        manifest = agent._fetch_manifest()  # noqa: SLF001 - util de diagnóstico
        current = agent.installer.current_version()
        print(f"canal={cfg.channel} desired={manifest.get('version')} current={current}")
        return 0

    result = agent.run_once()
    print(result)
    # Outcomes de fallo "duro" devuelven exit != 0 para que systemd lo registre.
    return 0 if result.outcome in ("noop", "updated", "skipped_failed_marker",
                                   "skipped_min_agent_version") else 1


if __name__ == "__main__":
    sys.exit(main())
