#!/usr/bin/env python3
"""Valida un channel-manifest contra `contracts/channel_manifest.schema.json`.

Modos:
  - `--manifest path.json`  valida ESE manifiesto.
  - sin `--manifest`         genera un manifiesto de EJEMPLO canónico y lo valida (útil en CI
                             para demostrar que el schema acepta la forma canónica).

Sin dependencias salvo `jsonschema` (ya presente en el entorno de CI/borde).
"""
import argparse
import json
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_SCHEMA = os.path.join(_REPO_ROOT, "contracts", "channel_manifest.schema.json")


def example_manifest(channel="canary", version="0.1.0", sequence=1):
    """Manifiesto canónico de ejemplo (forma de la sección 1 del contrato)."""
    return {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "sequence": sequence,
        "artifact": {
            "key": f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz",
            "sha256": "0" * 64,
            "size_bytes": 1234,
            "sig_key": f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz.minisig",
        },
        "native_blob": {
            "key": "native/box64-sysroot-arm64.tar.gz",
            "sha256": "0" * 64,
        },
        "min_agent_version": "0.1.0",
        "released_at": "2026-01-01T00:00:00Z",
        "released_by": "github-actions",
        "git_sha": "0" * 7,
        "previous_version": None,
    }


def load_schema(schema_path):
    with open(schema_path, encoding="utf-8") as fh:
        return json.load(fh)


def validate(manifest, schema):
    """Lanza jsonschema.ValidationError si no valida. Devuelve None si OK."""
    import jsonschema

    jsonschema.validate(instance=manifest, schema=schema)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=_DEFAULT_SCHEMA,
                        help="ruta del JSON Schema del channel-manifest.")
    parser.add_argument("--manifest", help="ruta del manifiesto a validar (o ejemplo si se omite).")
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)

    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        label = args.manifest
    else:
        manifest = example_manifest()
        label = "<ejemplo canónico>"

    try:
        validate(manifest, schema)
    except Exception as exc:  # noqa: BLE001
        print(f"INVÁLIDO: {label}: {exc}", file=sys.stderr)
        return 1
    print(f"VÁLIDO: {label} (channel={manifest.get('channel')} version={manifest.get('version')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
