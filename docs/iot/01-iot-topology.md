# Topologia IoT Core

cam-counter — AWS IoT Core para una flota de Raspberry Pi 5 contadoras. Cuenta `950639281773`, region `us-east-1`, endpoint ATS `a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com`.

Dispositivo de ejemplo a lo largo del documento:

- `site_id = sitio-demo`
- `device_id = rpi-001`
- `camera_id = rpi-001-cam0`
- `release_channel = stable`

> Todos los slugs cumplen `^[a-z0-9][a-z0-9-]{1,62}$`. En MQTT el `/` es delimitador de niveles de topic (no es slug-prohibido, los slugs siguen sin contener `/` ni `#`).

---

## 1. Estandar de nombres (recursos IoT)

| Recurso | Patron | Ejemplo |
|---|---|---|
| Thing Type | `cam-counter-rpi` | `cam-counter-rpi` |
| Thing (1 por device) | `cam-counter-{site_id}-{device_id}` | `cam-counter-sitio-demo-rpi-001` |
| Thing Group por sitio | `cam-counter-site-{site_id}` | `cam-counter-site-sitio-demo` |
| Thing Group por canal | `cam-counter-channel-{release_channel}` | `cam-counter-channel-stable` |
| Thing Group raiz (flota) | `cam-counter-fleet` | `cam-counter-fleet` |
| IoT Policy (1 plantilla, N attach) | `cam-counter-device-policy` | `cam-counter-device-policy` |
| Certificado (logico, por device) | `cam-counter-cert-{site_id}-{device_id}` | tag `Name=cam-counter-cert-sitio-demo-rpi-001` |
| IoT Rule eventos | `cam_counter_crossing_events` | (las Rules usan `[a-zA-Z0-9_]`, NO guiones) |
| IoT Rule status/heartbeat | `cam_counter_device_status` | |
| IoT Rule LWT offline | `cam_counter_device_lwt` | |
| Lambda eventos | `cam-counter-ingest-events` | |
| Lambda status/shadow-sink | `cam-counter-device-status` | |
| Named Shadow line-config | `line-config` | `$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/line-config` |
| Named Shadow comandos | `command` | `$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/command` |
| Provisioning template | `cam-counter-fleet-provisioning` | |

> Restriccion de naming de IoT Rules: el nombre solo admite `[a-zA-Z0-9_]`. Por eso las Rules usan snake_case (`cam_counter_*`), a diferencia del resto de recursos que usan el prefijo kebab `cam-counter-`.

---

## 2. Thing Type y atributos

Un unico Thing Type `cam-counter-rpi`. Atributos searchable (no mutan a menudo, max 3 atributos en Thing Type — el resto va a Thing attributes / shadow):

```jsonc
// Thing Type cam-counter-rpi (immutable searchable keys)
{
  "thingTypeName": "cam-counter-rpi",
  "thingTypeProperties": {
    "thingTypeDescription": "Raspberry Pi 5 + Hailo-8 person counter edge node",
    "searchableAttributes": ["site_id", "device_id", "release_channel"]
  }
}
```

Atributos por Thing (`thing.attributes`, hasta 50, usados por policy variables y fleet indexing):

```jsonc
{
  "site_id":        "sitio-demo",
  "device_id":      "rpi-001",
  "camera_id":      "rpi-001-cam0",   // camara principal; multi-cam -> ver nota
  "release_channel":"stable",
  "hw":             "rpi5-hailo8",
  "edge_role_arn":  "arn:aws:iam::950639281773:role/cam-counter-edge-sitio-demo-rpi-001"
}
```

- `device_id` es UNICO en la flota y es la clave de naming. `thingName = cam-counter-{site_id}-{device_id}` para legibilidad en consola, pero el `device_id` solo es lo que viaja en topics y DynamoDB (`PK=DEVICE#{device_id}`).
- Multi-camara: un device puede exponer varias camaras. `camera_id` NO va en el thing name ni en la policy; viaja en el payload/topic de evento. El atributo `camera_id` del thing es solo la camara por defecto.

