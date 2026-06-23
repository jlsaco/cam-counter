# Lambdas (ingesta + dashboard)

Diseño serverless para cam-counter. Cuenta `950639281773`, region `us-east-1`, runtime **Python 3.12** (arm64). Todo aditivo/monótono (MAD `terraform apply -auto-approve`, se aborta ante destroy/replace). Edge-first: la nube es best-effort; el conteo nunca depende de estas funciones. Idempotencia: `event_id` determinista (sha1) + conditional put.

> Nota sobre naming: los specs `iot-topology` y `security` divergen en algunos nombres. Este diseño fija el set canónico abajo y lo justifica; el resto se deriva de él 1:1.

---

## 0. Inventario de funciones

| Lambda | Trigger | Propósito | Estado |
|---|---|---|---|
| `cam-counter-events-ingest` | IoT Rule `cam_counter_crossing_events` | Valida `CrossingEvent`, conditional put en `cam-counter-events`, enlaza `clip_key` | núcleo (1) |
| `cam-counter-device-status` | IoT Rules `cam_counter_device_status` + `cam_counter_device_lwt` (+ lifecycle) | Heartbeat / online / offline-LWT → `cam-counter-devices` | núcleo (1) |
| `cam-counter-clip-presign` | API Gateway HTTP (Cognito JWT authorizer) | Presigned **GET** del clip acotado a `clip_key` del evento | dashboard (2) |
| `cam-counter-clip-presign-put` | API Gateway HTTP (Cognito JWT) — **OPCIONAL** | Presigned **PUT** si se elige modelo presign para subida del device | dashboard (2), no recomendado |

**Modelo de subida elegido:** el device sube el clip con **IoT Credentials Provider** (role alias → rol por device, `s3:PutObject` acotado a su prefijo), no con presigned PUT. Razón: reusa el cert mTLS (sin segundo secreto), soporta multipart/reintentos nativos de boto3 (edge-first), sin Lambda en el camino caliente de binarios grandes. `cam-counter-clip-presign-put` se entrega como variante documentada/feature-flag por si se prefiere presign, pero queda **desactivada por defecto**.

Naming canónico fijado: Lambdas en kebab `cam-counter-{dominio}-{accion}`; IoT Rules en snake_case `cam_counter_*` (las Rules solo admiten `[a-zA-Z0-9_]`).

---

## 1. `cam-counter-events-ingest` (INGESTA)

### 1.1 Contrato de entrada (IoT Rule → Lambda)

La Rule `cam_counter_crossing_events` enriquece el payload antes de invocar:

```sql
SELECT *,
       topic(2)    AS _device_id_topic,   -- cam-counter/{device_id}/events/crossing -> seg 2
       clientid()  AS _client_id,         -- == thingName forzado por policy
       timestamp() AS _ingest_ts_ms
FROM 'cam-counter/+/events/crossing'
```

La Lambda recibe el JSON del `CrossingEvent` + los campos `_device_id_topic`, `_client_id`, `_ingest_ts_ms`. Invocación **asíncrona** desde IoT (la Rule es el caller); el reintento y la DLQ son de la propia Lambda (async config), no de IoT.

### 1.2 Estructura del handler

```
lambdas/events_ingest/
├── handler.py              # lambda_handler(event, context)
├── validation.py           # jsonschema contra crossing_event.schema.json (embebido en build)
├── keys.py                 # PK/SK builders, clip_key validator
├── ddb.py                  # conditional put + UpdateItem helpers
├── schema/crossing_event.schema.json   # copia del contrato, horneada en el paquete
├── requirements.txt        # jsonschema (boto3 viene en el runtime)
└── tests/
```

