# Dispositivo: refactor MQTT + Docker

Refactor del nodo de borde (Raspberry Pi 5 + Hailo-8) de **escritura directa a DynamoDB con llave IAM estatica** hacia **publicacion MQTT por mTLS a AWS IoT Core**, mas su empaquetado en Docker. Diseno alineado con los SPEC de naming, topologia IoT, provisioning y seguridad. Edge-first, aditivo/monotono, migracion por fases. Convencion canonica de nombres usada aqui: thing/topics segun el SPEC de seguridad y `cert-provisioning` (separador `__` para `DEVICE_FULL`, topics `cam-counter/evt|cmd/...`); cuando dos SPEC divergen en un detalle de slug, se documenta y se centraliza en `.env` para no acoplar el codigo a una convencion.

---

## 1. Reemplazo de `sync_runner` por publicador MQTT

### 1.1 Arquitectura del nuevo proceso `mqtt_publisher` (reemplaza `sync_runner`)

El `sync_runner` actual (boto3, escribe directo a DynamoDB + sube clips a S3 con la llave estatica `raspberry`) se reescribe como **`mqtt_publisher`**: un proceso de larga vida con un cliente `paho-mqtt` persistente sobre mTLS. Mantiene el principio edge-first: **el conteo y el SQLite WAL son la fuente de verdad**; MQTT es best-effort con cola local + reintentos.

Responsabilidades:

1. **Conexion mTLS persistente** a `a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com:8883`, `client_id == THING_NAME`.
2. **Drenado de la cola** `CrossingEvents WHERE synced=0` del SQLite -> publica al topic de eventos (QoS 1).
3. **Confirmacion idempotente**: marca `synced=1` solo tras `on_publish` (PUBACK QoS1) del `mid` correspondiente.
4. **Subida de clips a S3** preservando el modelo de seguridad (IoT Credential Provider, sin llaves estaticas — seccion 1.5).
5. **LWT + status online/offline**, **telemetria/heartbeat** periodica.
6. **Suscripcion a Shadow + comandos** (seccion 2).

### 1.2 Cliente paho-mqtt sobre mTLS

```python
import ssl, paho.mqtt.client as mqtt

client = mqtt.Client(
    client_id=THING_NAME,                  # == ThingName, exigido por iot:Connect policy
    protocol=mqtt.MQTTv311,
    clean_session=False,                   # sesion persistente: el broker retiene subs y QoS1 in-flight
)
client.tls_set(
    ca_certs=CC_CA_FILE,                   # AmazonRootCA1.pem (pinneado)
    certfile=CC_CERT_FILE,                 # device.cert.pem
    keyfile=CC_KEY_FILE,                   # device.private.key (montada :ro, 0600)
    tls_version=ssl.PROTOCOL_TLSv1_2,
)
client.tls_insecure_set(False)             # valida el hostname del endpoint ATS

# Last Will: si el socket cae sin DISCONNECT, el broker publica offline (retained)
client.will_set(
    topic=f"{TOPIC_STATUS}",               # cam-counter/.../status/connection
    payload=json.dumps({"status":"offline","reason":"lwt","device_id":CC_DEVICE_ID,"ts_ms":0}),
    qos=1, retain=True,
)
client.reconnect_delay_set(min_delay=1, max_delay=120)   # backoff exponencial nativo
client.connect_async(CC_IOT_ENDPOINT, 8883, keepalive=30)
client.loop_start()
```

Notas clave:
- `clean_session=False` + QoS 1 da entrega at-least-once con persistencia de sesion; combinado con `event_id` determinista (sha1) + conditional put en la Lambda ingest, el reintento del mismo evento **no duplica**.
- `paho` gestiona reconexion y backoff; nuestra logica solo decide **que** drenar, no **como** reconectar.
- La llave privada nunca se lee a memoria por nuestro codigo: la pasa OpenSSL via `tls_set`.

### 1.3 Cola local offline + reintentos idempotentes

La cola **ya existe**: la tabla `CrossingEvents` del SQLite con la columna `synced`. No se introduce una segunda cola; se reusa el WAL como buffer durable (sobrevive reinicios y cortes de red indefinidos — edge-first).

Bucle de drenado (idempotente, ack-driven):

