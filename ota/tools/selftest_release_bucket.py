#!/usr/bin/env python3
"""Self-test del bucket de releases REAL: publicar -> validar -> leer de vuelta -> LIMPIAR.

Demuestra, contra el bucket `cam-counter-fleet-releases-950639281773` YA aplicado por el
runner, que el bloque "publicar release -> leer manifiesto del canal" funciona de verdad:

  (a) publica un artefacto de PRUEBA bajo `releases/_selftest/<runid>/...` (+ `.sha256` +
      `.minisig` firmado con una clave EFÍMERA de prueba — NUNCA la clave productiva de
      Secrets Manager);
  (b) escribe un channel-manifest de PRUEBA en `channels/_selftest/manifest.json` (clave de
      PRUEBA, jamás los productivos `channels/canary|stable/manifest.json`);
  (c) valida ese manifiesto contra `contracts/channel_manifest.schema.json`;
  (d) lo lee de vuelta (HeadObject + GetObject), confirma sha256 del artefacto y la igualdad
      del manifiesto leído vs. escrito; opcionalmente prueba la lectura con el rol per-Pi
      (least-privilege) para validar que el IAM acotado funciona;
  (e) LIMPIA todos los objetos de prueba (DeleteObject) y verifica con HeadObject que ya no
      existen — no contamina el bucket.

GATED por credenciales: SIN credenciales (o sin boto3) -> SKIP con aviso (exit 0), nunca rojo
en CI sin OIDC. CON credenciales en el runner, un SKIP NO es aceptable: se exige un PASS real
(modo strict por defecto cuando hay credenciales).

NOTA sobre `channel`: el schema exige `channel ∈ {canary, stable}`. El manifiesto de prueba
usa el valor de enum válido `canary` en su CONTENIDO (para que valide) pero se almacena bajo
la CLAVE de prueba `channels/_selftest/manifest.json`; los objetos productivos
`channels/canary/manifest.json` y `channels/stable/manifest.json` NO se tocan.
"""
import argparse
import hashlib
import json
import os
import sys
import time

# Hacer importables `agent` (minisign) y `tools` (validate_manifest) desde ota/.
_OTA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_OTA_DIR, ".."))
if _OTA_DIR not in sys.path:
    sys.path.insert(0, _OTA_DIR)

from agent import minisign  # noqa: E402
from tools import validate_manifest  # noqa: E402

_SELFTEST_PREFIX = "_selftest"
_TEST_CHANNEL_KEY = f"channels/{_SELFTEST_PREFIX}/manifest.json"


def _have_credentials():
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False, "boto3 no instalado"
    try:
        import boto3

        ident = boto3.client("sts").get_caller_identity()
        return True, ident.get("Arn", "?")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _resolve_edge_role_arn():
    """Lee `edge_role_arn` del output de terraform (fuente canónica). None si no resuelve."""
    import subprocess

    try:
        out = subprocess.run(
            ["terraform", f"-chdir={_REPO_ROOT}/terraform/environments/prod",
             "output", "-raw", "edge_role_arn"],
            capture_output=True, text=True, check=False,
        )
        arn = out.stdout.strip()
        return arn if arn.startswith("arn:aws:iam::") else None
    except Exception:  # noqa: BLE001
        return None


