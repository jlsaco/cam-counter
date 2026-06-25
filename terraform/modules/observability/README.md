# Módulo `observability` — alarmas, dashboard y status path (WP18)

Cierre de observabilidad **end-to-end** de la flota `cam-counter`. **Aditivo y autocontenido**
(F1): sólo *añade* recursos; no toca ni referencia recursos de PR02–PR11.

## Qué crea

| Recurso | Propósito |
| --- | --- |
| `aws_sns_topic.alarms` (+ suscripción email opcional) | Destino único de `alarm_actions`/`ok_actions`. |
| `aws_cloudwatch_metric_alarm.lambda_errors` (×3) | Errores de `events-ingest`, `fleet-api`, `clip-presign`. |
| `aws_cloudwatch_metric_alarm.lambda_throttles` (×3) | Throttles de las 3 Lambdas. |
| `aws_cloudwatch_metric_alarm.iot_rule_throttled` | Throttling de la IoT Rule de cruces. |
| `aws_cloudwatch_metric_alarm.api_5xx` / `api_4xx` | 5xx + 4xx (proxy de authorizer JWT) del HTTP API v2. **Sólo si `api_id != ""`**. |
| `aws_cloudwatch_metric_alarm.dlq_depth` | Profundidad de la DLQ de ingesta. **Sólo si `dlq_name != ""`**. |
| `aws_cloudwatch_dashboard.fleet` | Dashboard `cam-counter-fleet`: ingesta/status/throttles/DLQ/API. |
| `aws_dynamodb_table.device_status` | Tabla de **status de presencia** (`cam-counter-device-status`, PK `clientId`). |
| `aws_iot_topic_rule.presence_disconnected` (+ rol IAM) | **IoT Lifecycle Events** `$aws/events/presence/disconnected/+` → tabla de status. |

## Por qué es autocontenido y aditivo

Las alarmas de CloudWatch referencian sus destinos por **dimensión** (string: `FunctionName`,
`ApiId`, `RuleName`, `QueueName`), **no** por referencia Terraform. El `plan`/`apply` no falla
aunque el destino aún no exista: la alarma nace en `INSUFFICIENT_DATA` y se activa cuando el
recurso empieza a emitir métricas. Las alarmas de **API** y **DLQ** se crean sólo si se pasa
su identificador (`count`), para no fabricar alarmas que nunca podrán resolverse.

## IoT Lifecycle Events — respaldo NO opcional del LWT

El broker de IoT Core publica `$aws/events/presence/disconnected/+` de forma **garantizada**
cuando una sesión MQTT cae, **incluso si el Last-Will-and-Testament (LWT) del dispositivo no
llegó a publicarse**. Sin este respaldo, un corte abrupto dejaría al dispositivo como
*online fantasma*. La IoT Rule de este módulo persiste cada desconexión en una tabla
**dedicada** (`cam-counter-device-status`) — **no pisa** el `device-registry` (`cam-counter-devices`).
Para activarlo a nivel de cuenta, los IoT Lifecycle/Presence events deben estar habilitados en
**IoT Core → Settings → Event-based messages** (paso operativo en [`docs/runbooks.md`](../../../docs/runbooks.md)).

## Proveedores

Recibe **dos** proveedores desde la raíz `environments/prod`:

```hcl
module "observability" {
  source = "../../modules/observability"
  providers = {
    aws     = aws       # CloudWatch/IoT/SNS/DynamoDB (dual-case F3, case-sensitive)
    aws.iam = aws.iam   # rol IAM de la IoT Rule (claves de tag case-insensitive)
  }
  # ... variables ...
}
```

## Variables principales

- `ingest_lambda_name` / `fleet_api_lambda_name` / `clip_presign_lambda_name` — defaults reales.
- `ingest_iot_rule_name` — default `cam_counter_events_crossing`.
- `api_id` — ApiId del HTTP API v2 (vacío ⇒ sin alarmas de API).
- `dlq_name` — cola DLQ de ingesta (vacío ⇒ sin alarma de DLQ).
- `device_status_table_name` — default `cam-counter-device-status`.
- `alarm_email` — email opcional para SNS.

Ver [`variables.tf`](variables.tf) para la lista completa.