---

## 3. Things, Thing Groups y membership

Jerarquia de grupos (un Thing puede estar en varios grupos a la vez; usamos grupos planos por dimension, no anidados, para que el indexado y las dynamic policies sean simples):

```
cam-counter-fleet                      (raiz, atributos/политика comunes futuras)
├── cam-counter-site-sitio-demo        (membership por sitio)
├── cam-counter-site-sitio-norte
├── cam-counter-channel-stable         (membership por canal de release)
├── cam-counter-channel-beta
└── cam-counter-channel-canary
```

El Thing `cam-counter-sitio-demo-rpi-001` pertenece a: `cam-counter-fleet`, `cam-counter-site-sitio-demo`, `cam-counter-channel-stable`.

- Grupos por **sitio**: agrupan operacion/observabilidad por ubicacion fisica; util para Fleet Indexing queries (`thingGroupNames:cam-counter-site-sitio-demo`).
- Grupos por **canal de release**: alinean con el OTA pull-based existente (`channels/<channel>/manifest.json`) y con el GSI1 de `cam-counter-devices` (`CHANNEL#{release_channel}`). Mover un device de `beta` a `stable` = cambio de membership + cambio de atributo `release_channel` + cambio de GSI1 en DynamoDB.

> La IoT Policy se attachea al **certificado**, no al grupo (ver seccion 4) — minimo privilegio por-device. Los grupos NO llevan policy en el diseno base; quedan reservados para futuras static group policies de solo lectura comunes.

Terraform: nuevo modulo `terraform/modules/iot-core` (thing type, groups, policy, rules) + extension de `device-registry` para el registro del thing por device. Estado aditivo/monotono.

---

## 4. IoT Policy por-certificado (minimo privilegio con policy variables)

**Una sola plantilla** `cam-counter-device-policy` attacheada a cada certificado. Usa policy variables para que el mismo documento conceda a cada device acceso SOLO a sus propios recursos:

- `${iot:Connect.Thing.ThingName}` — clientId forzado = thingName.
- `${iot:Connect.Thing.Attributes[device_id]}` — el `device_id` del thing, validado contra el atributo del registry.

El clientId del device DEBE ser su `thingName` (`cam-counter-sitio-demo-rpi-001`), y el thing va con `thingName == clientId` para que las variables resuelvan.

```jsonc
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ConnectAsOwnThingOnly",
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:us-east-1:950639281773:client/${iot:Connect.Thing.ThingName}",
      "Condition": {
        "Bool": { "iot:Connect.Thing.IsAttached": "true" }
      }
    },

    {
      "Sid": "PublishOwnDataTopicsOnly",
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/events/crossing",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/telemetry/heartbeat",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/status/connection",
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/cmd/ack"
      ]
    },

    {
      "Sid": "SubscribeOwnCmdAndShadowOnly",
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topicfilter/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/cmd/request",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/update/delta",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/get/accepted",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/update/delta",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/get/accepted"
      ]
    },

    {
      "Sid": "ReceiveOwnCmdAndShadowOnly",
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connect.Thing.Attributes[device_id]}/cmd/request",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/update/delta",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/get/accepted",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/update/delta",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/get/accepted"
      ]
    },

    {
      "Sid": "PublishOwnShadowUpdatesAndGets",
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/update",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/line-config/get",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/update",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/command/get"
      ]
    },
    {
      "Sid": "SubscribeOwnShadowAck",
      "Effect": "Allow",
      "Action": ["iot:Subscribe", "iot:Receive"],
      "Resource": [
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/*/update/accepted",
        "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/*/update/rejected",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/*/update/accepted",
        "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connect.Thing.ThingName}/shadow/name/*/update/rejected"
      ]
    }
  ]
}
```

Garantias de minimo privilegio:

