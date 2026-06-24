# Outputs del módulo `iot-credential-provider`. Los consume `iot-core` (WP futuro): la IoT
# Policy del dispositivo necesita el ARN del role alias para conceder
# `iot:AssumeRoleWithCertificate` sobre él, y el provisioning necesita el nombre del alias.

output "role_alias_name" {
  description = "Nombre del role alias del IoT Credentials Provider (cam-counter-edge-s3-role-alias). Lo presenta el cert del Pi al credentials endpoint."
  value       = aws_iot_role_alias.edge_s3.alias
}

output "role_alias_arn" {
  description = "ARN REAL del role alias. La IoT Policy del dispositivo lo usa como Resource de iot:AssumeRoleWithCertificate."
  value       = aws_iot_role_alias.edge_s3.arn
}

output "edge_s3_role_arn" {
  description = "ARN del rol IAM cam-counter-edge-s3-role que el role alias expone (trust en credentials.iot.amazonaws.com)."
  value       = aws_iam_role.edge_s3.arn
}