```python
# handler.py  (esqueleto, idempotente y defensivo)
import json, os, time, logging
import boto3
from botocore.exceptions import ClientError
from validation import validate_crossing_event           # raises on invalid
from keys import build_pk, build_sk, looks_like_clip_key
from ddb import conditional_put_event

log = logging.getLogger(); log.setLevel(logging.INFO)
_ddb = boto3.resource("dynamodb")
TABLE = _ddb.Table(os.environ["EVENTS_TABLE"])            # cam-counter-events
EXPECTED_TOPIC_DEVICE = None  # se valida contra payload, no env

def lambda_handler(event, context):
    # 1) validar contra el contrato (jsonschema). Falla -> excepcion -> DLQ async.
    validate_crossing_event(event)                        # schema_version, event_id, slugs, ts_event_ms...

    # 2) anti-spoof defensa en profundidad: topic(2) == device_id; clientid == thing
    dev = event["device_id"]
    if event.get("_device_id_topic") not in (None, dev):
        raise ValueError(f"device_id mismatch topic={event['_device_id_topic']} payload={dev}")
    expected_client = f"cam-counter-{event['site_id']}-{dev}"   # thingName == clientid
    cid = event.get("_client_id")
    if cid is not None and cid != expected_client:
        raise ValueError(f"clientid spoof: {cid} != {expected_client}")

    # 3) clip_key acotado: debe empezar por media/{site}/{device}/{camera}/ y terminar en {event_id}.mp4
    ck = event.get("clip_key")
    if ck and not looks_like_clip_key(ck, event):
        raise ValueError(f"clip_key fuera de prefijo del device: {ck}")

    # 4) construir item + conditional put idempotente
    item = {
        "PK": build_pk(event["site_id"], dev, event["camera_id"]),     # CAM#site#device#camera
        "SK": build_sk(event["ts_event_ms"], event["event_id"]),       # TS#{ts:013d}#{event_id}
        "GSI1PK": f"SITE#{event['site_id']}",
        "GSI1SK": build_sk(event["ts_event_ms"], event["event_id"]),
        "event_id": event["event_id"],
        "site_id": event["site_id"], "device_id": dev, "camera_id": event["camera_id"],
        "ts_event_ms": event["ts_event_ms"],
        "direction": event["direction"], "count_delta": event["count_delta"],
        "confidence": event.get("confidence"),
        "line_config_version": event.get("line_config_version"),
        "clip_key": ck, "clip_status": event.get("clip_status", "pending"),
        "ingest_ts_ms": event.get("_ingest_ts_ms") or int(time.time() * 1000),
        "schema_version": event["schema_version"],
    }
    try:
        conditional_put_event(TABLE, item)                # attribute_not_exists(PK) AND attribute_not_exists(SK)
        log.info(json.dumps({"msg":"put","event_id":event["event_id"],"dup":False}))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # reintento del mismo event_id: NO es error. Idempotente. Opcional: reconciliar clip_status.
            log.info(json.dumps({"msg":"dup","event_id":event["event_id"],"dup":True}))
            _maybe_update_clip_status(item)               # UpdateItem condicional: pending -> uploaded
            return {"ok": True, "dup": True}
        raise                                             # otros errores -> reintento/DLQ
    return {"ok": True, "dup": False}
```

```python
# ddb.py
def conditional_put_event(table, item):
    table.put_item(
        Item={k: v for k, v in item.items() if v is not None},
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )

def _maybe_update_clip_status(table, item):
    # si el evento ya existia con clip_status=pending y este trae uploaded, avanzar (monótono)
    if item.get("clip_status") != "uploaded":
        return
    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET clip_status = :u",
        ConditionExpression="clip_status = :p",
        ExpressionAttributeValues={":u": "uploaded", ":p": "pending"},
    )
```

**Por qué este patrón = idempotencia:** mismo `event_id` ⇒ misma `SK` (`TS#{ts}#{event_id}`). El `ConditionExpression attribute_not_exists` rechaza el segundo put; `ConditionalCheckFailedException` se trata como éxito (no excepción que propague a DLQ). Esto soporta el **dual-write** de la Fase 1 de migración (device escribe directo + por MQTT) sin duplicar, y los reintentos del propio IoT/Lambda.

### 1.3 clip_key acotado (sin trust del payload)

`looks_like_clip_key` reconstruye el prefijo esperado y compara; nunca confía ciegamente:

