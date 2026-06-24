# Estándar de nombres — `cam-counter` (canon de nomenclatura IoT + entorno)

> **Fuente de verdad ÚNICA** de nomenclatura para toda la pila de PRs de la iniciativa
> **AWS IoT Core**. Si una spec antigua, un comentario o un borrador de Terraform contradice
> este archivo, **manda este archivo**. Este documento es **solo-doc**: NO crea, modifica ni
> borra ningún recurso AWS ni HCL. Su trabajo es fijar **un** nombre canónico por cosa antes
> de escribir Terraform o tocar el dispositivo.
>
> Complementa a [`CLAUDE.md`](../CLAUDE.md) (arquitectura y convenciones del monorepo) y a
> [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). Las convenciones de identificadores (§3 de
> `CLAUDE.md`), coordenadas (§4), regla de los tres buckets (§7) y contratos (§8) **se
> heredan tal cual**; aquí se extiende esa base a los recursos nuevos de IoT.

**Alcance de este WP (WP01):** SOLO nomenclatura. NO reconcilia contratos versionados
(`contracts/*.json`) — eso es trabajo de WP02. NO propone tocar la identidad admin
`raspberry` ni `~/.aws` (fuera de alcance — guardarraíl). NO toca recursos en marcha.

---

## 0. Reglas generales

1. **Prefijo de producto**: TODO recurso AWS del producto empieza por `cam-counter-`
   (cuenta `950639281773`, región `us-east-1`). Único prefijo; sin excepciones.
2. **Separador canónico = guion simple `-`** (kebab-case) en nombres de recursos AWS y de
   entorno-de-nombre. **PROHIBIDO el doble guion bajo `__`** y el `camelCase` en nombres de
   recursos. Excepción justificada y única: **IoT Rules** usan **`snake_case`** con prefijo
   `cam_counter_` porque el motor de reglas de IoT **no admite `-`** en el nombre de la regla
   (ver §6 y la tabla de divergencias §10).
3. **Slugs de identidad** (`site_id`, `device_id`, `camera_id`) cumplen el regex
   **`^[a-z0-9][a-z0-9-]{1,62}$`** (ASCII minúscula; sin `#`, sin `/`; ver `CLAUDE.md` §3).
   **Validar el regex ANTES** de componer cualquier nombre de recurso, clave de S3, clave de
   DynamoDB o topic MQTT. Los nombres **compuestos** (p. ej. `cam-counter-casa-rpi-cam`)
   pueden superar 63 caracteres: el regex se aplica a **cada slug componente**, no al nombre
   final compuesto.
4. **Prefijo de entorno canónico = `CAMCOUNTER_*`** (el que LEE el código real). El prefijo
   `CC_*` **NO existe en el repo y queda PROHIBIDO** (ver §4).
5. **Coherencia HCL↔doc (gate)**: el **valor por defecto** (`default`) de cada variable de
   nomenclatura en cada módulo Terraform **DEBE ser idéntico** al valor canónico de este
   documento (ver §11). Cualquier divergencia entre un `default` de HCL y esta tabla es un
   **fallo de revisión**.

---

## 1. AWS IoT Core — Thing, Thing Type, Thing Groups, Policy

| Categoría | Nombre canónico | Plantilla / valor | Notas |
|---|---|---|---|
| **Thing name** | `cam-counter-{site_id}-{device_id}` | p. ej. `cam-counter-casa-rpi-cam` | Identidad MQTT del Pi en la flota. Único global. |
| **MQTT client-id** | **== Thing name** | `cam-counter-{site_id}-{device_id}` | El `clientId` de la conexión MQTT **es idéntico** al Thing name. Permite `${iot:Connection.Thing.IsAttached}` y el aislamiento por política basada en thing. |
| **Thing Type** | `cam-counter-edge-device` | — | UN solo thing type para la flota de Pis de borde. |
| **Thing Group (por sitio)** | `cam-counter-site-{site_id}` | p. ej. `cam-counter-site-casa` | Agrupa todos los Pis de un sitio. |
| **Thing Group (por canal OTA)** | `cam-counter-channel-{channel}` | `cam-counter-channel-canary` / `cam-counter-channel-stable` | Espejo del `release_channel` del device-registry (`canary` \| `stable`). |
| **IoT Policy (dispositivo)** | `cam-counter-device-policy` | — | UNA política de dispositivo, parametrizada con **variables de política** (`${iot:Connection.Thing.ThingName}`), NO una política por Pi. |

