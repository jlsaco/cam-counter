# Módulo `iot-credential-provider` — role alias S3 para clips, sin llaves estáticas

Provisiona el **IoT Credentials Provider** que permite al device seguir subiendo clips MP4 a
S3 **sin credenciales AWS directas ni un segundo secreto**: el Pi llama al *credentials
endpoint* de IoT con su **MISMO cert mTLS** y recibe **credenciales STS de corta vida** del
rol `cam-counter-edge-s3-role` (vía el role alias `cam-counter-edge-s3-role-alias`), acotadas a
su propio prefijo de media. Reusa la identidad del cert: **cero llaves estáticas** en el
dispositivo. Cuenta `950639281773` / `us-east-1`.

Apila sobre WP03 (`iam-lambda`); es **aditivo** (sólo añade un rol IAM y un role alias). El
bucket de media **NO** lo gestiona este módulo: se **referencia** (en la raíz, vía un `data`
source). Para el **primer Pi** se instancia con placeholders **no sensibles**
(`site_id = "sitio-demo"`, `device_id = "rpi-001"`), igual que `iam-edge`.

---

## Recursos creados (2)

| # | Recurso | Nombre canónico | Notas |
| --- | --- | --- | --- |
| 1 | `aws_iam_role` | `cam-counter-edge-s3-role` | Trust en `credentials.iot.amazonaws.com`. Política inline sólo `s3:PutObject`. |
| 2 | `aws_iot_role_alias` | `cam-counter-edge-s3-role-alias` | Apunta al rol (1). `credential_duration` corta (default 3600 s). |

---

## Permiso concedido (EXACTO)

| Servicio | Acciones | Recurso / condición | Por qué |
| --- | --- | --- | --- |
| **S3 media** | `s3:PutObject` | `…media-…/media/${site_id}/${device_id}/*`, `Condition Bool aws:SecureTransport = true` | Subir clips SÓLO al prefijo del propio Pi, **sólo sobre TLS**. |

**SIN `s3:GetObject` / `s3:ListBucket` / `s3:DeleteObject`.** El proceso edge sólo necesita
**escribir** clips; no lee, no lista, no borra con estas credenciales.

> **Aislamiento entre dispositivos**: con estas credenciales un Pi **no** puede escribir media
> de otro device (`media/otro/otro/*`): no cae bajo el `Allow` → **DENY**. Tampoco puede leer,
> listar ni borrar nada (acción no concedida → DENY).

### Por qué se acota por **Resource ARN** y NO por `s3:prefix` ni por `${credentials-iot:ThingName}`

- **`Condition StringLike s3:prefix` es INERTE sobre `PutObject`** (nota del revisor WP04):
  `s3:prefix` sólo lo evalúa `s3:ListBucket`, no `PutObject`. Acotar con él **no restringe
  nada** en una subida. Por eso la restricción real es el **Resource ARN**
  `media/${site_id}/${device_id}/*`.
- **NO se usa `${credentials-iot:ThingName}` crudo** en el Resource: el ThingName es
  `cam-counter-{site}-{device}` (separador `-`), pero el prefijo S3 es
  `media/{site}/{device}/` (separador `/`). El ThingName **no casa** con el prefijo separado
  por `/`, así que se **derivan `site_id`/`device_id` de variables del provisioning** y se
  construye el ARN exacto. (El aislamiento multi-tenant *a escala de flota* se hace por
  ThingName en la **IoT Policy del dispositivo**, que vive en el módulo `iot-core`; aquí el
  rol S3 se parametriza por device como hace `iam-edge`.)

---

## Trust policy

El rol lo asume **EXCLUSIVAMENTE** el servicio del credentials endpoint
(`credentials.iot.amazonaws.com`), en nombre del cert X.509 del Thing. Ningún otro principal
(ni el runner, ni Lambda, ni el Pi directamente con claves) puede asumirlo.

Flujo en el device (WP futuro, fuera de este módulo):

1. El Pi llama a `https://<iot-cred-endpoint>/role-aliases/cam-counter-edge-s3-role-alias/credentials`
   presentando su cert/llave mTLS (`/etc/cam-counter/certs/device.{cert.pem,private.key}`).
2. IoT verifica que el cert está **activo** y adjunto a una IoT Policy que permite
   `iot:AssumeRoleWithCertificate` sobre el ARN de **este** role alias.
3. IoT devuelve credenciales STS del rol `cam-counter-edge-s3-role`, válidas
   `credential_duration_seconds` (default 3600).

> **Guardarraíl**: este credential provider es justamente lo que permitirá que el **proceso
> edge** deje de usar credenciales AWS directas en el corte (WP16) **sin tocar** la identidad
> de despliegue `raspberry` con la que MAD aplica Terraform.

---

## Variables

| Nombre | Tipo | Default | Descripción |
| --- | --- | --- | --- |
| `role_alias_name` | string | `cam-counter-edge-s3-role-alias` | Nombre del role alias. |
| `edge_s3_role_name` | string | `cam-counter-edge-s3-role` | Nombre del rol IAM expuesto. |
| `media_bucket_name` | string | `cam-counter-media-950639281773` | Nombre del bucket de media (cross-check del ARN; no gestionado). |
| `media_bucket_arn` | string | — (requerido) | ARN del bucket de media (en la raíz: `data` source). |
| `site_id` | string | `sitio-demo` | Slug de sitio del provisioning (acota el prefijo S3). |
| `device_id` | string | `rpi-001` | Slug de device del provisioning (acota el prefijo S3). |
| `credential_duration_seconds` | number | `3600` | Vida de las STS [900, 43200]. |
| `tags` | map(string) | `{}` | Tags lógicos minúscula (F3). |

