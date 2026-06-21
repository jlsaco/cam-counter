#!/usr/bin/env python3
"""Publica / repunta el `channels/<channel>/manifest.json` del bucket de releases.

Regla del **escritor único** con **concurrencia optimista S3 (ETag If-Match)** y **sequence
MONÓTONO**: lee el manifiesto actual del canal, incrementa `sequence`, fija
`previous_version`, y hace `PutObject` con `If-Match=<etag>` (o `If-None-Match=*` si es la
primera publicación del canal). Si el ETag cambió entre lectura y escritura (otro escritor),
S3 devuelve `PreconditionFailed` y la operación se rechaza (no se pisa el manifiesto).

Espeja `desired_version` en el device-registry (`cam-counter-devices`) para todos los
dispositivos del canal (vía GSI1) — es SÓLO observabilidad; el agente NUNCA lo lee.

Modos:
  - PUBLISH (default): publica una versión nueva con su artefacto/firma ya subidos a S3.
  - REPOINT-ONLY (`--repoint-only --from-channel <c>`): promueve una versión YA publicada de
    otro canal (p.ej. canary->stable) o rollback (`--version <previous>`), SIN reconstruir:
    copia la referencia del artefacto/firma del manifiesto origen.

Escritores ÚNICOS = los workflows release/promote (rol de deploy OIDC gated por Environment).
Publicar OBJETOS S3 NO es `terraform apply` de infra.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCHEMA = os.path.join(_REPO_ROOT, "contracts", "channel_manifest.schema.json")
_VALID_CHANNELS = ("canary", "stable")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_key(channel):
    return f"channels/{channel}/manifest.json"


def get_current(s3, bucket, channel):
    """Devuelve (manifest_dict|None, etag|None) del manifiesto actual del canal."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=manifest_key(channel))
    except Exception as exc:  # noqa: BLE001 - NoSuchKey u otros -> tratamos como "no existe"
        if _is_no_such_key(exc):
            return None, None
        raise
    body = resp["Body"].read()
    etag = resp.get("ETag")
    return json.loads(body), etag


def _is_no_such_key(exc):
    code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})("Error", {})
    if isinstance(code, dict) and code.get("Code") in ("NoSuchKey", "404"):
        return True
    return "NoSuchKey" in str(exc) or "Not Found" in str(exc)


def next_sequence(current):
    if current is None:
        return 1
    return int(current.get("sequence", 0)) + 1


def build_manifest(channel, version, artifact, native_blob, *, git_sha, released_by,
                   min_agent_version, sequence, previous_version, released_at=None):
    manifest = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "sequence": sequence,
        "artifact": artifact,
        "min_agent_version": min_agent_version,
        "released_at": released_at or _now_iso(),
        "released_by": released_by,
        "git_sha": git_sha,
        "previous_version": previous_version,
    }
    if native_blob:
        manifest["native_blob"] = native_blob
    return manifest


def validate_manifest(manifest, schema_path=_SCHEMA):
    import jsonschema

    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=manifest, schema=schema)


def put_manifest(s3, bucket, channel, manifest, etag):
    """PutObject con If-Match (update) o If-None-Match=* (create). Single-writer."""
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    extra = {"ContentType": "application/json"}
    if etag:
        extra["IfMatch"] = etag
    else:
        extra["IfNoneMatch"] = "*"
    return s3.put_object(Bucket=bucket, Key=manifest_key(channel), Body=body, **extra)


def mirror_desired_version(ddb, table, channel, version, gsi1="GSI1"):
    """Refleja desired_version en cada device del canal (vía GSI1). Sólo observabilidad."""
    updated = 0
    kwargs = {
        "TableName": table,
        "IndexName": gsi1,
        "KeyConditionExpression": "GSI1PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"CHANNEL#{channel}"}},
    }
    while True:
        resp = ddb.query(**kwargs)
        for item in resp.get("Items", []):
            pk = item["PK"]["S"]
            ddb.update_item(
                TableName=table,
                Key={"PK": {"S": pk}},
                UpdateExpression="SET desired_version = :v",
                ExpressionAttributeValues={":v": {"S": version}},
            )
            updated += 1
        if "LastEvaluatedKey" in resp:
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break
    return updated


