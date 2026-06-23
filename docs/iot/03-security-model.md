This is a pure design/documentation task. I have a comprehensive context. Let me produce the markdown security model directly.

# Modelo de seguridad

End-to-end para **cam-counter** (cuenta AWS `950639281773`, región `us-east-1`, monorepo `github.com/jlsaco/cam-counter`). Diseño de seguridad para la migración de "llaves IAM estáticas en el dispositivo" hacia "identidad por certificado X.509 mTLS + IoT Core + Lambda". Todo lo nuevo es **aditivo y monótono** (compatible con el runner MAD: `terraform apply -auto-approve`, se aborta ante destroy/replace), y la migración es **por fases, no big-bang**.

---

## 0. Principios

- **Sin secretos AWS de larga vida en el borde.** El dispositivo sólo posee material criptográfico de **su** identidad (clave privada del cert X.509, no exportable, generada en el dispositivo). Nunca una `aws_access_key_id`/`secret`.
- **Identidad = certificado por dispositivo.** Un cert ⇄ un Thing ⇄ un `device_id`. La autorización (IoT policy, rol) se evalúa contra atributos del cert/Thing, no contra una credencial compartida de flota.
- **Least privilege literal.** Cada principal (cert, Lambda, Cognito role, GHA OIDC) recibe sólo las acciones y los recursos/prefijos que su función exige, acotados con `Condition` siempre que IoT/IAM lo permitan.
- **Edge-first.** La seguridad nube nunca puede bloquear el conteo local: la cola SQLite + reintentos siguen siendo la fuente de verdad; IoT/MQTT es best-effort.
- **Defensa en profundidad de S3.** Los clips nunca viajan por MQTT ni por la Lambda; el binario va directo dispositivo→S3 sobre TLS con credenciales **temporales** acotadas al prefijo del dispositivo.

---

## 1. Identidad del dispositivo: certificado X.509 + mTLS (sin llaves IAM)

### 1.1 Modelo de identidad

| Objeto IoT | Valor | Notas |
|---|---|---|
| Thing name | `cam-counter-{site_id}-{device_id}` | ej. `cam-counter-sitio-demo-rpi-001`. Slugs cumplen `^[a-z0-9][a-z0-9-]{1,62}$` (sin `#`/`/`). |
| Thing type | `cam-counter-edge-device` | atributos: `site_id`, `device_id`, `release_channel`. |
| Thing group | `cam-counter-fleet` (raíz) → `cam-counter-site-{site_id}` | la policy se adjunta al **grupo**, no a cada cert. |
| Certificate | 1 por dispositivo, **clave privada generada en la Raspberry** (CSR-based o `keys-and-certificate` con clave nunca enviada a AWS si se usa CSR) | preferir CSR: la clave privada nunca sale del dispositivo. |
| IoT Policy | `cam-counter-edge-policy` | adjunta al cert vía el grupo / o al cert directamente. |

**Clave privada:** se genera **en el dispositivo** (`openssl genrsa`/`ec` o el provisioning flow), permisos `0600`, propietario del usuario del contenedor, montada en el contenedor como volumen read-only (`/certs/device.key`). En Docker NO se hornea en la imagen; se monta desde el host. La autenticación a IoT Core es **mTLS**: IoT presenta su cert de servidor (validado contra Amazon Root CA, pinneado en el dispositivo), el dispositivo presenta su cert de cliente; IoT mapea el cert al principal y evalúa la policy.

### 1.2 Provisioning repetible para flota (sencillo)

Dos modos, ambos terraform-friendly:

- **Modo A — JITP / Fleet Provisioning by claim (flota grande, recomendado a futuro):** un *provisioning claim cert* de vida corta (no da acceso a datos, sólo a `RegisterThing`) horneado por el instalador; el dispositivo se auto-registra con un *provisioning template* `cam-counter-fleet-prov` que crea Thing + cert + adjunta policy automáticamente, validando `site_id`/`device_id` contra el patrón de slug. El claim cert se rota y se revoca tras el bootstrap.
- **Modo B — registro manual asistido por script (arranque, 1–N dispositivos):** script `scripts/provision-device.sh` que:
  1. genera clave EC P-256 + CSR en el dispositivo,
  2. `aws iot create-certificate-from-csr` (firma sin ver la clave privada),
  3. crea el Thing `cam-counter-{site}-{device}` con atributos, lo añade al group del sitio,
  4. adjunta el cert al Thing y la policy al cert,
  5. descarga Amazon Root CA + cert de cliente,
  6. emite el `.env` del contenedor con rutas de certs y endpoint.

El template/registro vive en `terraform/modules/iot-core` (nuevo módulo). La flota es reproducible: un `terraform apply` define policy/template/groups; el script per-device sólo materializa la identidad.

---

## 2. Eliminación segura del IAM user `raspberry` (plan de corte)

`arn:aws:iam::950639281773:user/raspberry` y su access key estática se eliminan, pero **sólo después** de que el camino IoT esté verificado en producción para el dispositivo. Corte gradual, reversible hasta el último paso.

**Fase 0 — Preparación (sin tocar al user):**
- Stand-up de IoT Core (thing/policy/group/rule), Lambda ingest, y el camino S3 por credenciales temporales (sección 4). Todo nuevo, aditivo.
- El `sync_runner` gana una bandera `SYNC_TRANSPORT={direct_iam|iot}` (default `direct_iam`). Sin cambio de comportamiento aún.

**Fase 1 — Doble escritura sombra (canary, 1 dispositivo):**
- En `cam-counter-sitio-demo-rpi-001` se activa `SYNC_TRANSPORT=iot`. Eventos van por MQTT→Rule→Lambda→DynamoDB; clips por presigned/IoT-creds. El idempotente por `event_id` (sha1 + conditional put) garantiza que **no se duplica** aunque coexistan ambos caminos un tiempo.
- Verificación: contar eventos en `cam-counter-events`, comparar con SQLite local, validar `clip_key` resoluble. CloudWatch + métricas de la Rule/Lambda.

**Fase 2 — Restricción progresiva de la llave estática (reversible):**
- Adjuntar al user `raspberry` una **deny policy explícita** sobre DynamoDB `PutItem`/`UpdateItem` y S3 `PutObject` (corta el camino directo sin borrar la credencial todavía). Si algo se rompe, se quita el deny.
- Confirmar 24–72 h sin uso: `aws iam get-access-key-last-used` para la access key del user, y CloudTrail `LastUsed` por servicio.

**Fase 3 — Desactivar la access key:**
- `aws iam update-access-key --status Inactive`. Esperar otra ventana de observación. (Reversible: reactivar.)

**Fase 4 — Eliminación (punto de no retorno):**
- `aws iam delete-access-key`, `delete-user-policy`/`detach`, `aws iam delete-user raspberry`.
- Eliminar `~/.aws/credentials` de todos los dispositivos (y de la imagen Docker; nunca debió estar horneada). Confirmar que el contenedor arranca sin `AWS_*` env de credenciales estáticas.
- En Terraform: remover el recurso `aws_iam_user.raspberry` (si está gestionado) en un PR aparte; como MAD aborta ante destroy/replace, el `delete-user` se ejecuta fuera de banda o el recurso se marca para destroy explícito y revisado por humano, no por el runner autónomo.

**Criterio de salida:** `aws iam list-access-keys --user-name raspberry` vacío y el user borrado; 0 eventos `AccessDenied` esperados; todos los dispositivos en `SYNC_TRANSPORT=iot`.

---

## 3. IoT Policy de mínimo privilegio (por-cert, scoped a la identidad)

