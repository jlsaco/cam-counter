# Estandar de nombres

Estandar de nombres y etiquetas para **cam-counter** (recursos existentes + nuevos de AWS IoT Core). Cuenta `950639281773`, region `us-east-1`. Endpoint IoT: `a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com`.

## Principios transversales

- Prefijo global obligatorio: `cam-counter-`.
- Slugs (`site_id`, `device_id`, `camera_id`) cumplen `^[a-z0-9][a-z0-9-]{1,62}$`. Prohibidos `#` y `/` (el `#` delimita claves compuestas en DynamoDB; el `/` delimita topics MQTT y claves S3).
- Buckets/recursos AWS en minuscula-kebab. Recursos AWS globales (S3) llevan sufijo de cuenta `-950639281773`.
- Nunca se usa `/` ni `#` dentro de un slug; si dos slugs se concatenan en un nombre AWS se unen con `-` (ej. `sitio-demo-rpi-001`).
- `event_id` determinista (sha1 de la identidad) para idempotencia; aparece como ultimo segmento de claves S3 y SK DynamoDB.
- Cambios de infra son aditivos/monotonos: ningun nombre nuevo colisiona ni reemplaza uno existente.

---

## 1. IoT Thing name

| Patron | Ejemplo | Regla |
|---|---|---|
| `cam-counter-edge-{site_id}-{device_id}` | `cam-counter-edge-sitio-demo-rpi-001` | Un Thing por dispositivo fisico. Reusa EXACTAMENTE la convencion del rol IAM por dispositivo (`cam-counter-edge-{site}-{device}`) para que Thing, rol y certificado se mapeen 1:1 por nombre. `{site_id}` y `{device_id}` son slugs validos; longitud total <=128 (limite de Thing name). |

---

## 2. Thing Type

| Patron | Ejemplo | Regla |
|---|---|---|
| `cam-counter-{clase-hardware}` | `cam-counter-rpi5-hailo8` | Un Thing Type por arquitectura de borde soportada. Describe HW/runtime (Raspberry Pi 5 + Hailo-8). Atributos searchable del Type: `site_id`, `device_id`, `camera_id`, `release_channel`, `hw_rev`. Inmutable una vez creado; nueva clase de HW = nuevo Type. |

---

## 3. Thing Groups

| Patron | Ejemplo | Regla |
|---|---|---|
| Raiz: `cam-counter-fleet` | `cam-counter-fleet` | Grupo estatico raiz que contiene TODA la flota. Punto de anclaje para jobs/OTA a nivel flota. |
| Por sitio: `cam-counter-site-{site_id}` | `cam-counter-site-sitio-demo` | Grupo estatico, hijo de `cam-counter-fleet`. Agrupa todos los Things de un sitio. Se usa para policy/permiso y consultas por sitio. |
| Por canal (release): `cam-counter-channel-{release_channel}` | `cam-counter-channel-stable` | Grupo **dinamico** (query `attributes.release_channel:stable`) para orquestar OTA por canal, consistente con `CHANNEL#{release_channel}` de `cam-counter-devices` GSI1. Canales: `stable`, `beta`, `canary`. |

---

## 4. IoT Policy names

| Patron | Ejemplo | Regla |
|---|---|---|
| Policy por flota (una, parametrizada): `cam-counter-edge-device-policy` | `cam-counter-edge-device-policy` | UNA sola policy adjunta a TODOS los certificados de dispositivo; el aislamiento entre dispositivos se logra con variables de policy (`${iot:Connection.Thing.ThingName}`), no con una policy por device. Versionada en Terraform. |
| Policy de provisioning (claim): `cam-counter-provisioning-claim-policy` | `cam-counter-provisioning-claim-policy` | Adjunta al certificado claim usado SOLO durante fleet provisioning. Permite unicamente `iot:Connect`/`Publish`/`Subscribe`/`Receive` sobre los topics `$aws/provisioning-templates/*` y `$aws/certificates/*`. |
| Provisioning template: `cam-counter-fleet-provisioning` | `cam-counter-fleet-provisioning` | Plantilla de Fleet Provisioning que crea Thing + adjunta policy + grupos a partir de los parametros `{site_id,device_id,camera_id}`. |

### Plantilla de policy por-cert (variables de policy)