**Aislamiento multi-tenant**: se hace **por ThingName** vía variables de política IoT
(`${iot:Connection.Thing.ThingName}` en los recursos de `iot:Publish`/`iot:Subscribe`), **no**
por el nombre del rol IAM ni por una política distinta por dispositivo. Ver §8 (relación
thing↔rol per-Pi).

---

## 2. Fleet Provisioning (template + certs + rutas en el device)

| Categoría | Nombre canónico | Plantilla / valor | Notas |
|---|---|---|---|
| **Provisioning template** | `cam-counter-provisioning-template` | — | Plantilla de fleet provisioning (claim → cert permanente). |
| **Provisioning IoT policy (claim)** | `cam-counter-provisioning-claim-policy` | — | Política del **claim cert** (solo permite el handshake de provisioning). Distinta de la de dispositivo. |
| **Provisioning IAM role** | `cam-counter-iot-provisioning-role` | — | Rol que el servicio de provisioning asume para registrar la thing. |
| **Directorio de identidad en el device** | `/etc/cam-counter/` | — | Raíz de config + identidad en el Pi. |
| **Cert de dispositivo** | `/etc/cam-counter/certs/device.cert.pem` | — | **NUNCA** se commitea (ver §13). |
| **Llave privada** | `/etc/cam-counter/certs/device.private.key` | — | **NUNCA** se commitea. `chmod 600`. |
| **CA raíz de Amazon** | `/etc/cam-counter/certs/AmazonRootCA1.pem` | — | CA pública de AWS IoT. |
| **Claim cert (bootstrap)** | `/etc/cam-counter/certs/claim.cert.pem` (+ `.private.key`) | — | Solo para fleet provisioning; se puede rotar/retirar tras el primer arranque. |
| **Endpoint IoT (config local)** | `/etc/cam-counter/iot-endpoint` | `…-ats.iot.us-east-1.amazonaws.com` | ATS data endpoint de la cuenta. No secreto. |

> Las rutas `certs/*.pem` y `*.key` están cubiertas por `.gitignore` / `.gitleaks.toml`
> (verificación en §13). El device lee estas rutas; el repo NUNCA contiene su contenido.

---

## 3. Topics MQTT

**Patrón canónico (ÚNICO):** `cam-counter/{device_id}/{dominio}[/{subtipo}]`

- Raíz literal `cam-counter` (con `-`, **no** `cam_counter` ni `camcounter`).
- Segmento de identidad = **`{device_id}`** (NO el thing name completo, NO `{site_id}/{device_id}`).
  El `device_id` es único global y mantiene los topics cortos y estables.
- `/` es el separador jerárquico de topics MQTT — es **legal aquí** (la prohibición de `/` de
  `CLAUDE.md` §3 aplica a **slugs**, no a la jerarquía de topics ni a las keys de S3).

| Propósito | Topic canónico | Dirección | Notas |
|---|---|---|---|
| **Eventos de cruce** | `cam-counter/{device_id}/events/crossing` | device → cloud | Payload = `CrossingEvent` (contrato A). Lo enruta una IoT Rule a DynamoDB/S3. |
| **Status / lifecycle** | `cam-counter/{device_id}/status` | device → cloud | Conectado/desconectado, arranque, versión. Alimenta `last_seen_at`/`status` del registry. |
| **Telemetría** | `cam-counter/{device_id}/telemetry` | device → cloud | Métricas (fps, temp, cola de sync). Best-effort. |
| **Comandos** | `cam-counter/{device_id}/cmd` | cloud → device | Comandos al Pi (p. ej. recargar config, capturar snapshot). |