```python
# keys.py
import re
SLUG = r"[a-z0-9][a-z0-9-]{1,62}"
def looks_like_clip_key(ck, ev):
    expect_prefix = f"media/{ev['site_id']}/{ev['device_id']}/{ev['camera_id']}/"
    return (ck.startswith(expect_prefix)
            and ck.endswith(f"/{ev['event_id']}.mp4")
            and ".." not in ck)
```

La Lambda **no** lee S3 (no necesita `GetObject` para ingestar): solo persiste el puntero. La existencia real del objeto se resuelve perezosamente cuando el dashboard pide la presigned GET.

### 1.4 Heartbeat / status → `cam-counter-device-status`

Función separada (no reusar rol). Triggers: `cam_counter_device_status` (online/heartbeat sobre `cam-counter/+/status/connection` y `.../telemetry/heartbeat`) y `cam_counter_device_lwt` (offline-LWT filtrado). Hace `UpdateItem` idempotente sobre `cam-counter-devices`:

```python
# device_status/handler.py (núcleo)
def lambda_handler(ev, ctx):
    dev = ev["device_id"]                      # de topic(2)
    now = ev.get("server_ts_ms") or int(time.time()*1000)
    expr = ["last_seen_ms = :now", "connection_status = :st"]
    vals = {":now": now, ":st": ev.get("status","online")}
    for k in ("fw_version","release_channel","line_config_version",
              "rtsp_ok","hailo_ok","cpu_temp_c","queue_depth","offline_reason"):
        if k in ev: expr.append(f"{k} = :{k}"); vals[f":{k}"] = ev[k]
    # GSI1 por canal, consistente con cam-counter-devices
    if "release_channel" in ev:
        expr.append("GSI1PK = :ch"); vals[":ch"] = f"CHANNEL#{ev['release_channel']}"
    # last-writer-wins por tiempo: no retroceder last_seen
    DEVICES.update_item(
        Key={"PK": f"DEVICE#{dev}"},
        UpdateExpression="SET " + ", ".join(expr),
        ConditionExpression="attribute_not_exists(last_seen_ms) OR last_seen_ms <= :now",
        ExpressionAttributeValues=vals,
    )
```

LWT/offline es un publish normal del broker al mismo topic `status/connection` con `status:"offline"`; el guard `last_seen_ms <= :now` evita que un mensaje retenido viejo pise un estado más nuevo.

### 1.5 DLQ y observabilidad

- **Invocación async** (IoT Rule → Lambda es async): configurar `aws_lambda_function_event_invoke_config` con `maximum_retry_attempts = 2`, `maximum_event_age_in_seconds = 3600`, y **on_failure → SQS `cam-counter-events-ingest-dlq`**. El mensaje malformado (falla jsonschema, spoof) acaba en la DLQ tras agotar reintentos.
- La **IoT Rule** además define `error_action` → CloudWatch Logs + re-publish a `cam-counter/$dlq/cam_counter_crossing_events` (cubre fallos antes de llegar a la Lambda, p. ej. throttling de invoke).
- **Observabilidad:** logs JSON estructurados (`event_id`, `dup`, `device_id`); métricas EMF embebidas o alarmas CloudWatch sobre `Errors`, `Throttles`, `IteratorAge` n/a (async), profundidad de la DLQ (`ApproximateNumberOfMessagesVisible > 0`). Alarma sobre `ConditionalCheckFailedException` **no** (es esperado). X-Ray opcional. Log group `/aws/lambda/cam-counter-events-ingest` con retención 30 días.
- Reproceso DLQ: redrive manual desde SQS una vez corregido el contrato/bug.

### 1.6 IAM role mínimo — `cam-counter-lambda-events-ingest-role`