- **No puede publicar** en el topic de otro device: el ARN incrusta `${iot:Connect.Thing.Attributes[device_id]}`, que IoT resuelve desde el registry del thing attacheado a SU certificado. Falsificar el `device_id` en el topic = denegado.
- **No puede suscribir** a cmd/shadow de otro device: los shadows usan `${iot:Connect.Thing.ThingName}` (igual al clientId, forzado en `iot:Connect`).
- **No puede conectarse** con un clientId que no sea su thingName, ni con un cert no attacheado (`IsAttached: true`).
- Las llaves estaticas del IAM user `raspberry` se ELIMINAN: el device ya no necesita credenciales IAM para escribir a DynamoDB/S3 (lo hace la Lambda). La subida de clips a S3 se resuelve con un `iot:AssumeRoleWithCertificate` opcional o, mas simple en la migracion, con presigned URLs (ver seccion 9 — la Lambda ingest no firma, pero el flujo de subida puede emitir presigned PUT). En este diseno base el device sube el clip via **Credentials Provider de IoT** asumiendo `cam-counter-edge-{site}-{device}` (rol por device ya existente), sin llaves estaticas.

---

## 5. Esquema de topics jerarquico

Namespace de aplicacion: `cam-counter/{device_id}/...`. Los shadows usan el namespace reservado `$aws/things/{thingName}/shadow/...`.

| Proposito | Direccion | QoS | Topic | Retained / LWT |
|---|---|---|---|---|
| Evento de cruce | device -> cloud | 1 | `cam-counter/{device_id}/events/crossing` | no |
| Telemetria / heartbeat | device -> cloud | 0 | `cam-counter/{device_id}/telemetry/heartbeat` | no |
| Estado conexion (online) | device -> cloud | 1 | `cam-counter/{device_id}/status/connection` | retained |
| Estado conexion (offline, LWT) | broker -> cloud | 1 | `cam-counter/{device_id}/status/connection` | retained, LWT |
| Comando cloud -> device | cloud -> device | 1 | `cam-counter/{device_id}/cmd/request` | no |
| Ack de comando | device -> cloud | 1 | `cam-counter/{device_id}/cmd/ack` | no |

Topics concretos del device de ejemplo (`device_id=rpi-001`, `thingName=cam-counter-sitio-demo-rpi-001`):

```
cam-counter/rpi-001/events/crossing
cam-counter/rpi-001/telemetry/heartbeat
cam-counter/rpi-001/status/connection
cam-counter/rpi-001/cmd/request
cam-counter/rpi-001/cmd/ack

$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/line-config/get
$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/line-config/update
$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/line-config/update/delta
$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/command/update
$aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/command/update/delta
```

### Last Will and Testament (LWT)

El device configura el LWT en el CONNECT:

```jsonc
{
  "will": {
    "topic": "cam-counter/rpi-001/status/connection",
    "qos": 1,
    "retain": true,
    "payload": {
      "device_id": "rpi-001",
      "site_id": "sitio-demo",
      "status": "offline",
      "reason": "lwt",
      "ts_ms": 0          // el broker publica el will tal cual; ts real lo pone la Rule via timestamp()
    }
  }
}
```

Al conectar (online) el device publica el MISMO topic retained con `status:"online"` y un `ts_ms` real, lo que sobreescribe el will retenido. Si el socket cae sin DISCONNECT limpio, el broker publica el will -> `status:"offline"` -> la Rule LWT marca el device offline en DynamoDB.

---

## 6. Payloads MQTT

### 6.1 CrossingEvent -> payload (sin binarios; el clip va a S3 aparte)

El `CrossingEvent` del SQLite local se mapea a JSON MQTT validado contra `contracts/crossing_event.schema.json`. El MP4 **no** viaja por MQTT: el `ClipRecorder` lo sube a S3 con la clave determinista y el payload solo lleva el `clip_key` (puntero):

