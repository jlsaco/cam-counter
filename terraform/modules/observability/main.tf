# Módulo `observability` (WP18 — cierre) — alarmas + dashboard + status path por broker.
#
# ADITIVO Y AUTOCONTENIDO (F1): sólo AÑADE alarmas de CloudWatch, un dashboard, un topic SNS,
# la tabla DynamoDB de status de presencia y la IoT Rule (+ rol) que la alimenta. No toca ni
# referencia recursos de PR02–PR11; las alarmas referencian destinos por DIMENSIÓN (nombre),
# así que el plan/apply no falla aunque un destino aún no exista (alarma en INSUFFICIENT_DATA).
#
# Cobertura de la observabilidad end-to-end:
#   ingest    → Lambda events-ingest (Errors/Throttles) + IoT Rule de cruces (throttle)
#   status    → IoT Lifecycle Events (disconnected) → tabla cam-counter-device-status (LWT backstop)
#   throttles → Throttles de las 3 Lambdas
#   DLQ       → profundidad de la DLQ de ingesta (si se pasa dlq_name)
#   API       → 5xx + 4xx (proxy de authorizer/401) del HTTP API v2 (si se pasa api_id)

locals {
  # Tags lógicos minúscula (F3) garantizados en TODOS los recursos taggables del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  # Las 3 Lambdas del plano cloud → alarmas Errors+Throttles por función.
  lambdas = {
    ingest       = var.ingest_lambda_name
    fleet_api    = var.fleet_api_lambda_name
    clip_presign = var.clip_presign_lambda_name
  }

  presence_rule_name = "cam_counter_presence_disconnected"

  # Las alarmas de API/DLQ se materializan SÓLO si su identificador está presente.
  api_enabled = var.api_id != ""
  dlq_enabled = var.dlq_name != ""
}

# ═══════════════════════════════════ SNS — destino de alarmas ═══════════════════════════════════

resource "aws_sns_topic" "alarms" {
  name = var.sns_topic_name
  tags = local.tags
}

# Suscripción email OPCIONAL (requiere confirmación del destinatario). Sin email => sólo el
# topic (se suscribe luego por consola/CLI o se conecta a un chatbot/PagerDuty).
resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ═════════════════════ Status path — tabla + IoT Lifecycle Events (LWT backstop) ═════════════════════
#
# RESPALDO NO OPCIONAL del LWT (nota del revisor, BAJA): el broker de IoT Core publica
# `$aws/events/presence/disconnected/+` de forma GARANTIZADA cuando una sesión cae, incluso si
# el Last-Will-and-Testament del dispositivo no llegó a publicarse (evita el "online fantasma").
# Esta IoT Rule persiste cada desconexión en una tabla dedicada (no pisa el device-registry).