Solo escritura en la tabla de eventos + DLQ + logs. **Sin** S3 (no lee el clip), sin `Query`/`Scan`/`DeleteItem`, sin otras tablas.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "WriteEventsOnly", "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-events" },
    { "Sid": "Dlq", "Effect": "Allow", "Action": ["sqs:SendMessage"],
      "Resource": "arn:aws:sqs:us-east-1:950639281773:cam-counter-events-ingest-dlq" },
    { "Sid": "Logs", "Effect": "Allow",
      "Action": ["logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:950639281773:log-group:/aws/lambda/cam-counter-events-ingest:*" }
  ]
}
```

`cam-counter-lambda-device-status-role`: análogo, `dynamodb:UpdateItem` sobre `cam-counter-devices` (+ su GSI1 si fuera necesario para condition, normalmente no) + DLQ propia + logs.

---

## 2. Lambdas de soporte del dashboard

### 2.1 `cam-counter-clip-presign` (presigned GET, recomendado)

Sirve a la app Next.js (Amplify). El browser **nunca** toca S3/DynamoDB directo: llama a `cam-counter-fleet-api` (API Gateway HTTP v2) con JWT de Cognito; el authorizer valida; la Lambda firma una GET de vida corta.

```
lambdas/clip_presign/
├── handler.py
├── authz.py          # extrae claims del JWT (ya validado por el authorizer), grupos cognito
└── requirements.txt  # boto3 del runtime; nada extra
```

```python
# clip_presign/handler.py
import json, os, re, boto3
s3 = boto3.client("s3", config=boto3.session.Config(signature_version="s3v4"))
BUCKET = os.environ["MEDIA_BUCKET"]           # cam-counter-media-950639281773
TTL = int(os.environ.get("PRESIGN_TTL", "300"))
KEY_RE = re.compile(r"^media/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[0-9a-f]+\.mp4$")

def lambda_handler(event, ctx):
    qs = event.get("queryStringParameters") or {}
    clip_key = qs.get("clip_key", "")
    # acotar: la URL solo puede apuntar a una clave media/ valida; nunca a otro objeto del bucket
    if not KEY_RE.match(clip_key) or ".." in clip_key:
        return _resp(400, {"error": "invalid clip_key"})
    # (opcional) verificar que el operador tiene acceso al site del clip via claims/grupos
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": clip_key, "ResponseContentType": "video/mp4"},
        ExpiresIn=TTL,
    )
    return _resp(200, {"url": url, "expires_in": TTL})

def _resp(code, body):
    return {"statusCode": code, "headers": {"content-type": "application/json"},
            "body": json.dumps(body)}
```

Garantías: la clave se valida contra un regex anclado al prefijo `media/` (no `..`, no otro bucket, no `fleet-releases`/`tfstate`/`rpi-artifacts`); TTL 300 s; sin listado. La forma robusta de obtener `clip_key` es que el frontend lo lea del item DynamoDB (vía la API de listado) y lo pase — la Lambda igual lo re-valida.

**IAM `cam-counter-lambda-clip-presign-role`** — solo `GetObject` sobre el prefijo `media/`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "GetClipsOnly", "Effect": "Allow", "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::cam-counter-media-950639281773/media/*",
      "Condition": { "Bool": { "aws:SecureTransport": "true" } } },
    { "Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:950639281773:log-group:/aws/lambda/cam-counter-clip-presign:*" }
  ]
}
```

`s3:GetObject` no soporta `s3:prefix` en Condition (eso es para `ListBucket`); el acotado real lo da el `Resource: .../media/*` + la validación regex de la clave en código. Sin `ListBucket`, sin `GetObject` fuera de `media/`.

### 2.2 `cam-counter-clip-presign-put` (presigned PUT, OPCIONAL — desactivado por defecto)

Si se prefiere el modelo presign en lugar de IoT Credentials Provider para la subida del device. **No recomendado** (round-trip extra, Lambda en camino caliente, TTL en uploads grandes). Si se activa: el device autenticado por cert pide la URL vía topic MQTT `cam-counter/{device_id}/cmd/presign-req` → otra Rule → esta Lambda → responde por `cmd/ack`; o vía endpoint HTTPS con SigV4 del IoT cred provider. La Lambda firma un **PUT** acotado a la clave determinista exacta `media/{site}/{device}/{camera}/{yyyy}/{mm}/{dd}/{event_id}.mp4` (reconstruida server-side desde la identidad del cert, no del input), con `ContentType=video/mp4` y TTL corto. IAM: solo `s3:PutObject` sobre `media/*`. Queda como feature-flag `ENABLE_PRESIGN_PUT=false`.