def _s3_for_role(role_arn, region):
    """Devuelve un cliente S3 con credenciales asumidas del rol dado."""
    import boto3

    sts = boto3.client("sts", region_name=region)
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="cam-counter-selftest-read")[
        "Credentials"
    ]
    return boto3.client(
        "s3", region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _head_exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_selftest(bucket, region="us-east-1", cleanup=True, read_role_arn=None,
                 require_read_role=False):
    import boto3

    s3 = boto3.client("s3", region_name=region)
    runid = f"{int(time.time())}-{os.urandom(4).hex()}"
    base = f"releases/{_SELFTEST_PREFIX}/{runid}"
    art_key = f"{base}/cam-counter-edge-selftest-arm64.tar.gz"
    sha_key = f"{art_key}.sha256"
    sig_key = f"{art_key}.minisig"
    keys = [art_key, sha_key, sig_key, _TEST_CHANNEL_KEY]

    print(f"[selftest] bucket={bucket} runid={runid}")

    # (a) artefacto de prueba + sha256 + firma minisign efímera.
    artifact = b"cam-counter selftest artifact " + runid.encode() + b"\n"
    sha256 = hashlib.sha256(artifact).hexdigest()
    pub_text, secret = minisign.generate_keypair(comment="cam-counter selftest ephemeral key")
    sig_text = minisign.sign(artifact, secret, trusted_comment=f"selftest {runid}")
    assert minisign.verify(artifact, sig_text, pub_text), "la firma efímera debe verificar localmente"

    try:
        s3.put_object(Bucket=bucket, Key=art_key, Body=artifact,
                      ContentType="application/gzip")
        s3.put_object(Bucket=bucket, Key=sha_key,
                      Body=f"{sha256}  {os.path.basename(art_key)}\n".encode(),
                      ContentType="text/plain")
        s3.put_object(Bucket=bucket, Key=sig_key, Body=sig_text.encode(),
                      ContentType="text/plain")
        print(f"[selftest] (a) PutObject artefacto+sha256+minisig OK ({len(artifact)} bytes)")

        # (b) channel-manifest de prueba (channel enum válido 'canary'; clave _selftest).
        manifest = {
            "schema_version": 1,
            "channel": "canary",
            "version": "0.0.0-selftest",
            "sequence": 1,
            "artifact": {
                "key": art_key, "sha256": sha256,
                "size_bytes": len(artifact), "sig_key": sig_key,
            },
            "min_agent_version": "0.1.0",
            "released_at": "2026-01-01T00:00:00Z",
            "released_by": "selftest",
            "git_sha": "0000000",
            "previous_version": None,
        }
        # (c) valida contra el schema.
        schema = validate_manifest.load_schema(validate_manifest._DEFAULT_SCHEMA)
        validate_manifest.validate(manifest, schema)
        print("[selftest] (c) manifiesto VÁLIDO contra channel_manifest.schema.json")

        s3.put_object(Bucket=bucket, Key=_TEST_CHANNEL_KEY,
                      Body=json.dumps(manifest, indent=2, sort_keys=True).encode(),
                      ContentType="application/json")
        print(f"[selftest] (b) PutObject manifiesto de prueba OK -> {_TEST_CHANNEL_KEY}")

        # (d) leer de vuelta (con rol per-Pi si se pidió, para validar least-privilege).
        reader = s3
        reader_label = "runner-env-creds"
        if read_role_arn:
            try:
                reader = _s3_for_role(read_role_arn, region)
                reader_label = f"per-Pi role {read_role_arn.split('/')[-1]}"
            except Exception as exc:  # noqa: BLE001
                if require_read_role:
                    raise RuntimeError(
                        f"integración real con rol per-Pi habilitada pero no se pudo asumir "
                        f"{read_role_arn}: {exc} (FALLO de configuración, sin fallback silencioso)"
                    ) from exc
                print(f"[selftest] aviso: no se pudo asumir {read_role_arn} ({exc}); "
                      f"se lee con creds del runner.")

        for k in keys:
            if not _head_exists(reader, bucket, k):
                raise RuntimeError(f"HeadObject falló para {k} (lector={reader_label})")
        got = reader.get_object(Bucket=bucket, Key=art_key)["Body"].read()
        if hashlib.sha256(got).hexdigest() != sha256:
            raise RuntimeError("sha256 del artefacto descargado != publicado")
        got_manifest = json.loads(
            reader.get_object(Bucket=bucket, Key=_TEST_CHANNEL_KEY)["Body"].read()
        )
        if got_manifest != manifest:
            raise RuntimeError("el manifiesto leído != escrito")
        print(f"[selftest] (d) read-back OK (lector={reader_label}; sha256 y manifiesto coinciden)")

        ok = True
    finally:
        # (e) LIMPIA siempre (incluso si algo falló a mitad).
        if cleanup:
            for k in keys:
                try:
                    s3.delete_object(Bucket=bucket, Key=k)
                except Exception as exc:  # noqa: BLE001
                    print(f"[selftest] aviso: no se pudo borrar {k}: {exc}")
            # Verifica que ya no existen.
            leftover = [k for k in keys if _head_exists(s3, bucket, k)]
            if leftover:
                raise RuntimeError(f"limpieza incompleta, quedan objetos: {leftover}")
            print("[selftest] (e) cleanup OK (HeadObject confirma que ya no existen)")

    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="cam-counter-fleet-releases-950639281773")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--cleanup", action="store_true", default=True,
                        help="borra los objetos de prueba al final (default: on).")
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false")
    parser.add_argument("--read-role-arn",
                        help="ARN del rol per-Pi para validar la lectura least-privilege "
                             "(default: edge_role_arn del output de terraform si resuelve).")
    parser.add_argument("--no-read-role", action="store_true",
                        help="no intentar leer con el rol per-Pi (sólo creds del runner).")
    parser.add_argument("--require-read-role", action="store_true",
                        help="exige asumir el rol per-Pi; si no resuelve -> FALLO de config.")
    args = parser.parse_args(argv)

    have, info = _have_credentials()
    if not have:
        print(f"[selftest] SKIP: sin credenciales AWS ({info}). El RUNNER ejecuta este "
              f"self-test con las credenciales de su entorno; CI sin OIDC queda en skip.")
        return 0

    print(f"[selftest] credenciales presentes: {info}")
    read_role_arn = None
    if not args.no_read_role:
        read_role_arn = args.read_role_arn or _resolve_edge_role_arn()
        if read_role_arn:
            print(f"[selftest] rol per-Pi para read-back: {read_role_arn}")
        elif args.require_read_role:
            print("[selftest] FALLO: --require-read-role pero no se resolvió edge_role_arn",
                  file=sys.stderr)
            return 1

    try:
        run_selftest(args.bucket, region=args.region, cleanup=args.cleanup,
                     read_role_arn=read_role_arn, require_read_role=args.require_read_role)
    except Exception as exc:  # noqa: BLE001
        print(f"[selftest] FAIL: {exc}", file=sys.stderr)
        return 1
    print("[selftest] PASS: publicar -> validar -> leer -> limpiar OK contra AWS real.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