```jsonc
// publish -> cam-counter/rpi-001/events/crossing  (QoS1)
{
  "schema_version": 1,
  "event_id": "9f2c1a...e7",          // sha1 determinista de la identidad -> idempotencia
  "site_id": "sitio-demo",
  "device_id": "rpi-001",
  "camera_id": "rpi-001-cam0",
  "ts_event_ms": 1718900000123,        // instante del cruce (epoch ms)
  "direction": "in",                   // in | out
  "count_delta": 1,
  "line_config_version": 42,           // version de linea-umbral vigente al contar
  "confidence": 0.94,
  "clip_key": "media/sitio-demo/rpi-001/rpi-001-cam0/2026/06/22/9f2c1a...e7.mp4",
  "clip_status": "pending"             // pending | uploaded ; el clip sube async a S3
}
```

Mapeo de campos:

| CrossingEvent (SQLite) | Payload MQTT | DynamoDB `cam-counter-events` |
|---|---|---|
| `event_id` (sha1) | `event_id` | parte de `SK=TS#{ts_event_ms:013d}#{event_id}` + condition key |
| `site_id`,`device_id`,`camera_id` | idem | `PK=CAM#{site}#{device}#{camera}` |
| `ts_event_ms` | `ts_event_ms` | `SK=TS#{ts_event_ms:013d}#...` |
| `direction`,`count_delta`,`confidence` | idem | atributos |
| `config_version` | `line_config_version` | atributo |
| clave S3 del clip | `clip_key` | `clip_key` (puntero al objeto en `cam-counter-media-950639281773`) |

El clip y el evento van por **caminos separados**: el clip por HTTPS a S3 (binario), el evento por MQTT (metadata). Se reconcilian por `clip_key` deterministico (mismo `event_id`). Orden tolerante: si el evento llega antes que el clip, `clip_status:"pending"`; cuando el clip termina de subir, un segundo publish (o la lectura de S3 por la UI) actualiza el estado. La presigned URL para reproducir se genera bajo demanda (Lambda/Amplify) a partir de `clip_key`.

### 6.2 Heartbeat / telemetria

```jsonc
// publish -> cam-counter/rpi-001/telemetry/heartbeat  (QoS0, cada ~30s)
{
  "device_id": "rpi-001",
  "site_id": "sitio-demo",
  "ts_ms": 1718900030000,
  "fw_version": "2026.06.0",
  "release_channel": "stable",
  "line_config_version": 42,
  "queue_depth": 3,           // CrossingEvents pendientes de drenar
  "rtsp_ok": true,
  "hailo_ok": true,
  "cpu_temp_c": 61.2,
  "uptime_s": 84211
}
```

### 6.3 Status connection (online)

```jsonc
// publish -> cam-counter/rpi-001/status/connection  (QoS1, retained)
{ "device_id":"rpi-001","site_id":"sitio-demo","status":"online","ts_ms":1718900000000,"fw_version":"2026.06.0" }
```

### 6.4 Comando cloud -> device y ack

```jsonc
// cloud publish -> cam-counter/rpi-001/cmd/request  (QoS1)
{
  "command_id": "c-7b3e...",          // idempotencia del ack
  "type": "snapshot",                  // snapshot | restart | reload-config | set-channel
  "args": { "camera_id": "rpi-001-cam0" },
  "issued_by": "tools.makesens@gmail.com",
  "ts_ms": 1718900100000
}

// device publish -> cam-counter/rpi-001/cmd/ack  (QoS1)
{
  "command_id": "c-7b3e...",
  "type": "snapshot",
  "status": "done",                    // accepted | done | failed
  "detail": "snapshot uploaded media/sitio-demo/rpi-001/rpi-001-cam0/2026/06/22/snap-...jpg",
  "ts_ms": 1718900102000
}
```

> Comandos "fire-and-forget" (snapshot, restart) van por `cmd/request` + `cmd/ack`. Comandos que son **estado deseado persistente** (linea-umbral, canal) van por **Device Shadow** (seccion 7), no por `cmd/*`, para que sobrevivan a reconexiones.

---

