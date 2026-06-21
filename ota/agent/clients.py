"""Implementaciones concretas (producción) de las interfaces del agente.

boto3 se importa de forma PEREZOSA (sólo al construir los clientes reales), de modo que la
suite de tests x86 —que usa fakes— no requiere boto3 instalado.
"""
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

from .interfaces import HealthUnavailable


# ─────────────────────────────── S3 (SigV4, NUNCA presigned) ───────────────────────────────
class S3ObjectStore:
    """Lee objetos del bucket de releases vía boto3 (firma SigV4/IAM). Sin presigned URLs."""

    def __init__(self, bucket, region="us-east-1", client=None):
        self.bucket = bucket
        if client is not None:
            self._client = client
        else:
            import boto3  # import perezoso: sólo en producción

            self._client = boto3.client("s3", region_name=region)

    def get_bytes(self, key):
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()


# ─────────────────────────────────────── systemd ───────────────────────────────────────────
class SystemctlController:
    """Controla el servicio de producto vía `systemctl`."""

    def __init__(self, runner=None):
        # runner(args:list)->CompletedProcess; inyectable para tests.
        self._run = runner or self._default_run

    @staticmethod
    def _default_run(args):
        return subprocess.run(args, capture_output=True, text=True, check=False)

    def restart(self, name):
        self._run(["systemctl", "restart", name])

    def is_active(self, name):
        proc = self._run(["systemctl", "is-active", name])
        return proc.stdout.strip() == "active"

    def n_restarts(self, name):
        proc = self._run(["systemctl", "show", name, "--property=NRestarts", "--value"])
        try:
            return int(proc.stdout.strip() or "0")
        except ValueError:
            return 0


# ─────────────────────────────── HTTP health (/api/health) ─────────────────────────────────
class HttpHealthProbe:
    """GET del endpoint de salud de PRODUCTO. Lanza HealthUnavailable si no-200/no-conecta."""

    def __init__(self, url, timeout=5.0, opener=None):
        self.url = url
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def get(self):
        try:
            with self._opener(self.url, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de red == no saludable
            raise HealthUnavailable(f"health no disponible: {exc}") from exc
        if status != 200:
            raise HealthUnavailable(f"health devolvió HTTP {status}")
        return json.loads(body)


# ───────────────────────────────────────── reloj ───────────────────────────────────────────
class RealClock:
    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def now_iso(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────── registry (DynamoDB heartbeat) ──────────────────────────────
class DynamoRegistry:
    """Heartbeat al device-registry `cam-counter-devices` (UpdateItem). NUNCA lee desired."""

    # Sólo escribe estos campos; JAMÁS desired_version (lo refleja la nube).
    _ALLOWED = {
        "reported_version",
        "last_good_version",
        "last_update_status",
        "last_update_error",
        "last_seen_at",
        "status",
        "agent_version",
        "site_id",
    }

    def __init__(self, table_name="cam-counter-devices", region="us-east-1",
                 device_id=None, client=None):
        self.table_name = table_name
        self.device_id = device_id
        if client is not None:
            self._client = client
        else:
            import boto3  # import perezoso

            self._client = boto3.client("dynamodb", region_name=region)

    def heartbeat(self, **fields):
        device_id = fields.pop("device_id", self.device_id)
        if not device_id:
            raise ValueError("device_id requerido para el heartbeat")
        names, values, sets = {}, {}, []
        for k, v in fields.items():
            if k not in self._ALLOWED or v is None:
                continue
            names[f"#{k}"] = k
            values[f":{k}"] = {"S": str(v)}
            sets.append(f"#{k} = :{k}")
        if not sets:
            return
        self._client.update_item(
            TableName=self.table_name,
            Key={"PK": {"S": f"DEVICE#{device_id}"}},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