Las **shadows** usan los topics reservados `$aws/things/{thingName}/shadow/name/{shadow}/...`
(gestionados por AWS); no se renombran (ver §4).

---

## 4. Named Shadows

| Shadow | Nombre canónico | Propósito | Escritor / lector |
|---|---|---|---|
| **Config de línea** | `line-config` | Estado deseado/reportado de la **línea-umbral** (coords normalizadas 0..1) y parámetros de conteo. Reemplaza el polling de config por reconciliación de shadow. | `desired` ← cloud/UI; `reported` ← device |
| **Comando** | `command` | Comandos de larga duración con estado deseado/confirmado (p. ej. canal OTA objetivo, reinicio). | `desired` ← cloud; `reported` ← device |

> No se usa la **shadow clásica/sin nombre**; toda config va por **named shadows** para
> separar dominios. La geometría de la línea sigue el contrato `line_config.schema.json`
> (coords normalizadas; ver `CLAUDE.md` §4) — su **reconciliación versionada** es WP02, aquí
> solo se fija el **nombre** del shadow.

---

## 5. Lambdas + roles IAM de lambda + role alias

**Patrón de lambda (kebab):** `cam-counter-{dominio}-{accion}`  → **`{dominio}` primero,
`{accion}` después** (resuelve `events-ingest` vs `ingest-events`; ver §10).

| Función | Nombre canónico | Rol IAM de ejecución | Notas |
|---|---|---|---|
| **Ingesta de eventos** | `cam-counter-events-ingest` | `cam-counter-events-ingest-role` | Destino de la IoT Rule de cruces → DynamoDB `cam-counter-events`. |
| **Registro/heartbeat de device** | `cam-counter-devices-register` | `cam-counter-devices-register-role` | Upsert en `cam-counter-devices` desde status/telemetry. |
| **Promoción de config de línea** | `cam-counter-line-publish` | `cam-counter-line-publish-role` | Empuja `desired` a la shadow `line-config`. |

- **Patrón de rol IAM de lambda:** `cam-counter-{dominio}-{accion}-role`.
- **Patrón de política de lambda:** `cam-counter-{dominio}-{accion}-policy`.

| Role alias (IoT credential provider) | Nombre canónico | Notas |
|---|---|---|
| **Acceso S3 del borde vía cert** | `cam-counter-edge-s3-role-alias` | El Pi cambia su **cert X.509** por credenciales STS de corta vida para subir media a S3. Apunta al rol per-Pi (ver §8). |

---

## 6. IoT Rules (motor de reglas)

**Patrón canónico (`snake_case`):** `cam_counter_{dominio}_{accion}` — **excepción** al kebab
porque el nombre de regla de IoT **no admite `-`** (solo `[a-zA-Z0-9_]`).

| Regla | Nombre canónico | Topic origen | Acción |
|---|---|---|---|
| **Enrutado de cruces** | `cam_counter_crossing_ingest` | `cam-counter/+/events/crossing` | → Lambda `cam-counter-events-ingest`. |
| **Status → registry** | `cam_counter_status_register` | `cam-counter/+/status` | → Lambda `cam-counter-devices-register`. |

> El comodín `+` casa cualquier `{device_id}`. El nombre de la **regla** es snake; el **topic**
> y la **lambda** destino siguen siendo kebab. Esta es la única excepción de separador.

---

## 7. API Gateway, Cognito, Amplify, Docker