## 7. Device Shadows (Digital Twin)

Dos **named shadows** por thing (no se usa el classic shadow):

### 7.1 Shadow `line-config` (linea-umbral, estado deseado persistente)

Reconciliacion `desired` (nube) vs `reported` (edge). Validado contra `contracts/line_config.schema.json`.

```jsonc
// $aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/line-config
{
  "state": {
    "desired": {
      "version": 43,
      "line": { "x1": 0.10, "y1": 0.55, "x2": 0.90, "y2": 0.55 },
      "direction_positive": "in",
      "min_confidence": 0.85
    },
    "reported": {
      "version": 42,
      "line": { "x1": 0.12, "y1": 0.52, "x2": 0.88, "y2": 0.52 },
      "direction_positive": "in",
      "min_confidence": 0.85,
      "applied_ts_ms": 1718899000000
    }
  }
}
```

Reconciliacion en el edge:

1. La UI de la flota (Amplify) o la UI local escriben `desired` (la local tambien escribe el SQLite). Quien publica `desired` en la nube hace `shadow/name/line-config/update` con solo `{state:{desired:{...}}}`.
2. IoT calcula el **delta** y lo publica en `.../line-config/update/delta`. El edge esta suscrito.
3. El edge recibe el delta, valida contra `line_config.schema.json`, escribe la nueva linea en el SQLite (incrementa `config_version`), y el `ConfigWatcher` existente la recarga **en caliente** (mecanismo ya implementado por `config_version`).
4. El edge publica `reported` con `version` aplicada -> delta se cierra. Conflicto local-vs-nube: gana el de `version` mayor (monotono); el edge nunca baja de version.
5. Al arrancar, el edge hace `shadow/name/line-config/get` para sincronizar el estado deseado offline-acumulado.

Esto unifica los dos origenes de la linea (UI local + nube): el SQLite sigue siendo la fuente que lee `edge`, y el shadow es el canal de propagacion nube->SQLite. La UI local sigue editando directo el SQLite y reporta a `reported` para que la nube vea el estado real.

### 7.2 Shadow `command` (comandos persistentes / config de operacion)

Para deseos persistentes que deben sobrevivir reconexion (p.ej. cambiar `release_channel`, habilitar/inhabilitar conteo, fps target):

```jsonc
// $aws/things/cam-counter-sitio-demo-rpi-001/shadow/name/command
{
  "state": {
    "desired":  { "counting_enabled": true, "release_channel": "stable", "target_fps": 15 },
    "reported": { "counting_enabled": true, "release_channel": "stable", "target_fps": 15, "applied_ts_ms": 1718899500000 }
  }
}
```

Mismo loop desired/delta/reported. El cambio de `release_channel` aqui se coordina con: membership del Thing Group `cam-counter-channel-*`, atributo `release_channel` del thing, GSI1 de `cam-counter-devices`, y el OTA pull-based (`channels/<channel>/manifest.json`).

---

## 8. IoT Rules

Tres rules (SQL de IoT). Todas con error action a CloudWatch Logs + re-publish a un topic DLQ `cam-counter/$dlq/{rulename}`.

### 8.1 `cam_counter_crossing_events` — eventos -> Lambda ingest

```sql
SELECT *,
       topic(2)            AS device_id_topic,   -- validar == device_id del payload
       clientid()          AS client_id,
       timestamp()         AS ingest_ts_ms
FROM 'cam-counter/+/events/crossing'
```

- Action: invocar Lambda `cam-counter-ingest-events`.
- La Lambda: valida `crossing_event.schema.json`, comprueba `topic(2) == payload.device_id` y `client_id == cam-counter-{site}-{device}` (anti-spoof defensa en profundidad), y hace **conditional put** en `cam-counter-events` con `attribute_not_exists(PK) AND attribute_not_exists(SK)` -> idempotente (reintento del mismo `event_id` no duplica). Construye `PK=CAM#{site}#{device}#{camera}`, `SK=TS#{ts_event_ms:013d}#{event_id}`, copia `clip_key`. No toca el binario.