### 2.3 Conexión con el API del dashboard — API Gateway HTTP v2 (recomendado sobre AppSync)

**Decisión: API Gateway HTTP API** (`cam-counter-fleet-api`, stage `prod`), no AppSync. Razón: el dashboard hace lecturas REST simples (listar devices, listar eventos por site/device, pedir presign); no hay suscripciones en tiempo real ni grafo complejo que justifique GraphQL. HTTP API es más barato y simple. (AppSync `cam-counter-fleet-gql` queda documentado como alternativa si luego se quiere realtime de eventos.)

Rutas:

| Método | Ruta | Lambda | Notas |
|---|---|---|---|
| `GET` | `/devices` | `cam-counter-fleet-list` | Query GSI1 de `cam-counter-devices` por canal/flota |
| `GET` | `/devices/{device_id}/events` | `cam-counter-fleet-list` | Query `cam-counter-events` por PK del device + rango ts |
| `GET` | `/clips/presign` | `cam-counter-clip-presign` | `?clip_key=...` → URL GET 300 s |

Authorizer: **JWT (Cognito)** `cam-counter-fleet-cognito-authorizer` apuntando al User Pool `cam-counter-fleet-users` (app client `cam-counter-fleet-web-client`, Auth Code + PKCE). Todas las rutas requieren JWT válido; los grupos Cognito (`operators`/`admins`) pueden restringir scope por site en la Lambda. CORS limitado al dominio Amplify `cam-counter-fleet-console`.

```
Browser (Next.js/Amplify, Cognito JWT)
   │  GET /clips/presign?clip_key=...   (Authorization: Bearer <jwt>)
   ▼
API GW HTTP v2  ──JWT authorizer──▶  cam-counter-clip-presign  ──GetObject(media/*)──▶ S3
                                                  │ presigned GET (300s)
   ◀──────────────────────────────────────────────┘
Browser <video src="<presigned-url>">   ← reproduce MP4 directo desde S3 (no pasa por Lambda)
```

El binario nunca pasa por la Lambda ni por API Gateway: la Lambda solo firma; el `<video>` descarga directo de S3 por HTTPS.

---

## 3. Empaquetado

- **Runtime:** `python3.12`, `architecture = ["arm64"]` (Graviton, más barato; sin dependencias nativas problemáticas).
- **`events-ingest`:** única dep externa = `jsonschema`. Build con `pip install -r requirements.txt -t build/ && zip`. El contrato `crossing_event.schema.json` se **hornea** copiándolo desde `contracts/` al paquete en build (no fetch en runtime). `boto3` ya está en el runtime.
- **`device-status`, `clip-presign`:** **cero deps externas** (solo `boto3`/`botocore` del runtime) → zip plano del directorio, sin layer.
- Layer compartido opcional `cam-counter-lambda-common` (helpers `keys.py`, logging EMF) si crece; al inicio mantener self-contained.
- Build reproducible vía `terraform/modules/lambda-*` con `data.archive_file` (zip determinista) o un `make build-lambdas` que genere los zips en CI; el `source_code_hash` dispara redeploy solo ante cambio real. Alias `:live` por función (`aws_lambda_alias`) para apuntar la Rule/integración a una versión publicada y permitir rollback.
- Empaquetado de jsonschema embebido para validación offline; no llamar a red en el camino caliente.

```
# Makefile (extracto)
build-lambdas:
	for fn in events_ingest device_status clip_presign; do \
	  rm -rf build/$$fn && mkdir -p build/$$fn ; \
	  cp -r lambdas/$$fn/*.py build/$$fn/ ; \
	  [ -f lambdas/$$fn/requirements.txt ] && pip install -r lambdas/$$fn/requirements.txt -t build/$$fn/ ; \
	  cp contracts/crossing_event.schema.json build/$$fn/schema/ 2>/dev/null || true ; \
	  (cd build/$$fn && zip -qr ../$$fn.zip .) ; \
	done
```