resource "aws_dynamodb_table" "device_status" {
  name         = var.device_status_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "clientId"

  attribute {
    name = "clientId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  # SSE-S3 gestionado por AWS (cifrado en reposo siempre activo en DynamoDB; explícito por F-hardening).
  server_side_encryption {
    enabled = true
  }

  ttl {
    enabled        = var.device_status_ttl_days > 0
    attribute_name = var.device_status_ttl_days > 0 ? "expires_at" : ""
  }

  tags = local.tags
}

# Rol que la IoT Rule asume para escribir en la tabla de status. Usa el proveedor IAM-safe
# `aws.iam` (claves de tag case-insensitive en IAM, igual que iam-edge/iam-github-oidc).
data "aws_iam_policy_document" "iot_status_assume" {
  statement {
    sid     = "IoTAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "iot_status" {
  provider           = aws.iam
  name               = "cam-counter-iot-presence-status"
  description        = "Rol que asume la IoT Rule de presencia para escribir desconexiones en ${var.device_status_table_name} (LWT backstop)."
  assume_role_policy = data.aws_iam_policy_document.iot_status_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "iot_status_write" {
  statement {
    sid       = "PutPresenceStatus"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.device_status.arn]
  }
}

resource "aws_iam_role_policy" "iot_status" {
  provider = aws.iam
  name     = "cam-counter-iot-presence-status-policy"
  role     = aws_iam_role.iot_status.id
  policy   = data.aws_iam_policy_document.iot_status_write.json
}

# IoT Topic Rule — enruta CADA desconexión de presencia a la tabla de status.
# dynamodbv2 mapea los campos top-level del SELECT a columnas; `clientId` es la PK.
resource "aws_iot_topic_rule" "presence_disconnected" {
  name        = local.presence_rule_name
  description = "LWT backstop: persiste $aws/events/presence/disconnected/+ en la tabla de status de la flota."
  enabled     = true
  sql         = "SELECT clientId, timestamp AS disconnected_at_ms, eventType, sessionIdentifier, principalIdentifier, disconnectReason, versionNumber, ipAddress FROM '$aws/events/presence/disconnected/+'"
  sql_version = "2016-03-23"

  dynamodbv2 {
    role_arn = aws_iam_role.iot_status.arn
    put_item {
      table_name = aws_dynamodb_table.device_status.name
    }
  }

  tags = local.tags
}

# ═══════════════════════════════════ Alarmas — Lambdas ═══════════════════════════════════

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.lambdas

  alarm_name          = "${each.value}-errors"
  alarm_description   = "Errores de invocación de la Lambda ${each.value} (>=1 en ${var.alarm_period_seconds}s)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each = local.lambdas

  alarm_name          = "${each.value}-throttles"
  alarm_description   = "Throttles de la Lambda ${each.value} (>=1 en ${var.alarm_period_seconds}s)."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ═══════════════════════════════════ Alarma — IoT Rule de cruces ═══════════════════════════════════
#
# RuleMessageThrottled lleva dimensión RuleName de forma fiable (a diferencia de las métricas
# de acción Success/Failure, que requieren además ActionType). Las FALLAS de la acción
# (entrega a la Lambda) quedan cubiertas por la alarma `*-errors` de la Lambda de ingesta.

resource "aws_cloudwatch_metric_alarm" "iot_rule_throttled" {
  alarm_name          = "${var.ingest_iot_rule_name}-throttled"
  alarm_description   = "Mensajes descartados por throttling en la IoT Rule de cruces ${var.ingest_iot_rule_name}."
  namespace           = "AWS/IoT"
  metric_name         = "RuleMessageThrottled"
  dimensions          = { RuleName = var.ingest_iot_rule_name }
  statistic           = "Sum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ═══════════════════════════════════ Alarmas — API Gateway HTTP API v2 ═══════════════════════════════════
#
# Sólo si se conoce el ApiId. 5xx = fallos del backend/integración. 4xx = proxy de errores del
# authorizer JWT Cognito (401/403): un pico de 4xx señala authorizer rechazando o mal config.

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  count = local.api_enabled ? 1 : 0

  alarm_name          = "cam-counter-fleet-api-5xx"
  alarm_description   = "Respuestas 5xx del HTTP API v2 (ApiId ${var.api_id})."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = var.api_id }
  statistic           = "Sum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "api_4xx" {
  count = local.api_enabled ? 1 : 0

  alarm_name          = "cam-counter-fleet-api-4xx-authorizer"
  alarm_description   = "Pico de 4xx del HTTP API v2 (proxy de errores del authorizer JWT Cognito: 401/403)."
  namespace           = "AWS/ApiGateway"
  metric_name         = "4xx"
  dimensions          = { ApiId = var.api_id }
  statistic           = "Sum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ═══════════════════════════════════ Alarma — DLQ de ingesta ═══════════════════════════════════

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  count = local.dlq_enabled ? 1 : 0

  alarm_name          = "${var.dlq_name}-depth"
  alarm_description   = "Profundidad de la DLQ de ingesta ${var.dlq_name} (mensajes muertos sin reprocesar)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_name }
  statistic           = "Maximum"
  period              = var.alarm_period_seconds
  evaluation_periods  = 1
  threshold           = var.dlq_depth_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ═══════════════════════════════════ Dashboard `cam-counter-fleet` ═══════════════════════════════════

resource "aws_cloudwatch_dashboard" "fleet" {
  dashboard_name = var.dashboard_name

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2,
        properties = {
          markdown = "# cam-counter — consola de flota\nIngesta · status de presencia · throttles · DLQ · API. Alarmas → SNS `${var.sns_topic_name}`."
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 12, height = 6,
        properties = {
          title  = "Ingesta — invocaciones / errores / throttles",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", var.ingest_lambda_name, { stat = "Sum" }],
            ["AWS/Lambda", "Errors", "FunctionName", var.ingest_lambda_name, { stat = "Sum" }],
            ["AWS/Lambda", "Throttles", "FunctionName", var.ingest_lambda_name, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 2, width = 12, height = 6,
        properties = {
          title  = "Ingesta — duración (p50/p99)",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", var.ingest_lambda_name, { stat = "p50" }],
            ["AWS/Lambda", "Duration", "FunctionName", var.ingest_lambda_name, { stat = "p99" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 8, width = 12, height = 6,
        properties = {
          title  = "API fleet — errores / throttles",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", var.fleet_api_lambda_name, { stat = "Sum" }],
            ["AWS/Lambda", "Throttles", "FunctionName", var.fleet_api_lambda_name, { stat = "Sum" }],
            ["AWS/Lambda", "Errors", "FunctionName", var.clip_presign_lambda_name, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 8, width = 12, height = 6,
        properties = {
          title  = "IoT Rule de cruces — throttling",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/IoT", "RuleMessageThrottled", "RuleName", var.ingest_iot_rule_name, { stat = "Sum" }],
            ["AWS/IoT", "TopicMatch", "RuleName", var.ingest_iot_rule_name, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 14, width = 12, height = 6,
        properties = {
          title  = "Status de presencia (LWT backstop) — escrituras",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = [
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", var.device_status_table_name, { stat = "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 14, width = 12, height = 6,
        properties = {
          title  = "API HTTP v2 — 4xx (authorizer) / 5xx",
          region = var.region,
          view   = "timeSeries", stacked = false,
          metrics = local.api_enabled ? [
            ["AWS/ApiGateway", "4xx", "ApiId", var.api_id, { stat = "Sum" }],
            ["AWS/ApiGateway", "5xx", "ApiId", var.api_id, { stat = "Sum" }]
          ] : [["AWS/ApiGateway", "5xx", "ApiId", "set-api_id-to-enable", { stat = "Sum" }]]
        }
      }
    ]
  })
}