### 8.2 `cam_counter_device_status` — status/heartbeat -> cam-counter-devices

```sql
SELECT *,
       topic(2)    AS device_id,
       timestamp() AS server_ts_ms
FROM 'cam-counter/+/status/connection'
```

(+ una segunda rule o el mismo basic-ingest para `cam-counter/+/telemetry/heartbeat`.)

- Action: invocar Lambda `cam-counter-device-status` (o `dynamoDBv2` directo si el mapeo es trivial).
- Efecto: `UpdateItem` en `cam-counter-devices` (`PK=DEVICE#{device_id}`): set `connection_status`, `last_seen_ms`, `fw_version`, `release_channel`, `line_config_version`, salud (`rtsp_ok`,`hailo_ok`,`cpu_temp_c`), y `GSI1PK=CHANNEL#{release_channel}`. `online`/`offline` viene del campo `status` del payload retained.

### 8.3 `cam_counter_device_lwt` — Last Will -> marcar offline

El will llega al MISMO topic `status/connection` con `status:"offline"`, asi que **la rule 8.2 ya lo captura** (no necesita rule aparte: el will es un publish normal del broker). Para trazabilidad/alarma de caidas inesperadas se añade una rule filtrada:

```sql
SELECT topic(2) AS device_id, timestamp() AS offline_ts_ms, reason
FROM 'cam-counter/+/status/connection'
WHERE status = 'offline' AND reason = 'lwt'
```

- Action: `UpdateItem` en `cam-counter-devices` set `connection_status='offline'`, `offline_reason='lwt'`, `last_seen_ms`; opcional SNS/alarma. Como el will es retained, un consumidor nuevo (Amplify) que se suscriba ve el ultimo estado conocido.

> Alternativa nativa: IoT **Lifecycle Events** (`$aws/events/presence/disconnected/{clientId}`) como respaldo del LWT para deteccion de caida garantizada por el broker; se enruta a la misma Lambda de status. El LWT da el payload de aplicacion; el lifecycle event da la garantia del broker.

---

## 9. Flujo de clips (binarios fuera de MQTT)

1. `ClipRecorder` arma el MP4 (pre+post roll) y lo encola con la clave determinista `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.mp4`.
2. El device sube el MP4 a `cam-counter-media-950639281773` por **HTTPS** usando credenciales temporales del **IoT Credentials Provider** (role alias -> `cam-counter-edge-sitio-demo-rpi-001`), NO con las llaves estaticas del user `raspberry` (que se eliminan). El rol solo permite `s3:PutObject` bajo el prefijo `media/{site_id}/{device_id}/*`.
3. El evento MQTT lleva `clip_key` (puntero) + `clip_status`. La Lambda ingest persiste el puntero en DynamoDB.
4. La UI de la flota (Amplify, seccion siguiente del proyecto) genera una **presigned GET URL** a partir de `clip_key` para reproducir, sin exponer el bucket.

Asi el binario nunca atraviesa IoT Core (limite 128 KB/mensaje MQTT) y el control-plane (evento/metadata) queda desacoplado del data-plane (clip).

---

## 10. Provisioning sencillo y repetible

- **Fleet Provisioning by Claiming Certificate** con template `cam-counter-fleet-provisioning`. Un claim cert compartido (de baja confianza, solo permite el flujo de provisioning) viene en la imagen Docker base. En el primer arranque el device llama `RegisterThing` con parametros `{site_id, device_id, camera_id, release_channel}` (tomados del `.env` del contenedor); el template crea: el Thing `cam-counter-{site}-{device}`, sus atributos, lo agrega a los Thing Groups `cam-counter-site-{site}` y `cam-counter-channel-{channel}`, genera un cert unico por-device, y attachea `cam-counter-device-policy`. El claim cert se descarta tras el primer boot.
- **Pre-provisioning hook** (Lambda) valida que `device_id` no este ya registrado y que los slugs cumplan `^[a-z0-9][a-z0-9-]{1,62}$` antes de aceptar.
- Alternativa manual para 1-2 devices (bootstrap inicial / migracion): `aws iot create-keys-and-certificate` + `create-thing` + `add-thing-to-thing-group` + `attach-policy` via un script Terraform/CLI, dejando cert+key en `/etc/cam-counter/certs/`.

