# Módulo `device-registry` — tabla DynamoDB de REGISTRO DE DISPOSITIVOS de la flota.
#
# Espejo/observabilidad de la flota OTA. NUNCA es la fuente de verdad de la versión deseada
# (esa es el manifiesto del canal en S3, leído por SigV4 — ver README, nota del espejo).
#
# Claves (EXACTAS, ver contrato canónico):
#   PK = DEVICE#{device_id}                                    (hash, String)
#   GSI1PK = CHANNEL#{release_channel}  /  GSI1SK = DEVICE#{device_id}   (proyección ALL)
#
# DynamoDB es schemaless: SÓLO se declaran como `attribute` los de keys/índices (PK, GSI1PK,
# GSI1SK). El resto del item (device_id, site_id, camera_ids, release_channel,
# desired_version, reported_version, last_good_version, last_update_status,
# last_update_error, last_seen_at, agent_version, status, hardware{model,hailo_fw}) NO se
# declara en Terraform.
#
# Billing PAY_PER_REQUEST (on-demand), PITR ON.
#
# F3 — TAGS: default_tags (dual-case, válido en DynamoDB) + merge de `local.tags` para
# garantizar la clave minúscula `managed_by=mad-runner`.

locals {
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )
}

resource "aws_dynamodb_table" "devices" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"

  attribute {
    name = "PK"
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

  # GSI1 — "enumerar todos los dispositivos de un canal" (para rollout canary→flota).
  # Proyección ALL: el orquestador de release lee el estado completo de cada dispositivo del
  # canal sin un GetItem extra por device.
  global_secondary_index {
    name            = var.gsi1_name
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}