```python
# pseudo: un solo hilo de drenado, despierta por evento nuevo o por reconexion
def drain_loop():
    while True:
        if not connected.is_set():
            connected.wait()                       # bloquea hasta reconectar; nada se pierde
        rows = db.fetch("SELECT * FROM CrossingEvents WHERE synced=0 ORDER BY ts_event_ms LIMIT 50")
        for row in rows:
            payload = to_crossing_event_json(row)  # valida contra contracts/crossing_event.schema.json
            info = client.publish(TOPIC_EVENTS, payload, qos=1)
            inflight[info.mid] = row["event_id"]   # se confirma en on_publish
        idle_wait(new_event_or_timeout=5)

def on_publish(client, userdata, mid):
    event_id = inflight.pop(mid, None)
    if event_id:
        db.execute("UPDATE CrossingEvents SET synced=1, synced_ts_ms=? WHERE event_id=?",
                   (now_ms(), event_id))           # marca SOLO tras PUBACK
```

Garantias:
- **Idempotencia end-to-end**: `event_id` = sha1 determinista de la identidad del cruce. Aunque un evento se publique 2 veces (p.ej. crash entre PUBACK y el `UPDATE`), la Lambda hace conditional put `attribute_not_exists(PK) AND attribute_not_exists(SK)` -> no duplica.
- **Offline tolerante a dias**: si no hay red, `synced=0` se acumula; nada se pierde, el conteo local sigue. Al reconectar se drena en orden de `ts_event_ms`.
- **Control de profundidad**: `queue_depth` (eventos `synced=0`) se reporta en heartbeat para observabilidad; no se purga jamas por antiguedad (los eventos son el dato de negocio).
- **Anti-spoof defensa en profundidad**: la Rule/Lambda valida `topic(2)==payload.device_id` y `clientid()==THING_NAME`.

### 1.4 Mapeo CrossingEvent (SQLite -> payload MQTT)

Payload validado contra `contracts/crossing_event.schema.json` antes de publicar (falla cerrada: si no valida, no se publica y se loguea, el evento queda `synced=0` para inspeccion). El binario MP4 **nunca** va por MQTT (limite 128 KB/mensaje); solo viaja `clip_key` (puntero) + `clip_status`.

| SQLite | Payload MQTT | Notas |
|---|---|---|
| `event_id` (sha1) | `event_id` | idempotencia |
| `site_id`/`device_id`/`camera_id` | idem | identidad; el publisher los toma del `.env`, no confia ciegamente en el row |
| `ts_event_ms` | `ts_event_ms` | epoch ms del cruce |
| `direction`,`count_delta`,`confidence` | idem | |
| `config_version` | `line_config_version` | version de linea vigente |
| clave S3 determinista | `clip_key` | puntero, no binario |
| estado de subida | `clip_status` | `pending`/`uploaded` |

### 1.5 Subida de clips a S3 (conserva el modelo de seguridad)

Se **conserva** el flujo de clips a S3, pero **sin la llave estatica** del IAM user `raspberry`. Recomendacion del SPEC de seguridad: **IoT Credential Provider** (Opcion B).

- El `ClipRecorder` arma el MP4 (pre+post roll, cv2) y lo encola con la clave determinista `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.mp4`.
- El publisher obtiene **credenciales STS temporales** llamando al *credentials endpoint* de IoT con su **mismo cert mTLS**, asumiendo el role alias `cam-counter-edge-s3-role-alias` -> rol `cam-counter-edge-s3-role` (solo `s3:PutObject` bajo `media/*/${credentials-iot:ThingName}/*`, TLS-only).
- boto3 sube multipart con reintentos nativos (edge-first); las credenciales se auto-rotan (`durationSeconds` ~3600). No hay segundo secreto: la identidad sigue siendo el cert X.509.

```python
# obtener credenciales temporales via IoT Credential Provider (mismo cert mTLS)
# GET https://<creds-endpoint>.credentials.iot.us-east-1.amazonaws.com/role-aliases/{ROLE_ALIAS}/credentials
# headers: cert mTLS; respuesta: AccessKeyId/SecretAccessKey/SessionToken/Expiration
sess = boto3.Session(aws_access_key_id=..., aws_secret_access_key=..., aws_session_token=...)
s3 = sess.client("s3", region_name=CC_AWS_REGION, config=Config(retries={"max_attempts":10,"mode":"standard"}))
s3.upload_file(local_mp4, CC_MEDIA_BUCKET, clip_key)   # PutObject acotado al prefijo del device
# tras subir: UPDATE clip_status='uploaded'; opcional re-publish del evento con clip_status actualizado
```