Variables de entorno del contenedor Docker (las consume el edge y el sync):

```bash
CAM_COUNTER_SITE_ID=sitio-demo
CAM_COUNTER_DEVICE_ID=rpi-001
CAM_COUNTER_CAMERA_ID=rpi-001-cam0
CAM_COUNTER_RELEASE_CHANNEL=stable
CAM_COUNTER_IOT_ENDPOINT=a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com
CAM_COUNTER_IOT_CLIENT_ID=cam-counter-sitio-demo-rpi-001   # == thingName, forzado por policy
CAM_COUNTER_THING_NAME=cam-counter-sitio-demo-rpi-001
CAM_COUNTER_CERT_PATH=/etc/cam-counter/certs/device.cert.pem
CAM_COUNTER_KEY_PATH=/etc/cam-counter/certs/device.private.key
CAM_COUNTER_ROOT_CA_PATH=/etc/cam-counter/certs/AmazonRootCA1.pem
CAM_COUNTER_ROLE_ALIAS=cam-counter-edge-role-alias          # IoT Credentials Provider -> S3
CAM_COUNTER_MEDIA_BUCKET=cam-counter-media-950639281773
```

(Hailo en Docker: `--device /dev/hailo0`, HailoRT + cv2 en la imagen, base ARM64 con paginas 16 KB. El cliente MQTT corre en el mismo contenedor edge o en un sidecar `sync` que ya no usa llaves estaticas.)

---

## 11. Migracion segura (no big-bang)

1. **Fase 0**: crear thing type, groups, policy, cert del device de ejemplo, rules, Lambdas (Terraform aditivo). El device sigue escribiendo DIRECTO a DynamoDB con la llave estatica (sin cambios funcionales todavia).
2. **Fase 1**: el `sync_runner` empieza a **publicar tambien** por MQTT (dual-write) ademas del put directo. La Lambda ingest hace conditional put idempotente -> aunque ambos caminos escriban, no se duplica (mismo `event_id`).
3. **Fase 2**: validar paridad (eventos por MQTT == eventos directos). Apagar el put directo del device; queda solo MQTT.
4. **Fase 3**: eliminar las llaves estaticas del IAM user `raspberry`, migrar la subida de clips al IoT Credentials Provider, borrar `~/.aws/credentials`. Validar shadows (linea-umbral nube->edge) y comandos.

Cada fase es un PR apilado sobre el anterior (PR00 sobre main, merge no-squash). MAD aplica `terraform apply -auto-approve`; estado aditivo/monotono, se aborta ante destroy/replace.

---

## 12. Resumen de recursos a crear (Terraform, modulo `iot-core`)

- 1 Thing Type `cam-counter-rpi`.
- N Things `cam-counter-{site}-{device}` (1 por device; el de ejemplo `cam-counter-sitio-demo-rpi-001`).
- Thing Groups: `cam-counter-fleet`, `cam-counter-site-*`, `cam-counter-channel-*`.
- 1 IoT Policy plantilla `cam-counter-device-policy` (policy variables, attach por cert).
- Certs X.509 por device (provisioning) + role alias `cam-counter-edge-role-alias` para S3.
- 2 named shadows por thing: `line-config`, `command`.
- 3 IoT Rules: `cam_counter_crossing_events`, `cam_counter_device_status`, `cam_counter_device_lwt`.
- 2 Lambdas: `cam-counter-ingest-events`, `cam-counter-device-status`.
- 1 provisioning template `cam-counter-fleet-provisioning` + pre-provisioning hook Lambda.