Policy `cam-counter-edge-policy`. Usa las variables de policy `${iot:Connection.Thing.ThingName}` y `${iot:Certificate.Subject.CommonName}` para que **cada cert sólo pueda actuar como su propio device** — una sola policy, aislamiento por-dispositivo.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ConnectAsOwnThing",
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:us-east-1:950639281773:client/${iot:Connection.Thing.ThingName}"
    },
    {
      "Sid": "PublishEventsOwnTopicOnly",
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/evt/${iot:Connection.Thing.Attributes[site_id]}/${iot:Connection.Thing.ThingName}/crossing"
    },
    {
      "Sid": "ShadowGetUpdateOwnNamedShadow",
      "Effect": "Allow",
      "Action": ["iot:Publish", "iot:Receive"],
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/line-config/get",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/line-config/update",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/commands/*"
      ]
    },
    {
      "Sid": "SubscribeOwnShadowAndCommands",
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/line-config/+",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/commands/+"
      ]
    },
    {
      "Sid": "AssumeRoleForS3UploadCredentials",
      "Effect": "Allow",
      "Action": "iot:AssumeRoleWithCertificate",
      "Resource": "arn:aws:iot:us-east-1:950639281773:rolealias/cam-counter-edge-s3-role-alias"
    }
  ]
}
```

Notas:
- **No hay `iot:*` ni topics comodín de flota.** El device no puede publicar/leer eventos de otro device: el ARN se construye con su propio `ThingName`/`site_id`.
- Topics MQTT con prefijo `cam-counter/evt/...` (estándar de nombres, sección 9).
- Conexión rechazada si el `client_id` ≠ `ThingName` (mitiga suplantación y desconexiones cruzadas).

---

## 4. Subida del clip a S3 sin llaves AWS — **Recomendación: IoT Credential Provider** (rol acotado)

Se evaluaron dos opciones:

| Opción | Cómo | Pros | Contras |
|---|---|---|---|
| **A. Presigned PUT URL** (device pide URL vía topic→Lambda) | El device publica `cam-counter/cmd/{thing}/presign-req`; una Lambda valida y devuelve una presigned PUT URL para la clave exacta. | Cero credenciales en el device; control central de la clave. | Round-trip MQTT por clip; Lambda en el camino caliente; URLs con TTL; reintentos de clips grandes complican; otra Lambda que mantener. |
| **B. IoT Credential Provider + rol acotado** ✅ | El device llama al *credentials endpoint* de IoT con su **cert mTLS**, recibe credenciales STS temporales del rol `cam-counter-edge-s3-role` (vía role alias `cam-counter-edge-s3-role-alias`), y sube directo a S3 con `PutObject`. | Reusa la **misma identidad de cert** (sin segundo secreto), sin Lambda en el camino, soporta multipart/reintentos nativos de boto3, credenciales de vida corta auto-rotadas, scoping fuerte por `Condition` en el rol. | El rol debe acotarse con cuidado al prefijo del device. |

**Se recomienda la Opción B (IoT Credential Provider).** Encaja con "sin llaves estáticas", reusa el cert ya emitido, no añade una Lambda en el camino de binarios grandes, y boto3 puede subir multipart con reintentos (edge-first). El device obtiene credenciales temporales **sólo** para escribir su propio prefijo de media.

### 4.1 Rol `cam-counter-edge-s3-role` + role alias

El role alias `cam-counter-edge-s3-role-alias` apunta a `cam-counter-edge-s3-role` con `durationSeconds` corto (p. ej. 3600). El rol confía en `credentials.iot.amazonaws.com`. El scoping por-device se logra con la variable de sesión IoT `${credentials-iot:ThingName}` en el `Condition`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PutClipsOwnDevicePrefixOnly",
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::cam-counter-media-950639281773/media/*",
      "Condition": {
        "StringLike": {
          "s3:prefix": "media/*/${credentials-iot:ThingName}/*"
        },
        "Bool": { "aws:SecureTransport": "true" }
      }
    }
  ]
}
```

- **Sólo `PutObject`** (write-only de clips). El device **no** necesita `GetObject` ni `ListBucket`; la reproducción la sirve el dashboard vía presigned GET generada por backend (sección 6). Menos superficie.
- Acotado al prefijo `media/.../{thing}/...` → un device no puede sobreescribir clips de otro.
- `aws:SecureTransport=true` → sólo TLS.
- Bucket `cam-counter-media-950639281773`: privado, BlockPublicAccess all, SSE-S3, bucket-owner-enforced, bucket policy deny non-TLS (ya existente; se mantiene).

Clave del objeto (estándar, ya en uso): `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.mp4`. El `clip_key` viaja en el evento MQTT; la Lambda lo persiste en el item DynamoDB (no el binario).

---

## 5. Rol de ejecución de la Lambda de ingest (mínimo privilegio)

Lambda `cam-counter-ingest-events` (IoT Rule `cam-counter-evt-to-ddb` la invoca). Valida el payload contra `contracts/crossing_event.schema.json`, hace **put idempotente** (conditional put por `event_id` determinista) en `cam-counter-events`, y opcionalmente upsert de last-seen en `cam-counter-devices`.

Rol `cam-counter-ingest-events-role`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteEventsTableOnly",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-events",
        "arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-devices"
      ]
    },
    {
      "Sid": "MediaPrefixOnly",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::cam-counter-media-950639281773/media/*",
      "Condition": { "Bool": { "aws:SecureTransport": "true" } }
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:950639281773:log-group:/aws/lambda/cam-counter-ingest-events:*"
    }
  ]
}
```

- **Sólo `PutItem`/`UpdateItem`** en las dos tablas — sin `DeleteItem`, sin `Scan`, sin `Query` (la ingesta no lee tablas; si necesitara head-check de idempotencia, el conditional put lo cubre sin lectura).
- **S3 acotado a `media/`** prefijo, sólo `GetObject`/`PutObject` (p. ej. para validar HEAD del clip o adjuntar metadata). Sin `DeleteObject`, sin acceso a otros buckets (`fleet-releases`, `tfstate`, `rpi-artifacts` quedan fuera).
- No GSI ARNs porque la ingesta sólo escribe; las GSI son para lectura del dashboard.
- Sin `iot:*`: la Rule invoca la Lambda; la Lambda no reentra a IoT.
- DLQ (`cam-counter-ingest-dlq`, SQS) para eventos malformados; permiso `sqs:SendMessage` añadido sólo a esa cola.

---

## 6. Auth del dashboard (Next.js / Amplify) — Cognito + presigned GET

### 6.1 Cognito User Pool de operadores

- User pool `cam-counter-operators` con **sólo usuarios creados por admin** (`AdminCreateUserConfig`, self-signup deshabilitado), MFA TOTP **obligatorio**, política de contraseña fuerte, tokens de vida corta, refresh token rotado.
- App client `cam-counter-dashboard-client` (sin secret, SPA/Amplify; PKCE).
- Grupos: `cam-counter-operators` (lectura de toda la flota), `cam-counter-admins`. Identity Pool (`cam-counter-dashboard-idpool`) mapea a un rol autenticado **read-only**.

### 6.2 Acceso a datos del dashboard (no credenciales directas en el browser)

El frontend Next.js **no** habla DynamoDB/S3 directo. Llama a un backend autenticado (API Gateway + Lambda `cam-counter-dashboard-api`, autorizador Cognito JWT, o Amplify SSR con verificación del JWT). Ese backend:
- `Query` sobre `cam-counter-events` (y GSI1 por sitio) y `cam-counter-devices` (GSI1 por canal) — **read-only** (`Query`/`GetItem`/`BatchGetItem`, nunca write).
- Genera **presigned GET URL** (TTL corto, p. ej. 300 s) para el `clip_key` del evento seleccionado → reproducción del MP4. El device subió con write-only; el dashboard lee con presigned GET firmada server-side.

Rol del dashboard-api `cam-counter-dashboard-api-role`: `dynamodb:Query/GetItem/BatchGetItem` sobre las dos tablas + sus GSI; `s3:GetObject` sobre `media/*`; logs. **Sin** PutItem/PutObject/Delete.

El rol autenticado del Identity Pool (si el browser necesitara algo directo) se mantiene mínimo o inexistente; preferimos todo el acceso a datos mediado por el backend con JWT.

---

## 7. Cifrado en tránsito y en reposo

**En tránsito:**
- MQTT sobre **TLS 1.2+ con mTLS** (IoT Core, puerto 8883). Device pinnea Amazon Root CA.
- S3: bucket policy deny `aws:SecureTransport=false`; uploads y presigned URLs sólo HTTPS.
- IoT Credential Provider endpoint y STS: HTTPS.
- Dashboard: HTTPS forzado (Amplify/CloudFront, HSTS), Cognito sobre TLS.
- API interna del dispositivo (`:8088`) es same-origin localhost; si se expone fuera del host, debe ir tras TLS/reverse-proxy (fuera de alcance de la nube pero anotado).

**En reposo:**
- S3 `cam-counter-media-950639281773`: SSE-S3 (AES-256) ya activo. (Opción de endurecer a SSE-KMS con CMK `cam-counter-media-key` si se requiere control de acceso por clave + audit de descifrado.)
- DynamoDB `cam-counter-events` / `cam-counter-devices`: encryption at rest activada (AWS owned o KMS managed; recomendado KMS managed para auditoría).
- Claves privadas de cert en el device: en disco con `0600`, montadas read-only al contenedor, fuera de la imagen Docker, fuera de git.
- Cognito: datos cifrados en reposo por el servicio. Tokens nunca en localStorage persistente sin necesidad; usar almacenamiento seguro/cookies httpOnly cuando aplique (SSR).
- Secrets de build/CI: nunca en la imagen; GHA usa OIDC (sin llaves), no secrets estáticos de AWS.

---

## 8. Revocación de un certificado comprometido

Plan de respuesta para un device/cert comprometido:

1. **Revocar inmediato:** `aws iot update-certificate --certificate-id <id> --new-status REVOKED`. IoT Core rechaza nuevas conexiones mTLS de ese cert; las activas se cortan en la siguiente reautenticación (forzar con desconexión).
2. **Cortar credenciales temporales:** como el cert está revocado, `iot:AssumeRoleWithCertificate` falla → no puede obtener credenciales STS para S3. Las credenciales temporales ya emitidas expiran en ≤ `durationSeconds` (3600 s); para corte inmediato, adjuntar al rol `cam-counter-edge-s3-role` una deny temporal o revocar sesiones STS por política (`aws:TokenIssueTime`).
3. **Desadjuntar policy y limpiar:** `iot:DetachPolicy` / `DetachThingPrincipal` del cert revocado; opcionalmente `DeleteCertificate` tras desadjuntar.
4. **Re-provisionar:** generar **nueva** clave + CSR en el device (sección 1.2), emitir cert nuevo, adjuntar al mismo Thing. El `device_id`/Thing se conserva; sólo cambia el material criptográfico.
5. **Auditoría:** revisar CloudTrail/IoT logs por publicaciones/uploads anómalos del cert comprometido; el scoping por prefijo limita el blast radius a los datos de ese device.
6. **Rotación proactiva:** política de rotación de certs (p. ej. anual) vía el provisioning flow; alertas de cert próximo a expirar.

(Opcional avanzado: OCSP/CRL no es nativo en IoT mTLS; la revocación autoritativa es el estado `REVOKED` del cert en IoT Core, que es lo que aplicamos.)

---

## 9. Estándar de nombres (todos los recursos)

Prefijo global `cam-counter-`. Slugs `^[a-z0-9][a-z0-9-]{1,62}$`, sin `#`/`/`.

| Categoría | Patrón | Ejemplo |
|---|---|---|
| IoT Thing | `cam-counter-{site_id}-{device_id}` | `cam-counter-sitio-demo-rpi-001` |
| IoT Thing type | `cam-counter-edge-device` | — |
| IoT Thing group | `cam-counter-fleet`, `cam-counter-site-{site_id}` | `cam-counter-site-sitio-demo` |
| IoT Policy | `cam-counter-edge-policy` | — |
| Provisioning template | `cam-counter-fleet-prov` | — |
| IoT Rule | `cam-counter-evt-to-ddb` | — |
| Role alias (creds device) | `cam-counter-edge-s3-role-alias` | — |
| Topic eventos (MQTT) | `cam-counter/evt/{site_id}/{thing}/crossing` | `cam-counter/evt/sitio-demo/cam-counter-sitio-demo-rpi-001/crossing` |
| Topic comando | `cam-counter/cmd/{thing}/{action}` | `.../snapshot` |
| Named shadow (config) | `line-config` | `$aws/things/{thing}/shadow/name/line-config` |
| Named shadow (comandos) | `commands` | — |
| Lambda ingest | `cam-counter-ingest-events` | — |
| Lambda dashboard API | `cam-counter-dashboard-api` | — |
| DLQ | `cam-counter-ingest-dlq` | — |
| IAM rol Lambda ingest | `cam-counter-ingest-events-role` | — |
| IAM rol creds device | `cam-counter-edge-s3-role` | — |
| IAM rol dashboard API | `cam-counter-dashboard-api-role` | — |
| Cognito user pool | `cam-counter-operators` | — |
| Cognito app client | `cam-counter-dashboard-client` | — |
| Cognito identity pool | `cam-counter-dashboard-idpool` | — |
| Cognito grupos | `cam-counter-operators`, `cam-counter-admins` | — |
| Amplify app | `cam-counter-dashboard` | — |
| Clave S3 clip | `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.mp4` | — |
| Imagen Docker edge | `cam-counter-edge:{version}` | `cam-counter-edge:1.4.2` |
| Contenedor | `cam-counter-edge-{device_id}` | `cam-counter-edge-rpi-001` |
| Env vars (contenedor) | `CAM_COUNTER_*` | `CAM_COUNTER_SITE_ID`, `CAM_COUNTER_DEVICE_ID`, `CAM_COUNTER_IOT_ENDPOINT`, `CAM_COUNTER_CERT_PATH`, `CAM_COUNTER_KEY_PATH`, `CAM_COUNTER_ROOT_CA_PATH`, `CAM_COUNTER_ROLE_ALIAS`, `CAM_COUNTER_SYNC_TRANSPORT` |
| Terraform módulo nuevo | `terraform/modules/iot-core`, `.../lambda-ingest`, `.../cognito-dashboard`, `.../amplify-dashboard` | — |

Env del contenedor: lleva sólo **referencias y rutas**, nunca secretos AWS. Las claves de cert se montan como volúmenes (`-v /opt/cam-counter/certs:/certs:ro`); IoT endpoint = `a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com`.

---

## 10. Least privilege en el resto (resumen)

- **GHA OIDC** (`cam-counter-gha-plan` / `cam-counter-gha-deploy`): ya por OIDC, sin llaves estáticas; `plan` read-only, `deploy` acotado a los recursos del proyecto. Mantener; añadir permisos IoT/Lambda/Cognito sólo en `deploy`, scoped a recursos `cam-counter-*`.
- **Buckets no relacionados** (`fleet-releases`, `tfstate`, `rpi-artifacts`) **no** aparecen en ningún rol del device/Lambda/dashboard. `rpi-artifacts` reservado: no se toca.
- **Device shadow** (`line-config`): la edición desde la nube se autoriza vía el dashboard backend (operador autenticado) que hace `iot:UpdateThingShadow` sobre el named shadow del thing seleccionado; el device sólo lee/actualiza **su** shadow (sección 3). La UI local sigue editando el SQLite; el `ConfigWatcher` reconcilia shadow ⇄ SQLite con `config_version` como árbitro (last-writer-wins por versión).
- **Separación de identidades:** device (cert) ≠ Lambda (rol) ≠ dashboard (Cognito + rol read-only) ≠ CI (OIDC). Ninguna comparte credenciales; cada una falla cerrada.
- **Migración monótona:** todo lo anterior se añade sin destruir recursos existentes; el corte del user `raspberry` (sección 2, Fase 4) es el único destroy y se ejecuta revisado por humano, no por el runner MAD autónomo.