Orden tolerante: si el evento MQTT llega antes que el clip, queda `clip_status:"pending"`; al terminar la subida se actualiza (`uploaded`). La reproduccion la sirve el dashboard con presigned GET (no es responsabilidad del device, que es write-only sobre su prefijo).

### 1.6 Migracion segura (dual-write, no big-bang)

Bandera `CC_SYNC_TRANSPORT={direct_iam|iot|dual}` (default `direct_iam`):
- **Fase 0**: `direct_iam` — comportamiento actual intacto, infra IoT desplegada en paralelo.
- **Fase 1 (canary)**: `dual` en `cam-counter-sitio-demo-rpi-001` — escribe directo a DynamoDB **y** publica por MQTT. El conditional put idempotente garantiza paridad sin duplicar.
- **Fase 2**: `iot` — solo MQTT. Validada la paridad.
- **Fase 3**: matar la llave `raspberry` (deny -> inactive -> delete), migrar clips a IoT Credential Provider, borrar `~/.aws/credentials`. El borrado del IAM user lo revisa un humano (MAD aborta ante destroy).

---

## 2. Suscripcion al Device Shadow (line-config + comandos) y reconciliacion

El edge ya recarga la linea-umbral en caliente leyendo `config_version` del SQLite (mecanismo `ConfigWatcher` existente). El Shadow se **conecta a ese mismo mecanismo**: es el canal de propagacion nube -> SQLite, coexistiendo con la edicion local de la UI.

### 2.1 Named shadow `line-config` (estado deseado persistente)

El publisher (o un hilo dedicado del proceso edge) se suscribe a los topics reservados del named shadow:

```
SUB  $aws/things/{THING}/shadow/name/line-config/update/delta     # cambios deseados desde la nube
SUB  $aws/things/{THING}/shadow/name/line-config/get/accepted     # estado al arrancar
PUB  $aws/things/{THING}/shadow/name/line-config/get              # solicita estado al boot
PUB  $aws/things/{THING}/shadow/name/line-config/update           # reporta lo aplicado (reported)
```

Loop de reconciliacion (coexiste con la UI local):

1. **Al arrancar**: `PUB .../get` -> recibe `.../get/accepted` con el `desired` acumulado offline.
2. **En caliente**: la nube (dashboard Amplify) escribe `desired`; IoT calcula el **delta** y lo publica en `.../update/delta`. El edge esta suscrito.
3. El edge **valida** el delta contra `contracts/line_config.schema.json` (falla cerrada si no valida).
4. **Arbitro por version (monotono, last-writer-wins por `version`)**: si `desired.version > config_version` local, escribe la nueva linea en el SQLite e **incrementa `config_version`**. Si `desired.version <= config_version` local (la UI local ya avanzo mas), **ignora el delta** y solo re-reporta. El edge NUNCA baja de version.
5. El `ConfigWatcher` existente detecta el cambio de `config_version` y recarga la linea **en caliente** (sin reiniciar el detector).
6. El edge publica `reported` con la `version` y `line` realmente aplicadas -> IoT cierra el delta.

```jsonc
// $aws/things/{THING}/shadow/name/line-config/update   (reported tras aplicar)
{ "state": { "reported": {
    "version": 43, "line": {"x1":0.10,"y1":0.55,"x2":0.90,"y2":0.55},
    "direction_positive":"in", "min_confidence":0.85, "applied_ts_ms": 1718900000000
}}}
```

### 2.2 Coexistencia UI local <-> nube (clave del diseno)

- **Fuente de verdad para el detector**: SIEMPRE el SQLite (`config_version`). Ni la UI local ni el Shadow escriben directo al detector; ambos escriben al SQLite y el `ConfigWatcher` recarga.
- **UI local edita**: incrementa `config_version` en SQLite (como hoy). El publisher detecta el cambio y **publica `reported`** al shadow para que la nube vea el estado real. Asi un cambio local se refleja como `reported` y, si la nube tenia un `desired` viejo, el delta se cierra por version.
- **Nube edita**: escribe `desired` -> delta -> SQLite -> `ConfigWatcher`. La UI local lo ve al releer el SQLite.
- **Conflicto**: gana la `version` mayor (monotono). No hay split-brain porque el SQLite es el unico punto de aplicacion y `config_version` es el arbitro unico.