---

## 4. Terraform

Módulos nuevos, aditivos: `terraform/modules/lambda-events-ingest`, `lambda-device-status`, `lambda-clip-presign`, `fleet-api` (API GW + authorizer), y la conexión IoT en el módulo `iot-core`. Estado remoto S3 + lock existentes. `default_tags` aplica `Project/Environment/ManagedBy/Repo/CostCenter`; cada recurso anexa `Component`.

### 4.1 Función + DLQ + async config + observabilidad

```hcl
# modules/lambda-events-ingest/main.tf
resource "aws_sqs_queue" "dlq" {
  name = "cam-counter-events-ingest-dlq"
  message_retention_seconds = 1209600   # 14 dias
  tags = { Component = "events" }
}

data "archive_file" "zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../../build/events_ingest"
  output_path = "${path.module}/build/events_ingest.zip"
}

resource "aws_lambda_function" "ingest" {
  function_name = "cam-counter-events-ingest"
  role          = aws_iam_role.ingest.arn
  runtime       = "python3.12"
  architectures = ["arm64"]
  handler       = "handler.lambda_handler"
  filename         = data.archive_file.zip.output_path
  source_code_hash = data.archive_file.zip.output_base64sha256
  timeout       = 15
  memory_size   = 256
  environment { variables = { EVENTS_TABLE = "cam-counter-events", LOG_LEVEL = "INFO" } }
  tracing_config { mode = "Active" }
  tags = { Component = "events" }
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.ingest.function_name
  function_version = aws_lambda_function.ingest.version
}

resource "aws_lambda_function_event_invoke_config" "ingest" {
  function_name                = aws_lambda_function.ingest.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
  destination_config { on_failure { destination = aws_sqs_queue.dlq.arn } }
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/cam-counter-events-ingest"
  retention_in_days = 30
  tags = { Component = "events" }
}

resource "aws_cloudwatch_metric_alarm" "ingest_errors" {
  alarm_name          = "cam-counter-events-ingest-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.ingest.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
}

resource "aws_cloudwatch_metric_alarm" "ingest_dlq" {
  alarm_name          = "cam-counter-events-ingest-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
}
```

### 4.2 IAM rol mínimo (ingest)

```hcl
resource "aws_iam_role" "ingest" {
  name = "cam-counter-lambda-events-ingest-role"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]})
  tags = { Component = "events" }
}
resource "aws_iam_role_policy" "ingest" {
  name = "cam-counter-lambda-events-ingest-policy"
  role = aws_iam_role.ingest.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid="WriteEventsOnly", Effect="Allow", Action=["dynamodb:PutItem","dynamodb:UpdateItem"],
      Resource="arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-events" },
    { Sid="Dlq", Effect="Allow", Action=["sqs:SendMessage"], Resource=aws_sqs_queue.dlq.arn },
    { Sid="Logs", Effect="Allow", Action=["logs:CreateLogStream","logs:PutLogEvents"],
      Resource="${aws_cloudwatch_log_group.ingest.arn}:*" }
  ]})
}
```

### 4.3 Conexión IoT Rule → Lambda (en módulo iot-core)

```hcl
resource "aws_iot_topic_rule" "crossing_events" {
  name        = "cam_counter_crossing_events"     # snake_case (restriccion de IoT Rule names)
  enabled     = true
  sql         = "SELECT *, topic(2) AS _device_id_topic, clientid() AS _client_id, timestamp() AS _ingest_ts_ms FROM 'cam-counter/+/events/crossing'"
  sql_version = "2016-03-23"

  lambda { function_arn = var.ingest_lambda_alias_arn }   # apunta al alias :live

  error_action {
    republish {
      role_arn = aws_iam_role.iot_rule_republish.arn
      topic    = "cam-counter/$dlq/cam_counter_crossing_events"
      qos      = 1
    }
  }
}

# IoT necesita permiso explícito para invocar la Lambda
resource "aws_lambda_permission" "iot_invoke_ingest" {
  statement_id  = "AllowIoTRuleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.ingest_lambda_alias_arn          # alias ARN -> invoca :live
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.crossing_events.arn
}
```