## Outputs

| Nombre | Descripción |
| --- | --- |
| `role_alias_name` | Nombre del role alias (lo presenta el cert al endpoint). |
| `role_alias_arn` | ARN del role alias (Resource de `iot:AssumeRoleWithCertificate` en la IoT Policy del device — para `iot-core`). |
| `edge_s3_role_arn` | ARN del rol IAM `cam-counter-edge-s3-role`. |

---

## F3 — Tags

El rol IAM tiene claves de tag **CASE-INSENSITIVE**: la raíz instancia el módulo con el
proveedor **IAM-safe `aws.iam`** (providers = { aws = aws.iam }), cuyos `default_tags`
{ `Env`, `project=cam-counter`, `managed_by=mad-runner` } **NO** incluyen las capitalizadas
que colisionarían (`Project`/`ManagedBy`) en el rol. `local.tags` garantiza además la clave
**minúscula** `managed_by=mad-runner`. La verificación F3 busca la clave **minúscula**.

---

## Verificación real (Definition of Done)

El plan es **estrictamente aditivo** (sólo *to add*; F1). Tras el apply autónomo del runner
MAD (como `raspberry`), verificar contra AWS **real**:

```bash
# 1) El rol existe y su trust es SÓLO el credentials endpoint de IoT.
aws iam get-role --role-name cam-counter-edge-s3-role \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Principal.Service' --output text
# Esperado: credentials.iot.amazonaws.com

# 2) La política inline concede SÓLO s3:PutObject, TLS-only, al prefijo del Pi.
aws iam get-role-policy --role-name cam-counter-edge-s3-role \
  --policy-name cam-counter-edge-s3-role-policy \
  --query 'PolicyDocument.Statement[0]'
# Esperado: Action="s3:PutObject", Condition.Bool."aws:SecureTransport"="true",
#           Resource termina en /media/sitio-demo/rpi-001/*

# 3) El role alias existe, apunta al rol y tiene duración corta.
aws iot describe-role-alias --role-alias cam-counter-edge-s3-role-alias \
  --query 'roleAliasDescription.{Role:roleArn,Dur:credentialDurationSeconds}'
# Esperado: Role termina en role/cam-counter-edge-s3-role, Dur=3600
```

### Test NEGATIVO — `PutObject` fuera del prefijo → `AccessDenied`

Con las credenciales STS del role alias, una subida **dentro** del prefijo del Pi se permite y
**fuera** se deniega. Simulación con la API de IAM (sin necesidad de un device real):

```bash
ROLE_ARN=$(aws iam get-role --role-name cam-counter-edge-s3-role --query 'Role.Arn' --output text)

# (a) DENTRO del prefijo propio → allowed
aws iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
  --action-names s3:PutObject \
  --resource-arns "arn:aws:s3:::cam-counter-media-950639281773/media/sitio-demo/rpi-001/clip.mp4" \
  --context-entries 'ContextKeyName=aws:SecureTransport,ContextKeyType=boolean,ContextKeyValues=true' \
  --query 'EvaluationResults[0].EvalDecision' --output text
# Esperado: allowed

# (b) FUERA del prefijo (otro device) → explicitDeny/implicitDeny
aws iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
  --action-names s3:PutObject \
  --resource-arns "arn:aws:s3:::cam-counter-media-950639281773/media/otro/otro/clip.mp4" \
  --context-entries 'ContextKeyName=aws:SecureTransport,ContextKeyType=boolean,ContextKeyValues=true' \
  --query 'EvaluationResults[0].EvalDecision' --output text
# Esperado: implicitDeny  (no cae bajo el Allow del prefijo propio)

# (c) DENTRO del prefijo pero SIN TLS → denegado por la condición SecureTransport
aws iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
  --action-names s3:PutObject \
  --resource-arns "arn:aws:s3:::cam-counter-media-950639281773/media/sitio-demo/rpi-001/clip.mp4" \
  --context-entries 'ContextKeyName=aws:SecureTransport,ContextKeyType=boolean,ContextKeyValues=false' \
  --query 'EvaluationResults[0].EvalDecision' --output text
# Esperado: implicitDeny  (Bool aws:SecureTransport=true no se cumple)

# (d) Cualquier lectura/borrado → denegado (acción no concedida)
aws iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
  --action-names s3:GetObject s3:DeleteObject s3:ListBucket \
  --resource-arns "arn:aws:s3:::cam-counter-media-950639281773/media/sitio-demo/rpi-001/clip.mp4" \
  --query 'EvaluationResults[].EvalDecision' --output text
# Esperado: implicitDeny implicitDeny implicitDeny
```

---

## Reconciliación de naming

| Subsistema | Nombre | Patrón |
| --- | --- | --- |
| IoT Thing (provisioning) | `cam-counter-{site_id}-{device_id}` | sin infijo |
| Rol per-Pi (`iam-edge`) | `cam-counter-edge-{site_id}-{device_id}` | infijo `-edge-` |
| **Rol del credential provider (este módulo)** | `cam-counter-edge-s3-role` | sin infijo per-Pi |
| **Role alias (este módulo)** | `cam-counter-edge-s3-role-alias` | sin infijo per-Pi |

Los nombres `cam-counter-edge-s3-role{,-alias}` son **fijos** (naming-standard §5): el rol del
credential provider es el **puente** cert→S3 de la flota, NO un rol por dispositivo. El prefijo
S3 (`media/{site}/{device}/*`) es lo que se parametriza por Pi.