| Categoría | Nombre canónico | Notas |
|---|---|---|
| **API Gateway (REST/HTTP API)** | `cam-counter-api` | Backend cloud (consola de flota). Distinto de la API local FastAPI del Pi. |
| **Stage de API** | `prod` | Único stage (entorno `prod`). |
| **Cognito User Pool** | `cam-counter-user-pool` | Identidades de operadores de la consola. |
| **Cognito App Client** | `cam-counter-console-client` | Cliente SPA de la consola. |
| **Cognito Identity Pool** | `cam-counter-identity-pool` | Credenciales federadas para la consola. |
| **Amplify app (consola)** | `cam-counter-console` | Hosting de la SPA de flota. (La **UI local** del Pi NO usa Amplify.) |
| **Imagen Docker (edge)** | `cam-counter/edge` | Tag por versión SemVer (§ versionado `CLAUDE.md`). |
| **Imagen Docker (api)** | `cam-counter/api` | — |
| **Imagen Docker (sync)** | `cam-counter/sync` | Worker de sincronización (`cam-counter-sync`). |
| **Repositorio ECR** | `cam-counter/{componente}` | Mismo path que la imagen. |

> Los nombres de **contenedor** en un `docker-compose`/`.env` siguen el mismo slug:
> `cam-counter-edge`, `cam-counter-api`, `cam-counter-sync`.

---

## 8. Relación Thing ↔ rol IAM per-Pi (reconciliación explícita)

**Divergencia de prefijo, mapeo 1:1 preservado.** El Thing y el rol per-Pi describen el
**mismo Pi** `(site_id, device_id)` pero con **prefijos distintos por subsistema**:

| Subsistema | Nombre | Patrón |
|---|---|---|
| **IoT Thing** (este doc) | `cam-counter-{site_id}-{device_id}` | sin infijo |
| **Rol IAM per-Pi** (existente, `terraform/modules/iam-edge`) | `cam-counter-edge-{site_id}-{device_id}` | con infijo `-edge-` |
| **Política per-Pi** (existente) | `cam-counter-edge-{site_id}-{device_id}-policy` | con infijo `-edge-` |

- El **aislamiento IoT se hace por `ThingName`** (variables de política, §1), **NO** por el
  nombre del rol IAM. Por eso es correcto y deliberado que el Thing **no** lleve el infijo
  `-edge-` mientras el rol **sí** lo conserva.
- El **rol per-Pi existente NO se renombra** (ver §12): cambiarlo rompería el trust de F7 y
  el `iam-edge` ya aplicado. El `role-alias` `cam-counter-edge-s3-role-alias` (§5) es el
  puente: mapea el **cert** del Thing a ese **rol** per-Pi.
- **Mapa mental 1:1**: `Thing(cam-counter-{s}-{d})` ⇄ `Rol(cam-counter-edge-{s}-{d})`
  comparten la tupla `(site_id, device_id)`; el infijo `-edge-` es la **única** diferencia y
  es intencional.

---

## 9. Variables de entorno — prefijo canónico `CAMCOUNTER_*`

**`CAMCOUNTER_*` es el prefijo canónico y ÚNICO.** Es el que leen `v1/api/settings.py`,
`v1/edge/cam_counter_edge/*` y `.env.example`. **`CC_*` queda PROHIBIDO** (no existe en el
repo; introducirlo rompería `config.py`/`settings.py`). Cualquier `.env` de Docker mapea
**1:1** a estas claves (mismo nombre, sin traducción).

### 9.1 Variables existentes (leídas por el código — NO renombrar)