def publish(s3, bucket, channel, version, artifact, native_blob, *, git_sha, released_by,
            min_agent_version, ddb=None, devices_table=None, released_at=None,
            dry_run=False):
    """Orquesta: lee actual -> bump sequence -> valida -> put If-Match -> espeja registry."""
    if channel not in _VALID_CHANNELS:
        raise ValueError(f"canal inválido {channel!r}")
    current, etag = get_current(s3, bucket, channel)
    sequence = next_sequence(current)
    previous_version = current.get("version") if current else None
    manifest = build_manifest(
        channel, version, artifact, native_blob, git_sha=git_sha,
        released_by=released_by, min_agent_version=min_agent_version,
        sequence=sequence, previous_version=previous_version, released_at=released_at,
    )
    validate_manifest(manifest)
    if dry_run:
        return manifest, {"dry_run": True}
    put_resp = put_manifest(s3, bucket, channel, manifest, etag)
    mirror = None
    if ddb is not None and devices_table:
        mirror = mirror_desired_version(ddb, devices_table, channel, version)
    return manifest, {"etag_used": etag, "sequence": sequence, "mirrored": mirror,
                      "put_etag": put_resp.get("ETag") if isinstance(put_resp, dict) else None}


def _artifact_from_args(args):
    return {
        "key": args.artifact_key,
        "sha256": args.artifact_sha256,
        "size_bytes": int(args.artifact_size),
        "sig_key": args.sig_key,
    }


def artifact_keys_for(version):
    """Claves canónicas del artefacto/firma/digest de una versión publicada."""
    base = f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz"
    return base, f"{base}.minisig", f"{base}.sha256"


def reconstruct_artifact(s3, bucket, version):
    """REPOINT-only: reconstruye la referencia del artefacto desde `releases/<version>/`.

    El artefacto es INDEPENDIENTE del canal (vive bajo `releases/<version>/`), así que tanto
    promover canary->stable como hacer rollback a una versión previa se reducen a apuntar el
    manifiesto del canal destino a un artefacto YA publicado, SIN reconstruirlo. Lee el sha256
    del `.sha256` y el tamaño vía HeadObject.
    """
    art_key, sig_key, sha_key = artifact_keys_for(version)
    sha_body = s3.get_object(Bucket=bucket, Key=sha_key)["Body"].read().decode("utf-8")
    sha256 = sha_body.strip().split()[0]
    size = s3.head_object(Bucket=bucket, Key=art_key)["ContentLength"]
    return {"key": art_key, "sha256": sha256, "size_bytes": int(size), "sig_key": sig_key}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--channel", required=True, choices=_VALID_CHANNELS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--released-by", default="github-actions")
    parser.add_argument("--min-agent-version", default="0.1.0")
    parser.add_argument("--region", default="us-east-1")
    # PUBLISH inputs.
    parser.add_argument("--artifact-key")
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--artifact-size")
    parser.add_argument("--sig-key")
    parser.add_argument("--native-blob-key")
    parser.add_argument("--native-blob-sha256")
    # REPOINT-only: promueve/rollback a una versión YA publicada (reconstruye desde
    # releases/<version>/), SIN rebuild. Sirve para promote canary->stable y para rollback.
    parser.add_argument("--repoint-only", action="store_true",
                        help="apunta el canal a una versión ya publicada (releases/<version>/), sin rebuild.")
    # Registry mirror.
    parser.add_argument("--mirror-registry", action="store_true")
    parser.add_argument("--devices-table", default="cam-counter-devices")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    ddb = boto3.client("dynamodb", region_name=args.region) if args.mirror_registry else None

    if args.repoint_only:
        # Reconstruye la referencia del artefacto desde releases/<version>/ (sin rebuild).
        try:
            artifact = reconstruct_artifact(s3, args.bucket, args.version)
        except Exception as exc:  # noqa: BLE001
            parser.error(
                f"no se pudo reconstruir el artefacto publicado de {args.version}: {exc}"
            )
        native_blob = None
    else:
        if not all([args.artifact_key, args.artifact_sha256, args.artifact_size, args.sig_key]):
            parser.error("PUBLISH requiere --artifact-key/--artifact-sha256/--artifact-size/--sig-key")
        artifact = _artifact_from_args(args)
        native_blob = None
        if args.native_blob_key and args.native_blob_sha256:
            native_blob = {"key": args.native_blob_key, "sha256": args.native_blob_sha256}

    manifest, info = publish(
        s3, args.bucket, args.channel, args.version, artifact, native_blob,
        git_sha=args.git_sha, released_by=args.released_by,
        min_agent_version=args.min_agent_version, ddb=ddb,
        devices_table=args.devices_table if args.mirror_registry else None,
        dry_run=args.dry_run,
    )
    print(json.dumps({"manifest": manifest, "info": info}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
