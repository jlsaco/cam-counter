# ─────────────────────────────────────────────────────────────────────────────
# Tabla DynamoDB de EVENTOS de cruce (histórico en nube). Sustrato REAL contra el
# que PR10 validará la idempotencia edge→cloud (event_id determinista +
# conditional put). Ver contrato CrossingEvent en CLAUDE.md §8.A.
#
#   PK     = CAM#{site_id}#{device_id}#{camera_id}   (hash, String)
#   SK     = TS#{ts_event_ms:013d}#{event_id}        (range, String)
#   GSI1PK = SITE#{site_id}                           (hash GSI1, String)
#   GSI1SK = TS#{ts_event_ms:013d}#{event_id}         (range GSI1, String)
#
# Sólo se declaran como `attribute` las claves de la tabla y del GSI1; el resto de
# campos del evento (direction, label, confidence, clip_key, …) son schemaless.
# PAY_PER_REQUEST (on-demand) + Point-in-Time Recovery. TTL opcional (off default).
# F3 — tags lógicos minúscula vía var.tags + default_tags raíz.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "events" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  # GSI1 — "todos los eventos de un sitio (todas sus cámaras) ordenados por tiempo".
  # Proyección ALL: las queries por sitio devuelven el evento completo sin segundo
  # GetItem a la tabla base (lectura coste-eficiente para dashboards/sync por sitio).
  global_secondary_index {
    name            = var.gsi1_name
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  # TTL opcional: deshabilitado por defecto (el histórico no caduca salvo política).
  dynamic "ttl" {
    for_each = var.enable_ttl ? [1] : []
    content {
      enabled        = true
      attribute_name = var.ttl_attribute_name
    }
  }

  tags = merge(var.tags, {
    Name = var.table_name
  })
}