| Variable | Uso | Default en código |
|---|---|---|
| `CAMCOUNTER_RTSP_URL` | URL RTSP de la cámara (**secreto** → `.env`, nunca en git) | — |
| `CAMCOUNTER_SITE_ID` | slug de sitio | `demo-site` |
| `CAMCOUNTER_DEVICE_ID` | slug de dispositivo | `demo-pi` |
| `CAMCOUNTER_CAMERA_COUNT` | nº de cámaras lógicas | `2` |
| `CAMCOUNTER_DB_PATH` | ruta SQLite del borde (WAL) | `<pkg>/cam-counter.db` |
| `CAMCOUNTER_HOST` / `CAMCOUNTER_PORT` | bind de la API local | `0.0.0.0` / `8088` |
| `CAMCOUNTER_FRAME_INTERVAL` | cadencia MJPEG / fuente falsa | `0.2` |
| `CAMCOUNTER_API_TOKEN` | token OPCIONAL de escritura LAN | vacío (LAN abierta) |
| `CAMCOUNTER_HEALTHZ_HOST` / `CAMCOUNTER_HEALTHZ_PORT` | endpoint de salud | `0.0.0.0` / `8081` |
| `CAMCOUNTER_FAKE_SOURCE` | fuente determinista (E2E sin Pi) | `0` |
| `CAMCOUNTER_APP_VERSION` | override de versión (si no hay tag git) | derivado |
| `CAMCOUNTER_SYNC_ENABLED` | arranca el worker de sync | `1` |
| `CAMCOUNTER_SYNC_INTERVAL_S` | periodo de drenaje edge→cloud | `10` |
| `CAMCOUNTER_AWS_REGION` | región AWS | `us-east-1` |
| `CAMCOUNTER_EDGE_ROLE_ARN` | rol STS per-Pi a asumir (opcional) | vacío |
| `CAMCOUNTER_MEDIA_BUCKET` | override del bucket de media | `cam-counter-media-950639281773` |
| `CAMCOUNTER_EVENTS_TABLE` | override tabla de eventos | `cam-counter-events` |
| `CAMCOUNTER_DEVICES_TABLE` | override tabla de devices | `cam-counter-devices` |
| `CAMCOUNTER_CLIPS_ENABLED` | grabar/subir clips por evento | `1` |
| `CAMCOUNTER_CLIP_GRACE_S` / `CAMCOUNTER_CLIP_PRE_S` / `CAMCOUNTER_CLIP_POST_S` | ventana del clip | — |
| `CAMCOUNTER_CLIP_DIR` / `_FPS` / `_WIDTH` / `_HEIGHT` / `_QUALITY` | parámetros de clip | — |
| `CAMCOUNTER_AWS_INTEGRATION` / `_ALLOW_ENV_CREDS` | tests de integración AWS | `0` |
| `CAMCOUNTER_SELFTEST_SITE_ID` / `_DEVICE_ID` | self-test de integración | — |

### 9.2 Variable NUEVA de este WP

| Variable | Valores | Default | Semántica |
|---|---|---|---|
| **`CAMCOUNTER_SYNC_TRANSPORT`** | `direct` \| `iot` | **`direct`** | Selector de transporte de sincronización edge→cloud. `direct` = el camino ACTUAL (boto3 directo a DynamoDB/S3 vía rol STS per-Pi, comportamiento sin cambios). `iot` = publicar por **MQTT a IoT Core** (topics §3). Por defecto `direct` para **no romper** el stack en marcha; `iot` se habilita en WPs posteriores. |

> **Regla:** toda nueva clave de runtime usa el prefijo `CAMCOUNTER_`. NUNCA `CC_`. Un `.env`
> de Docker que defina, p. ej., `CAMCOUNTER_SYNC_TRANSPORT=iot` mapea 1:1 a la lectura del
> código (sin alias, sin traducción).

---

## 10. Divergencias resueltas (EXHAUSTIVA)

Cada fila fija **una** opción canónica con su justificación. Las "variantes rechazadas" son
las que aparecían en borradores/specs y **quedan prohibidas**.