Análogo para `cam_counter_device_status` (FROM `cam-counter/+/status/connection` y heartbeat) y `cam_counter_device_lwt` (`WHERE status='offline' AND reason='lwt'`) → `cam-counter-device-status`. Respaldo opcional: IoT **Lifecycle Events** (`$aws/events/presence/disconnected/+`) a la misma Lambda para garantía de detección de caída por el broker.

### 4.4 API Gateway HTTP v2 + authorizer Cognito + presign

```hcl
resource "aws_apigatewayv2_api" "fleet" {
  name          = "cam-counter-fleet-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://fleet.cam-counter.example.com"]
    allow_methods = ["GET"]
    allow_headers = ["authorization","content-type"]
  }
  tags = { Component = "fleet-console" }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.fleet.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cam-counter-fleet-cognito-authorizer"
  jwt_configuration {
    audience = [var.cognito_web_client_id]                 # cam-counter-fleet-web-client
    issuer   = "https://cognito-idp.us-east-1.amazonaws.com/${var.cognito_user_pool_id}"
  }
}

resource "aws_apigatewayv2_integration" "presign" {
  api_id                 = aws_apigatewayv2_api.fleet.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.clip_presign_alias_arn       # alias :live
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "presign" {
  api_id             = aws_apigatewayv2_api.fleet.id
  route_key          = "GET /clips/presign"
  target             = "integrations/${aws_apigatewayv2_integration.presign.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.fleet.id
  name        = "prod"
  auto_deploy = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format          = jsonencode({ requestId="$context.requestId", status="$context.status",
                                    routeKey="$context.routeKey", err="$context.authorizer.error" })
  }
}

resource "aws_lambda_permission" "apigw_presign" {
  statement_id  = "AllowApiGwInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.clip_presign_alias_arn
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.fleet.execution_arn}/*/*"
}
```

---

## 5. Idempotencia y edge-first (garantías de cierre)

- **Idempotencia ingest:** `SK` deriva de `event_id` determinista; `attribute_not_exists` rechaza duplicados; `ConditionalCheckFailedException` ⇒ éxito silencioso. Soporta dual-write de migración y reintentos. `clip_status` avanza **monótonamente** `pending→uploaded` con `UpdateItem` condicional (nunca retrocede).
- **Idempotencia status:** `UpdateItem` con guard `last_seen_ms <= :now` (last-writer-wins por tiempo); el LWT retenido viejo no pisa un estado nuevo.
- **Edge-first:** estas Lambdas son best-effort downstream. El device cuenta y encola en SQLite local sin depender de ellas; si la ingesta falla, el evento queda `synced=0` y se reintenta (y/o cae a DLQ para redrive). Ningún path síncrono device→Lambda bloquea el conteo.
- **Migración monótona:** todo (Lambdas, roles, DLQ, API, Rules) se **adjunta**; no se destruye/renombra ningún recurso existente (`cam-counter-events`, `cam-counter-devices`, `cam-counter-media-950639281773`, rol de borde). El corte del IAM user `raspberry` ocurre en un PR posterior, revisado por humano (no por el runner MAD autónomo, que aborta ante destroy).

## 6. Mapa de archivos (entregables)

```
lambdas/events_ingest/{handler,validation,keys,ddb}.py + schema/ + requirements.txt + tests/
lambdas/device_status/handler.py + tests/
lambdas/clip_presign/{handler,authz}.py
lambdas/clip_presign_put/handler.py            # OPCIONAL, feature-flag off
terraform/modules/lambda-events-ingest/        # fn + dlq + invoke-config + alias + alarms + iam
terraform/modules/lambda-device-status/
terraform/modules/lambda-clip-presign/
terraform/modules/fleet-api/                   # api gw http v2 + cognito authorizer + routes
terraform/modules/iot-core/                    # rules + lambda_permission + republish role (extiende)
terraform/environments/prod/                   # wiring de los modulos
Makefile                                       # build-lambdas (zip determinista)
```