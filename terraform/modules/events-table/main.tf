# Módulo `events-table` — tabla DynamoDB de EVENTOS de cruce (histórico en nube).
#
# Sustrato REAL del contrato CrossingEvent. El edge persiste en local (SQLite WAL) y la
# sincronización cloud (PR10) escribe aquí con conditional put sobre la PK/SK derivadas del
# `event_id` DETERMINISTA, de modo que reintentar el MISMO `event_id` NO duplica.
#
# Claves (EXACTAS, ver contrato canónico):
#   PK = CAM#{site_id}#{device_id}#{camera_id}                       (hash, String)
#   SK = TS#{ts_event_ms:013d}#{event_id}                            (range, String)
#   GSI1PK = SITE#{site_id}  /  GSI1SK = TS#{ts_event_ms:013d}#{event_id}   (proyección ALL)
#
# DynamoDB es schemaless: SÓLO se declaran como `attribute` los que participan en
# keys/índices (PK, SK, GSI1PK, GSI1SK). El resto de campos del evento (event_id, site_id,
# device_id, camera_id, track_id, direction, label, line_version, ts_event_ms, ts_event_iso,
# confidence, clip_key, clip_status, schema_version, created_at) NO se declaran en Terraform.
#
# Billing PAY_PER_REQUEST (on-demand), PITR ON, TTL opcional (off por defecto).
#
# F3 — TAGS: default_tags (dual-case, válido en DynamoDB que distingue mayúsculas) + merge de
# `local.tags` para garantizar la clave minúscula `managed_by=mad-runner`.

locals {
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )
}

resource "aws_dynamodb_table" "events" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  # Sólo los atributos de keys/índices se declaran (DynamoDB es schemaless en el resto).
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

  # GSI1 — "todos los eventos de un sitio (todas sus cámaras) por tiempo".
  # Proyección ALL: las consultas de sitio devuelven el item completo sin un GetItem extra
  # (lecturas históricas/exportación). Trade-off aceptado: mayor coste de almacenamiento del
  # índice a cambio de no re-leer la tabla base.
  global_secondary_index {
    name            = var.gsi1_name
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  # TTL opcional (off por defecto): hook de retención configurable del histórico.
  ttl {
    enabled        = var.enable_ttl
    attribute_name = var.enable_ttl ? var.ttl_attribute_name : ""
  }

  tags = local.tags
}