| # | Tema | Variantes rechazadas | **Canon** | Justificación |
|---|---|---|---|---|
| 1 | **Separador de nombres** | `cam__counter`, `camCounter`, `cam_counter-*` | **`cam-counter-…`** (kebab, guion simple) | Consistencia con el prefijo de producto ya usado en S3/DynamoDB/IAM existentes. `__` y camelCase no aparecen en ningún recurso real. |
| 2 | **Prefijo de entorno** | `CC_*` | **`CAMCOUNTER_*`** | Es el que LEE el código (`settings.py`, edge, `.env.example`). `CC_*` nunca existió; introducirlo rompería la config. |
| 3 | **Thing Type** | `cam-counter-thing-type`, `camCounterDevice`, `cam-counter-pi` | **`cam-counter-edge-device`** | Describe el rol (dispositivo de borde) sin acoplarse al hardware concreto (Pi5/Hailo). |
| 4a | **Topic — raíz** | `cam_counter/…`, `camcounter/…` | **`cam-counter/…`** | Misma raíz kebab que el resto del producto. |
| 4b | **Topic — segmento de identidad** | `…/{site_id}/{device_id}/…`, `…/{thing_name}/…` | **`…/{device_id}/…`** | `device_id` ya es único global; topics más cortos y estables; no duplica `site_id`. |
| 4c | **Topic — orden dominio/acción** | `cam-counter/{device_id}/crossing/events`, `…/event/cross` | **`cam-counter/{device_id}/events/crossing`** | Dominio (`events`) antes que subtipo (`crossing`); paralelo a `status`/`telemetry`/`cmd`. |
| 4d | **Topic — set completo** | mezcla de `cmd`/`commands`, `state`/`status`, `metrics`/`telemetry` | **`events/crossing`, `status`, `telemetry`, `cmd`** | Cuatro topics fijos; sin sinónimos. |
| 5 | **IoT Policy de dispositivo** | `cam-counter-iot-policy`, `cam-counter-edge-policy`, una política por Pi | **`cam-counter-device-policy`** (única, con variables de política) | Una sola política parametrizada por `${iot:Connection.Thing.ThingName}` escala a toda la flota; evita N políticas. |
| 6 | **Role alias** | `cam-counter-s3-role-alias`, `cam-counter-edge-role-alias`, `cam-counter-role-alias` | **`cam-counter-edge-s3-role-alias`** | Nombra el sujeto (`edge`) y el recurso (`s3`); coincide con el dominio del rol per-Pi `cam-counter-edge-*`. |
| 7 | **Nombre de lambda (orden)** | `cam-counter-ingest-events`, `cam-counter-event-ingest` | **`cam-counter-events-ingest`** | Patrón fijo `{dominio}-{accion}`: dominio (`events`) primero, acción (`ingest`) después. Resuelve `events-ingest` vs `ingest-events`. |
| 8 | **Nombre de IoT Rule (separador)** | `cam-counter-crossing-ingest` (kebab) | **`cam_counter_crossing_ingest`** (snake) | El motor de IoT Rules **no admite `-`**; es la única excepción de separador, prefijo `cam_counter_`. |
| 9 | **Thing name vs rol per-Pi** | renombrar el rol a `cam-counter-{s}-{d}`; renombrar el thing a `cam-counter-edge-{s}-{d}` | **Thing `cam-counter-{s}-{d}`** + **Rol `cam-counter-edge-{s}-{d}`** (sin tocar el rol) | El aislamiento es por ThingName, no por nombre de rol; el rol existente no se renombra (§8, §12). |
| 10 | **Named shadows** | `lineConfig`/`line_config`, `cmd`/`commands` como shadow | **`line-config`** y **`command`** | Kebab; nombres cortos y de dominio único. |
| 11 | **Directorio en el device** | `/opt/cam-counter/`, `~/.cam-counter/` | **`/etc/cam-counter/`** | Config + identidad de sistema; estándar FHS para config de servicio. |

---

## 11. Gate de coherencia HCL ↔ doc

Cuando un WP posterior escriba Terraform, el **valor por defecto** de cada variable de
nomenclatura **debe igualar** el canon de este documento. Ejemplos de pares a verificar:

| Variable Terraform (futura) | `default` esperado (== canon) |
|---|---|
| `thing_type_name` | `cam-counter-edge-device` |
| `device_policy_name` | `cam-counter-device-policy` |
| `role_alias_name` | `cam-counter-edge-s3-role-alias` |
| `provisioning_template_name` | `cam-counter-provisioning-template` |
| `events_ingest_lambda_name` | `cam-counter-events-ingest` |
| `crossing_rule_name` | `cam_counter_crossing_ingest` |
| `name_prefix` (iam-edge, **existente**) | `cam-counter-edge` |

> La verificación es **textual**: el `default` del HCL se compara carácter a carácter con la
> celda "Canon" correspondiente. Una discrepancia es un fallo de revisión del WP que
> introduzca ese HCL (este WP no introduce HCL).