### 2.3 Comandos fire-and-forget (`cmd/request` -> `cmd/ack`)

Comandos puntuales (`snapshot`, `restart`, `reload-config`) llegan por `cam-counter/{device_id}/cmd/request` (SUB). El edge ejecuta y publica `cmd/ack` correlando por `command_id` (idempotente: un `command_id` ya procesado se ackea sin re-ejecutar). Comandos que son **estado deseado persistente** (canal OTA, conteo on/off, target fps) van por el named shadow `command`/`ops`, no por `cmd/*`, para sobrevivir reconexiones.

```python
def on_message(client, userdata, msg):
    if msg.topic.endswith("/line-config/update/delta"):   reconcile_line_config(json.loads(msg.payload))
    elif msg.topic.endswith("/line-config/get/accepted"):  reconcile_line_config(json.loads(msg.payload), boot=True)
    elif msg.topic.endswith("/cmd/request"):               handle_command(json.loads(msg.payload))
    elif "/command/update/delta" in msg.topic:             reconcile_ops(json.loads(msg.payload))
```

---

## 3. Dockerizacion

Dos servicios `docker-compose` en la Pi: **`edge`** (detector + publicador MQTT + shadow + clips; necesita Hailo) y **`api`** (FastAPI + UI compilada). El SQLite WAL compartido se monta como volumen entre ambos. Se consolida el publicador MQTT dentro del contenedor `edge` (mismo proceso/sidecar que ya tiene el cert montado) para no duplicar la sesion mTLS ni el acceso al SQLite; un tercer servicio `sync` separado es opcional pero por defecto se integra en `edge`.

### 3.1 Hailo-8 en Docker: el mayor riesgo (concreto y pragmatico)

Correr inferencia Hailo dentro de un contenedor en la Pi 5 tiene cuatro retos reales. Aqui el detalle y como resolver cada uno:

**Reto 1 — Passthrough del dispositivo `/dev/hailo0`.**
El chip se expone como char device `/dev/hailo0` creado por el driver del kernel del HOST (`hailo_pci`). El contenedor no carga modulos de kernel; el driver vive en el host. Solucion:
- Instalar **HailoRT PCIe driver en el HOST** (DKMS, fuera de Docker). El contenedor NO trae el driver de kernel, solo el userspace.
- En compose: `devices: ["/dev/hailo0:/dev/hailo0"]`. Evitar `privileged: true` (sobre-permiso); el device-mapping puntual basta.
- Permisos: el `/dev/hailo0` del host pertenece al grupo `hailo`. El proceso del contenedor debe estar en ese grupo numerico via `group_add: [<gid-hailo-host>]` (el GID del host, no el nombre, porque el contenedor puede no tener el grupo).

**Reto 2 — Match de version driver(host) <-> HailoRT(userspace en imagen).**
HailoRT exige que la version del **userspace en la imagen** coincida (mismo major/minor) con la del **driver del kernel en el host**. Un mismatch da `HAILO_INVALID_DRIVER_VERSION` al abrir el device. Solucion:
- **Pinnear** la version de HailoRT en el Dockerfile a la MISMA que el driver del host (p.ej. `4.x`). Documentar la version del driver del host como prerequisito del nodo.
- Versionar la imagen edge con un sufijo que incluya HailoRT: tag `cam-counter-edge:1.4.0-hrt4.x-arm64`, para que un host con driver distinto no corra una imagen incompatible por accidente.
- El OTA / despliegue valida en el healthcheck que `hailortcli scan` / apertura del device tenga exito antes de marcar el contenedor healthy.