La policy `cam-counter-edge-device-policy` usa `${iot:Connection.Thing.ThingName}` para que cada certificado solo opere sobre los topics de SU propio Thing. El Thing name es `cam-counter-edge-{site}-{device}`, asi que el `topic-prefix` derivado de el aisla por dispositivo.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Connect",
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:us-east-1:950639281773:client/${iot:Connection.Thing.ThingName}",
      "Condition": {
        "Bool": { "iot:Connection.Thing.IsAttached": "true" }
      }
    },
    {
      "Sid": "PublishDeviceTopics",
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.ThingName}/events",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.ThingName}/telemetry",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.ThingName}/status",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.ThingName}/cmd-ack",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/*"
      ]
    },
    {
      "Sid": "SubscribeDeviceTopics",
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topicfilter/cam-counter/${iot:Connection.Thing.ThingName}/cmd",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/shadow/*"
      ]
    },
    {
      "Sid": "ReceiveDeviceTopics",
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.ThingName}/cmd",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/*"
      ]
    }
  ]
}
```

Regla: `client-id` MQTT == Thing name (lo exige el `Sid:Connect` con `IsAttached`). Nunca usar comodines fuera del prefijo `${iot:Connection.Thing.ThingName}`.

---

## 5. Certificados / llaves y donde viven

| Patron | Ejemplo | Regla |
|---|---|---|
| Directorio en device: `/etc/cam-counter/certs/{thing_name}/` | `/etc/cam-counter/certs/cam-counter-edge-sitio-demo-rpi-001/` | Raiz unica de secretos en el host; montada read-only en Docker. Permisos `0700` dir, `0600` archivos, owner del proceso edge. |
| Cert de cliente: `device.cert.pem` | `device.cert.pem` | Certificado X.509 del dispositivo (PEM). |
| Llave privada: `device.private.key` | `device.private.key` | Llave privada; NUNCA sale del device, NUNCA en git, NUNCA en la imagen Docker. `0600`. |
| Cadena CA: `AmazonRootCA1.pem` | `AmazonRootCA1.pem` | Root CA de Amazon para validar el endpoint ATS. |
| Cert claim (provisioning): `claim.cert.pem` / `claim.private.key` | `claim.cert.pem` | Par claim COMPARTIDO de fleet provisioning; se entrega solo para el primer arranque y se descarta tras obtener el cert permanente. |
| Backup en S3 (solo metadata cert, NO llave): `cam-counter-rpi-artifacts-950639281773` | (RESERVADO, no tocar) | Las llaves privadas jamas se suben a S3. El registro `certificateId` se guarda en `cam-counter-devices` (item del device). |

---

## 6. Topics MQTT

Espacio de nombres base: `cam-counter/{thing_name}/...`. `{thing_name}` = `cam-counter-edge-{site}-{device}`. Direccion: pub = device->cloud salvo `cmd` (cloud->device).

| Proposito | Patron (topic) | Ejemplo | Regla |
|---|---|---|---|
| Eventos de conteo | `cam-counter/{thing_name}/events` | `cam-counter/cam-counter-edge-sitio-demo-rpi-001/events` | Device PUB. Payload = `CrossingEvent` (contracts/crossing_event.schema.json). IoT Rule `cam-counter-events-ingest` enruta a Lambda. QoS 1. |
| Telemetria/metricas | `cam-counter/{thing_name}/telemetry` | `.../telemetry` | Device PUB periodico (fps, cpu, temp, hailo). Best-effort QoS 0. |
| Estado conexion (LWT) | `cam-counter/{thing_name}/status` | `.../status` | Device PUB con Last Will (`online`/`offline`). Retained. |
| Comando cloud->device | `cam-counter/{thing_name}/cmd` | `.../cmd` | Device SUB. Comandos puntuales (`restart`, `snapshot`, `reload-line`). |
| Ack de comando | `cam-counter/{thing_name}/cmd-ack` | `.../cmd-ack` | Device PUB tras ejecutar un `cmd`; correla por `cmd_id`. |
| Shadow (reservado AWS) | `$aws/things/{thing_name}/shadow/...` | `$aws/things/cam-counter-edge-sitio-demo-rpi-001/shadow/name/config/update` | Topics reservados de AWS para shadows; ver seccion 7. No re-nombrar. |

Regla: el primer segmento SIEMPRE es `cam-counter` (namespace), el segundo SIEMPRE el `thing_name` (aislamiento por policy var). Nada de payloads binarios grandes: los clips MP4 van a S3, no a MQTT.

---

## 7. Named Shadows

| Proposito | Patron (shadow name) | Topic update | Regla |
|---|---|---|---|
| Config de linea-umbral | `config` | `$aws/things/{thing_name}/shadow/name/config/update` | Named shadow. `desired` = empujado por la nube (linea-umbral, `config_version`); `reported` = lo que el edge aplico. El `ConfigWatcher` reconcilia con SQLite local. Payload sigue `contracts/line_config.schema.json`. |
| Operacion/comandos durables | `ops` | `$aws/things/{thing_name}/shadow/name/ops/update` | Estado deseado de operacion (canal OTA objetivo, modo). Complementa el OTA pull-based; no lo reemplaza. |
| Estado de salud reportado | `health` | `$aws/things/{thing_name}/shadow/name/health/update` | Solo `reported` por el device (version firmware, soak status). |

Regla: NO usar el shadow clasico (sin nombre); todo named para versionar dominios por separado. Nombres de shadow son slugs cortos (`config`, `ops`, `health`).

---

## 8. Funciones Lambda

| Patron | Ejemplo | Regla |
|---|---|---|
| `cam-counter-{dominio}-{accion}` | `cam-counter-events-ingest` | Procesa eventos MQTT: valida contra `crossing_event.schema.json`, conditional put idempotente en `cam-counter-events`, enlaza `clip_key`. Triggered por IoT Rule. |
| `cam-counter-clip-presign` | `cam-counter-clip-presign` | Genera presigned URL de lectura del clip en `cam-counter-media-950639281773` para la app Next.js. |
| `cam-counter-shadow-fanout` | `cam-counter-shadow-fanout` | (Opcional) Propaga cambios de config de la UI nube a los named shadows. |
| `cam-counter-device-register` | `cam-counter-device-register` | Hook de fleet provisioning: escribe el item en `cam-counter-devices` al vincular un device. |

Regla: nombre = `cam-counter-` + dominio (`events`,`clip`,`shadow`,`device`,`api`) + accion verbo. <=64 chars. Un alias `:live` por funcion para despliegues.

---

## 9. IAM roles de Lambda

| Patron | Ejemplo | Regla |
|---|---|---|
| `cam-counter-lambda-{funcion-corta}-role` | `cam-counter-lambda-events-ingest-role` | Un rol de ejecucion por funcion Lambda; least-privilege. Politica inline con prefijo `cam-counter-lambda-{funcion}-policy`. |
| `cam-counter-lambda-clip-presign-role` | `cam-counter-lambda-clip-presign-role` | Solo `s3:GetObject` sobre `cam-counter-media-950639281773/media/*`. |
| `cam-counter-lambda-device-register-role` | `cam-counter-lambda-device-register-role` | Solo `dynamodb:PutItem`/`UpdateItem` sobre `cam-counter-devices`. |

Regla: no reusar un rol entre funciones. Distinto del rol de borde `cam-counter-edge-{site}-{device}` (ese ahora solo gobierna acceso S3 residual durante migracion y se retira al matar la llave `raspberry`).

---

## 10. API Gateway / AppSync

| Patron | Ejemplo | Regla |
|---|---|---|
| REST API: `cam-counter-fleet-api` | `cam-counter-fleet-api` | API HTTP (API Gateway v2) que sirve a la app Next.js: lista devices/eventos y devuelve presigned URLs. Stage `prod`. |
| Stage: `prod` | `cam-counter-fleet-api/prod` | Un solo stage de produccion; nombres de stage sin prefijo (van bajo la API). |
| (Si AppSync) GraphQL API: `cam-counter-fleet-gql` | `cam-counter-fleet-gql` | Alternativa GraphQL; misma raiz `cam-counter-fleet-`. Autorizacion via Cognito User Pool. |
| Authorizer: `cam-counter-fleet-cognito-authorizer` | `cam-counter-fleet-cognito-authorizer` | JWT authorizer apuntando al User Pool. |

---

## 11. Cognito

| Recurso | Patron | Ejemplo | Regla |
|---|---|---|---|
| User Pool | `cam-counter-fleet-users` | `cam-counter-fleet-users` | Pool de operadores de la flota. MFA opcional, email como username. |
| User Pool domain | `cam-counter-fleet-{cuenta}` | `cam-counter-fleet-950639281773` | Dominio Hosted UI (global, requiere sufijo unico de cuenta). |
| App client (web Next.js) | `cam-counter-fleet-web-client` | `cam-counter-fleet-web-client` | Client sin secret (SPA/Next.js public), Auth Code + PKCE. |
| App client (server/SSR) | `cam-counter-fleet-server-client` | `cam-counter-fleet-server-client` | Client confidencial para SSR de Next.js si aplica. |
| Identity Pool | `cam-counter-fleet-identity` | `cam-counter-fleet-identity` | Federa el User Pool a credenciales AWS temporales (ej. acceso directo a presign si no via API). |
| Rol autenticado | `cam-counter-fleet-cognito-auth-role` | `cam-counter-fleet-cognito-auth-role` | Rol IAM para identidades autenticadas; least-privilege lectura. |

---

## 12. App Amplify (Next.js)

| Recurso | Patron | Ejemplo | Regla |
|---|---|---|---|
| Amplify App | `cam-counter-fleet-console` | `cam-counter-fleet-console` | App Next.js de consola de flota en AWS Amplify Hosting. |
| Branch/env prod | `main` -> env `prod` | `cam-counter-fleet-console (main)` | Branch `main` despliega prod; ramas de feature => previews. |
| Dominio | `fleet.cam-counter.<dominio>` | `fleet.cam-counter.example.com` | Subdominio dedicado de consola. |

---

## 13. Imagenes y servicios Docker (compose)

| Recurso | Patron | Ejemplo | Regla |
|---|---|---|---|
| Imagen edge | `cam-counter/edge:{tag}` | `cam-counter/edge:1.4.0-arm64` | Contiene runtime edge (cv2 + HailoRT). Base ARM64, paginas 16KB. Tag = version semver + `-arm64`. |
| Imagen api/ui | `cam-counter/api:{tag}` | `cam-counter/api:1.4.0-arm64` | FastAPI + UI compilada. |
| Imagen sync | `cam-counter/sync:{tag}` | `cam-counter/sync:1.4.0-arm64` | Publica eventos por MQTT (reemplaza escritura directa a DynamoDB) y sube clips a S3 via rol/cert. |
| Servicio compose edge | `edge` | `service: edge` | Nombre de servicio corto en `docker-compose.yml`; container_name `cam-counter-edge`. Passthrough `/dev/hailo0`. |
| Servicio compose api | `api` | `service: api`, container_name `cam-counter-api` | Expone `:8088` same-origin. |
| Servicio compose sync | `sync` | `service: sync`, container_name `cam-counter-sync` | Monta certs read-only desde `/etc/cam-counter/certs/{thing_name}`. |
| Container name | `cam-counter-{servicio}` | `cam-counter-edge` | `container_name` = prefijo + servicio para identificacion en el host. |
| Registry | `ghcr.io/jlsaco/cam-counter/{img}` o ECR `950639281773.dkr.ecr.us-east-1.amazonaws.com/cam-counter/{img}` | `ghcr.io/jlsaco/cam-counter/edge:1.4.0-arm64` | Repo de imagenes consistente con el monorepo. |

Regla: nombres de servicio compose son cortos (`edge`,`api`,`sync`); el prefijo `cam-counter-` vive en `container_name` y en el nombre de imagen. Hailo: el servicio que usa el chip declara `devices: [/dev/hailo0:/dev/hailo0]` y `group_add` del grupo `hailo`.

---

## 14. Variables de entorno del dispositivo

Prefijo `CAM_COUNTER_` (o `CC_`) para evitar colisiones; mayusculas SNAKE_CASE. Viven en `.env` (no en git) montado por compose.

| Variable | Ejemplo | Regla |
|---|---|---|
| `CC_SITE_ID` | `sitio-demo` | Slug del sitio; valida `^[a-z0-9][a-z0-9-]{1,62}$`. |
| `CC_DEVICE_ID` | `rpi-001` | Slug del dispositivo. |
| `CC_CAMERA_ID` | `cam-entrada` | Slug de camara. |
| `CC_THING_NAME` | `cam-counter-edge-sitio-demo-rpi-001` | Derivable (`cam-counter-edge-${CC_SITE_ID}-${CC_DEVICE_ID}`); igual a client-id MQTT. |
| `CC_RELEASE_CHANNEL` | `stable` | Canal OTA (`stable`/`beta`/`canary`); alimenta el grupo dinamico. |
| `CC_IOT_ENDPOINT` | `a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com` | Endpoint ATS IoT Core. |
| `CC_AWS_REGION` | `us-east-1` | Region fija. |
| `CC_AWS_ACCOUNT_ID` | `950639281773` | Cuenta. |
| `CC_CERT_DIR` | `/etc/cam-counter/certs/cam-counter-edge-sitio-demo-rpi-001` | Dir de certs montado RO. |
| `CC_CERT_FILE` | `${CC_CERT_DIR}/device.cert.pem` | Cert cliente. |
| `CC_KEY_FILE` | `${CC_CERT_DIR}/device.private.key` | Llave privada. |
| `CC_CA_FILE` | `${CC_CERT_DIR}/AmazonRootCA1.pem` | Root CA. |
| `CC_MEDIA_BUCKET` | `cam-counter-media-950639281773` | Bucket de clips. |
| `CC_TOPIC_BASE` | `cam-counter/cam-counter-edge-sitio-demo-rpi-001` | Prefijo de topics; segmentos `events`/`telemetry`/`status`/`cmd`/`cmd-ack` se anexan. |
| `CC_API_PORT` | `8088` | Puerto UI same-origin. |
| `CC_DB_PATH` | `/var/lib/cam-counter/cam-counter.db` | SQLite WAL compartido. |

Regla: ninguna variable contiene credenciales estaticas AWS (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` ELIMINADAS); la identidad es el cert X.509. Variables que apuntan a slugs se validan al arranque.

---

## 15. Tags AWS comunes

Todos los recursos gestionados por Terraform llevan este conjunto base (default_tags del provider). Claves en `PascalCase`.

| Tag | Patron / valores | Ejemplo | Regla |
|---|---|---|---|
| `Project` | `cam-counter` | `cam-counter` | Constante; identifica el proyecto. |
| `Environment` | `prod` \| `dev` | `prod` | Entorno; hoy solo `prod`. |
| `ManagedBy` | `terraform` | `terraform` | Todo lo de IaC. Recursos creados por provisioning llevan `provisioning`. |
| `Component` | `edge`\|`iot`\|`events`\|`media`\|`fleet-console`\|`api`\|`ota` | `iot` | Subsistema. |
| `Repo` | `jlsaco/cam-counter` | `jlsaco/cam-counter` | Origen del codigo. |
| `CostCenter` | `cam-counter` | `cam-counter` | Atribucion de costo. |
| `SiteId` | `{site_id}` (solo recursos por-device) | `sitio-demo` | Solo en Things/certs/items por dispositivo. |
| `DeviceId` | `{device_id}` (solo por-device) | `rpi-001` | Idem. |
| `ReleaseChannel` | `stable`\|`beta`\|`canary` | `stable` | En recursos asociados a un canal. |

Regla: claves de tag en `PascalCase`, valores en kebab/slug minuscula. `default_tags` del provider AWS aplica `Project`, `Environment`, `ManagedBy`, `Repo`, `CostCenter` automaticamente; `Component`/`SiteId`/`DeviceId`/`ReleaseChannel` se anexan por recurso.

---

## 16. Recursos existentes (referencia, NO renombrar)

| Recurso | Nombre actual | Categoria |
|---|---|---|
| S3 clips | `cam-counter-media-950639281773` | media |
| S3 OTA | `cam-counter-fleet-releases-950639281773` | ota |
| S3 tfstate | `cam-counter-tfstate-950639281773` | infra |
| S3 reservado | `cam-counter-rpi-artifacts-950639281773` | reservado (no tocar) |
| DynamoDB eventos | `cam-counter-events` (PK=`CAM#{site}#{device}#{camera}`, SK=`TS#{ts_event_ms:013d}#{event_id}`) | events |
| DynamoDB devices | `cam-counter-devices` (PK=`DEVICE#{device_id}`, GSI1 `CHANNEL#{release_channel}`) | registry |
| DynamoDB lock | `cam-counter-tfstate-lock` | infra |
| IAM rol borde | `cam-counter-edge-{site}-{device}` (ej. `cam-counter-edge-sitio-demo-rpi-001`) | edge |
| IAM OIDC GHA | `cam-counter-gha-plan`, `cam-counter-gha-deploy` | ci |
| Clave S3 clip | `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.mp4` | media |

Regla de migracion: lo nuevo (IoT, Lambda, Cognito, Amplify) se ADJUNTA sin tocar estos nombres. El rol `cam-counter-edge-{site}-{device}` y la llave del IAM user `raspberry` se retiran SOLO despues de validar el camino MQTT->Lambda->DynamoDB; ningun `terraform apply` destruye/reemplaza recursos existentes (estado aditivo/monotono).