---

## 12. Recursos EXISTENTES que NO se renombran

Estos recursos ya están aplicados (Terraform) o leídos por el código. **Renombrarlos rompería
el state, el trust IAM o la config en marcha. Se mantienen tal cual.**

| Recurso | Nombre | Fuente |
|---|---|---|
| Bucket media | `cam-counter-media-950639281773` | `media-bucket`, `sync.py` |
| Bucket releases OTA | `cam-counter-fleet-releases-950639281773` | `fleet-releases`, `iam-edge` |
| Bucket artifacts (RESERVADO, no tocar) | `cam-counter-rpi-artifacts-950639281773` | `CLAUDE.md` §7 |
| Bucket tfstate | `cam-counter-tfstate-950639281773` | `state-backend` |
| Tabla lock de state | `cam-counter-tfstate-lock` | `state-backend` |
| Tabla de eventos | `cam-counter-events` | `events-table`, `sync.py` |
| Tabla de devices | `cam-counter-devices` | `device-registry`, `sync.py` |
| Rol per-Pi | `cam-counter-edge-{site_id}-{device_id}` | `iam-edge` |
| Política per-Pi | `cam-counter-edge-{site_id}-{device_id}-policy` | `iam-edge` |
| Rol OIDC plan (read-only) | `cam-counter-gha-plan` | `iam-github-oidc` |
| Rol OIDC deploy (gated) | `cam-counter-gha-deploy` | `iam-github-oidc` |
| Worker de sync (proceso) | `cam-counter-sync` | `sync_runner.py` |
| Prefijo de entorno | `CAMCOUNTER_*` | `settings.py`, edge, `.env.example` |
| IAM admin del runner (FUERA DE ALCANCE — no tocar) | `raspberry` | guardarraíl MAD |

> Los **nombres IoT nuevos** de este documento se eligieron para **no colisionar** con
> ninguno de los anteriores: ningún thing/policy/rule/lambda/role-alias nuevo reusa un nombre
> existente, y todos respetan el prefijo `cam-counter-` y los slugs `^[a-z0-9][a-z0-9-]{1,62}$`.

---

## 13. Claves S3 y tags (heredados, recordatorio)

- **Claves de media** (sin cambios respecto a `CLAUDE.md` §7):
  `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}`.
- **Manifiestos de canal**: `channels/{channel}/manifest.json` (bucket de releases).
- **Tags** (sin cambios respecto a `CLAUDE.md` §5/§6 — F3):
  - `default_tags` **capitalizados**: `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }`.
  - **MÁS** tags lógicos en **minúscula** en todos los recursos: `project = "cam-counter"`,
    `managed_by = "mad-runner"`.
  - La clave capitalizada `ManagedBy` **siempre** vale `terraform`; la verificación de
    `managed_by=mad-runner` busca la clave **minúscula**.

---

## 14. Cero secretos (recordatorio)

- **NUNCA** se commitea un cert, llave privada, claim cert ni `.env` real. Los certs IoT viven
  en `/etc/cam-counter/certs/` **en el device** (§2), nunca en el repo.
- Las credenciales de cámara (`CAMCOUNTER_RTSP_URL`) van por `.env` / SSM, fuera de git.
- `gitleaks` (`.gitleaks.toml`) y `.gitignore` cubren `*.pem`, `*.key`, `.env`.

---

## Apéndice — validación de slugs

Regex canónico (idéntico al de `CLAUDE.md` §3 y a las `validation` de los módulos Terraform):

```
^[a-z0-9][a-z0-9-]{1,62}$
```

Se aplica a **cada** `site_id`, `device_id`, `camera_id` y `channel` ANTES de componer
cualquier nombre de recurso, key de S3, key de DynamoDB o topic MQTT. Los nombres compuestos
(thing, rol, policy) pueden exceder 63 caracteres; el regex valida los **slugs componentes**,
no el resultado compuesto.