**Reto 3 — ARM64 + paginas de 16 KB (especifico de Raspberry Pi 5).**
La Pi 5 corre un kernel con **paginas de 16 KB** (Raspberry Pi OS reciente). HailoRT y sus buffers DMA deben estar compilados/alineados para 16 KB; binarios construidos asumiendo 4 KB pueden fallar en asignacion DMA. Solucion:
- Imagen base **ARM64** nativa, construida/ejecutada en la Pi 5 (no cross-build x86 que arrastre asunciones de 4 KB). Usar los **paquetes HailoRT oficiales para Pi 5 / 16 KB** (Hailo publica ruedas/paquetes especificos para el kernel de la Pi 5).
- Verificar en build/CI ARM64 real (un runner ARM64 o build-on-device); no confiar en emulacion `qemu` para validar el camino DMA.
- Confirmar que el **kernel del host** y el driver estan en modo 16 KB consistente; el contenedor hereda el page size del host (no es ajustable desde el contenedor), asi que el unico requisito es que el userspace de la imagen tolere 16 KB.

**Reto 4 — `cv2` (OpenCV) + decodificacion RTSP/ffmpeg dentro del contenedor.**
El edge usa `cv2` para el ClipRecorder y el detector; la UI decodifica RTSP con ffmpeg. Solucion:
- Instalar `opencv` y `ffmpeg` con las libs del sistema (`libgl1`, `libglib2.0-0`, `libavcodec`...) en la imagen ARM64. Preferir el `python3-opencv` del sistema o `opencv-python-headless` (sin GUI) para evitar dependencias X.
- El detector usa el **Python del sistema** (no venv) porque `hailo_platform` + `cv2` se enlazan a libs del sistema; la imagen edge reproduce ese entorno (system Python + paquetes Hailo + cv2), NO un venv aislado.

**Base de imagen recomendada (edge):** `arm64v8/debian:bookworm` (o la imagen base Raspberry Pi OS Bookworm) que case el ABI/glibc del host Pi 5, + paquetes oficiales HailoRT para Pi 5 (16 KB), + `python3`, `python3-opencv`/`opencv-python-headless`, `ffmpeg`, `paho-mqtt`, `boto3`. Construir **en la Pi** o en runner ARM64 real. El driver de kernel queda EXPLICITAMENTE fuera de la imagen (vive en el host).

### 3.2 `docker-compose.yml`

```yaml
# /opt/cam-counter/docker-compose.yml
services:
  edge:
    image: ghcr.io/jlsaco/cam-counter/edge:${CC_IMAGE_TAG:-1.4.0-hrt4.x-arm64}
    container_name: cam-counter-edge
    env_file: /opt/cam-counter/.env            # identidad por-dispositivo (seccion 4)
    devices:
      - /dev/hailo0:/dev/hailo0                 # passthrough Hailo-8 (NO privileged)
    group_add:
      - "${CC_HAILO_GID}"                       # GID numerico del grupo 'hailo' del HOST
    volumes:
      - /opt/cam-counter/certs:/certs:ro        # certs mTLS read-only, fuera de la imagen
      - cam-counter-db:/var/lib/cam-counter     # SQLite WAL compartido
      - /opt/cam-counter/clips:/var/lib/cam-counter/clips   # buffer de clips local
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "/usr/local/bin/edge-healthcheck"]   # hailo device OK + mqtt connected + db writable
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s                         # da tiempo a abrir el device y conectar mTLS
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "5" }

  api:
    image: ghcr.io/jlsaco/cam-counter/api:${CC_IMAGE_TAG:-1.4.0-arm64}
    container_name: cam-counter-api
    env_file: /opt/cam-counter/.env
    ports:
      - "127.0.0.1:${CC_API_PORT:-8088}:8088"   # same-origin; bind a loopback (TLS/reverse-proxy si se expone)
    volumes:
      - cam-counter-db:/var/lib/cam-counter     # mismo SQLite WAL (lee/escribe config + eventos)
      - /opt/cam-counter/clips:/var/lib/cam-counter/clips:ro
    depends_on:
      edge:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8088/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "5" }

volumes:
  cam-counter-db:
```

Decisiones:
- **`api` NO accede a Hailo ni a los certs**: solo lee/escribe el SQLite y sirve la UI. Sin device-mapping, sin certs montados (superficie minima).
- **SQLite WAL compartido por volumen nombrado**: ambos contenedores montan `cam-counter-db`. WAL tolera lectores/escritores concurrentes de procesos distintos en el mismo filesystem.
- **`api` bindeado a `127.0.0.1`**: la UI es same-origin local; si se expone fuera del host va tras TLS/reverse-proxy.
- **Certs SOLO read-only en `edge`**, montados desde `/opt/cam-counter/certs` (host, `chmod 700`, llave `0600`), NUNCA horneados en la imagen.

### 3.3 Healthchecks

- **`edge`**: script `edge-healthcheck` que verifica (1) apertura del device Hailo (`hailortcli scan` o open de `/dev/hailo0` con exito -> cubre el reto de version-mismatch), (2) cliente MQTT `connected`, (3) SQLite escribible, (4) RTSP decodificando (frame reciente). Falla cerrada -> contenedor unhealthy -> restart. `start_period: 60s` para no marcar unhealthy durante el arranque de la sesion mTLS.
- **`api`**: endpoint `/healthz` que valida SQLite legible y proceso vivo.
- El estado healthy es la senal que el OTA/despliegue usa para soak + rollback (consistente con el OTA pull-based existente).

### 3.4 Logging

- Driver `json-file` con rotacion (`max-size: 10m`, `max-file: 5`) para no llenar la SD de la Pi.
- Logs estructurados JSON a stdout (un evento por linea: `event_id`, `topic`, `mid`, `synced`, `queue_depth`). `docker logs` + posible reenvio futuro a CloudWatch via telemetria MQTT (no por agente pesado en la Pi).
- Nada de secretos en logs: la llave privada y los tokens STS jamas se loguean.

---

## 4. Inyeccion de config por-dispositivo (solo via `.env` del compose)

La **imagen Docker es generica para toda la flota**. Lo unico por-dispositivo son: (a) el volumen `certs` montado, y (b) el archivo `.env`. El `.env` lo produce el `provision-device.sh` (SPEC cert-provisioning) y vive en `/opt/cam-counter/.env` (no en git; `.gitignore` excluye `*.env`, `*.key.pem`, `certs/`).

### 4.1 `.env` del compose (identidad + rutas, NUNCA secretos AWS)

```dotenv
# /opt/cam-counter/.env  — montado por compose, no en git, no en la imagen

# Identidad del dispositivo (slugs ^[a-z0-9][a-z0-9-]{1,62}$)
CC_SITE_ID=sitio-demo
CC_DEVICE_ID=rpi-001
CC_CAMERA_ID=rpi-001-cam0
CC_THING_NAME=cam-counter-sitio-demo-rpi-001        # == client_id MQTT (derivable de site+device)
CC_RELEASE_CHANNEL=stable

# AWS IoT (sin credenciales estaticas: la identidad es el cert X.509)
CC_IOT_ENDPOINT=a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com
CC_AWS_REGION=us-east-1
CC_AWS_ACCOUNT_ID=950639281773
CC_ROLE_ALIAS=cam-counter-edge-s3-role-alias        # IoT Credential Provider -> S3 (clips)

# Rutas de certs DENTRO del contenedor (volumen :ro montado desde el host)
CC_CERT_DIR=/certs
CC_CERT_FILE=/certs/device.cert.pem
CC_KEY_FILE=/certs/device.private.key
CC_CA_FILE=/certs/AmazonRootCA1.pem

# Topics derivados (explicitos para el operador)
CC_TOPIC_EVENTS=cam-counter/sitio-demo/rpi-001/events/crossing
CC_TOPIC_STATUS=cam-counter/sitio-demo/rpi-001/status/connection
CC_TOPIC_TELEMETRY=cam-counter/sitio-demo/rpi-001/telemetry/heartbeat
CC_TOPIC_CMD=cam-counter/sitio-demo/rpi-001/cmd/request
CC_TOPIC_CMD_ACK=cam-counter/sitio-demo/rpi-001/cmd/ack
CC_SHADOW_LINE_CONFIG=line-config
CC_SHADOW_COMMAND=command

# Almacenamiento / app
CC_MEDIA_BUCKET=cam-counter-media-950639281773
CC_DB_PATH=/var/lib/cam-counter/cam-counter.db
CC_API_PORT=8088

# Migracion (bandera de transporte)
CC_SYNC_TRANSPORT=direct_iam                        # direct_iam | dual | iot

# Docker host-specifics (no son secretos)
CC_HAILO_GID=44                                     # GID numerico del grupo 'hailo' del HOST
CC_IMAGE_TAG=1.4.0-hrt4.x-arm64
```

### 4.2 Reglas de la inyeccion

- **Solo `.env` por-dispositivo**: el nombre del dispositivo (`CC_DEVICE_ID`/`CC_SITE_ID`) y todo lo especifico viven aqui. Cambiar de un device a otro = cambiar `.env` + volumen `certs`; **la imagen no cambia**.
- **Derivacion al arranque**: `CC_THING_NAME` y los `CC_TOPIC_*` son derivables de `CC_SITE_ID`/`CC_DEVICE_ID`; se ponen explicitos en el `.env` para legibilidad del operador, pero el codigo los **valida** al arrancar (recomputa y compara; aborta si no casan o si un slug no cumple `^[a-z0-9][a-z0-9-]{1,62}$`).
- **Cero credenciales AWS estaticas**: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` **no existen** en el `.env`. La unica identidad es el cert X.509 montado en `/certs`. El `mqtt_publisher` obtiene credenciales S3 temporales via IoT Credential Provider (`CC_ROLE_ALIAS`).
- **Validacion fail-closed al boot**: el contenedor `edge` valida en arranque que (1) los slugs cumplen el patron, (2) los archivos de cert existen y son legibles, (3) `CC_THING_NAME == cam-counter-{site}-{device}`, (4) el endpoint y la region son los esperados. Si algo falla, no arranca (mejor no-arrancar que conectar mal).
- **`certs` como secret/volumen**: en compose se montan como volumen `:ro`; si se evoluciona a Docker Swarm o se quiere endurecer, las mismas rutas pueden alimentarse via `secrets:` de compose sin tocar el codigo (lee de `/certs`). El `.env` puede gestionarse con un gestor de secretos del host (p.ej. `sops`/age) descifrandolo a `/opt/cam-counter/.env` antes de `docker compose up`.

### 4.3 Flujo operador (un device nuevo)

1. `provision-device.sh --site sitio-demo --device rpi-001` (en laptop admin) -> produce `device-bundle.tar.gz` (certs + `.env`).
2. `scp`/USB a la Pi -> `tar xzf` en `/opt/cam-counter/` -> `chmod 700 certs`, `chmod 600 device.private.key` -> borrar el bundle del laptop.
3. Ajustar `CC_HAILO_GID` y `CC_IMAGE_TAG` del host si difieren (unicos valores host-especificos no derivados de la identidad).
4. `docker compose --env-file /opt/cam-counter/.env up -d` -> `edge` valida identidad, abre Hailo, conecta mTLS, drena la cola; `api` sirve la UI en `127.0.0.1:8088`.

---

## Notas de consistencia y riesgos

- **Convencion de nombres a centralizar**: los SPEC divergen levemente (thing `cam-counter-{site}-{device}` vs `cam-counter-{site}__{device}`; topics `cam-counter/{device_id}/events/crossing` vs `cam-counter/evt/{site}/{thing}/crossing`). El codigo NO debe hardcodear ninguna: toma `CC_THING_NAME` y `CC_TOPIC_*` del `.env`, de modo que la convencion final se fija en provisioning/Terraform sin recompilar la imagen. **Recomendacion**: unificar en un PR de naming antes de Fase 1 para evitar drift entre la policy IoT (que incrusta el patron en los ARN) y los topics que publica el device.
- **Mayor riesgo tecnico**: Hailo-en-Docker (version driver-host vs userspace-imagen y paginas 16 KB de la Pi 5). Mitigado con: driver en el host (DKMS), HailoRT pinneado y reflejado en el tag de imagen, build/validacion en ARM64 real, y healthcheck que abre el device antes de marcar healthy.
- **Edge-first preservado**: SQLite WAL como cola durable; MQTT best-effort con QoS1 + `clean_session=False`; idempotencia por `event_id` + conditional put. Ningun fallo de nube detiene el conteo.

Archivos a crear/modificar (rutas en el repo `cam-counter`): `device/mqtt_publisher.py` (reemplaza `sync_runner`), `device/shadow_reconciler.py`, `device/healthcheck.py`, `docker/edge.Dockerfile`, `docker/api.Dockerfile`, `docker-compose.yml`, `.env.example`, y `scripts/provision-device.sh` (del SPEC de provisioning).