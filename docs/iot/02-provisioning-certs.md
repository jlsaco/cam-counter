# Provisioning y certificados

> Cómo crear el certificado X.509 de un dispositivo nuevo, vincularlo a AWS IoT Core y entregarlo al contenedor Docker de la Raspberry. Pensado para que un operador NO experto lo haga en un comando.

## TL;DR — Recomendación

**Usa la Opción 1: script `provision-device.sh` one-command** para la fase actual (flota pequeña, alta supervisión, migración segura desde el IAM user `raspberry`). Es el camino más simple y auditable. **Deja Fleet Provisioning by claim como evolución futura** cuando la flota crezca y los dispositivos se aprovisionen sin operador presente.

---

## Estándar de nombres (lo que crea el provisioning)

Todo deriva de `site_id` y `device_id` (slugs `^[a-z0-9][a-z0-9-]{1,62}$`). El nombre canónico del dispositivo es:

```
DEVICE_FULL = {site_id}__{device_id}          # ej. sitio-demo__rpi-001
```

> Se usa `__` (doble guion bajo) como separador porque `/` y `#` están prohibidos en slugs, y `__` no colisiona con un slug válido (que solo admite `a-z0-9-`).

| Recurso | Patrón | Ejemplo |
|---|---|---|
| IoT Thing | `cam-counter-{site}__{device}` | `cam-counter-sitio-demo__rpi-001` |
| Thing Type | `cam-counter-edge` | — |
| Thing Group | `cam-counter-fleet` | — |
| IoT Policy (por dispositivo) | `cam-counter-edge-policy` (una policy parametrizada por variables de política `${iot:Connection.Thing.ThingName}`) | — |
| Certificado (alias/tag) | tag `Name=cam-counter-{site}__{device}` | — |
| Item en `cam-counter-devices` | PK `DEVICE#{device_id}` | `DEVICE#rpi-001` |
| MQTT topic eventos | `cam-counter/{site_id}/{device_id}/events` | `cam-counter/sitio-demo/rpi-001/events` |
| Named Shadow (config) | `config` (named shadow `$aws/things/{thing}/shadow/name/config`) | — |
| Docker container | `cam-counter-edge`, `cam-counter-api`, `cam-counter-sync` | — |

---

## Dónde viven las llaves privadas (regla de oro)

1. La **llave privada del dispositivo NUNCA entra a git, ni a S3, ni a DynamoDB, ni a logs.** Se genera, se entrega una vez al operador, y se monta en el contenedor.
2. AWS IoT **no almacena la llave privada** — solo la entrega en el momento de crear el cert. Si se pierde, se rota (no se recupera).
3. En la Raspberry viven en `/opt/cam-counter/certs/` con permisos `600` (dueño root o el usuario del contenedor), y se montan **read-only** como volumen Docker.
4. El bundle `device-bundle.tar.gz` que produce el script se trata como **secreto**: se transfiere por canal seguro (USB/scp), se descomprime en la Pi, y se borra del laptop del operador.

```
/opt/cam-counter/certs/        (en la Raspberry, chmod 700)
├── device.key.pem    600   <- llave privada (NUNCA sale de aquí)
├── device.cert.pem   644   <- cert del dispositivo
└── AmazonRootCA1.pem  644   <- CA raíz de Amazon
```

---

## Comparación de las 2 opciones

| Criterio | **(1) provision-device.sh one-command** | **(2) Fleet Provisioning by claim** |
|---|---|---|
| Quién corre qué | Operador con credenciales admin corre 1 script y obtiene `device-bundle.tar.gz` | Imagen lleva un **claim cert compartido**; el device se auto-registra al primer arranque |
| Llave privada del device | Generada en el laptop del operador, entregada una vez | Generada en el propio device (nunca sale de él) — más seguro |
| Complejidad de setup | Baja: un script bash + IAM admin | Alta: provisioning template, claim cert, policy de claim, hook Lambda de pre-registro, rotación del claim cert |
| Riesgo de credencial compartida | Ninguno (no hay cert compartido) | El **claim cert es compartido**: si se filtra de una imagen, hay que rotarlo en toda la flota |
| Auditabilidad | Total: cada device lo registra un humano con su identidad | El registro lo hace el device solo; auditas vía CloudTrail/template |
| Escala (flota grande, sin operador) | Pobre: requiere humano por device | Excelente: cero toque, ideal para fábrica |
| Migración desde estado actual | Inmediata, no rompe nada | Requiere más infra antes de dar valor |
| Encaja con `mad`/Terraform | Sí, el script es un artefacto del repo; la policy/thing-type/group son Terraform | Sí, pero más módulos Terraform |

### Por qué se recomienda (1) ahora

- La flota es pequeña y el objetivo inmediato es **matar la llave estática del IAM user `raspberry`** sin un big-bang.
- El operador no es experto: **un comando** es más simple que entender claim certs + templates.
- Es **aditivo y reversible**, encaja con la convención de PRs apilados y `terraform apply` autónomo de `mad` (la infra estable —policy, thing-type, thing-group— va en Terraform; el script solo crea el thing+cert+registro por dispositivo).
- Cuando la flota crezca, se migra a (2) reusando la misma policy y el mismo estándar de nombres.

---

## Camino recomendado paso a paso (Opción 1)

### Fase A — Infra estable (una sola vez, vía Terraform / `mad`)

Esto NO lo toca el operador; se aprovisiona una vez con Terraform en `terraform/modules/iot-provisioning` (nuevo módulo):

1. **Thing Type** `cam-counter-edge`.
2. **Thing Group** `cam-counter-fleet`.
3. **IoT Policy** `cam-counter-edge-policy` **parametrizada** (una sola policy para toda la flota, el aislamiento lo dan las variables de política):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "iot:Connect",
      "Resource": "arn:aws:iot:us-east-1:950639281773:client/${iot:Connection.Thing.ThingName}" },
    { "Effect": "Allow", "Action": "iot:Publish",
      "Resource": "arn:aws:iot:us-east-1:950639281773:topic/cam-counter/${iot:Connection.Thing.Attributes[site_id]}/${iot:Connection.Thing.Attributes[device_id]}/events" },
    { "Effect": "Allow", "Action": ["iot:Subscribe"],
      "Resource": "arn:aws:iot:us-east-1:950639281773:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/config/*" },
    { "Effect": "Allow", "Action": ["iot:Receive","iot:Publish"],
      "Resource": "arn:aws:iot:us-east-1:950639281773:topic/$aws/things/${iot:Connection.Thing.ThingName}/shadow/name/config/*" }
  ]
}
```

> Un device solo puede conectarse con un client-id == su ThingName, publicar a SU topic de eventos, y leer/escribir SU named shadow `config`. Un cert robado no puede suplantar a otro device.

### Fase B — Provisionar un dispositivo nuevo (el operador, un comando)

```bash
./provision-device.sh --site sitio-demo --device rpi-001
```

El script (corre con credenciales admin) hace, en orden idempotente:

1. **Valida** que `site` y `device` cumplen el slug `^[a-z0-9][a-z0-9-]{1,62}$`.
2. **Genera** la llave privada y un CSR localmente (`openssl`), de modo que la llave privada **nunca viaja a AWS**:
   ```bash
   openssl genrsa -out device.key.pem 2048
   openssl req -new -key device.key.pem -subj "/CN=cam-counter-${SITE}__${DEVICE}" -out device.csr
   ```
3. **Crea el cert desde el CSR** y lo activa:
   `aws iot create-certificate-from-csr --certificate-signing-request file://device.csr --set-as-active` → guarda `certificateArn` y `device.cert.pem`.
4. **Crea el Thing** `cam-counter-${SITE}__${DEVICE}` con type `cam-counter-edge` y **atributos** `site_id`, `device_id` (que la policy parametrizada usa).
5. **Adjunta** el thing al group `cam-counter-fleet`, **adjunta** la policy `cam-counter-edge-policy` al cert, y **adjunta** el cert al thing.
6. **Registra** el item en `cam-counter-devices` (PK `DEVICE#{device_id}`, GSI1 `CHANNEL#{release_channel}`) conforme a `contracts/device_registry_item.schema.json`, con `certificate_id`, `thing_name`, `site_id`, `provisioned_at`, `provisioned_by={operator}`, `status=active`. Usa **conditional put** (`attribute_not_exists`) para ser idempotente.
7. **Descarga** `AmazonRootCA1.pem`.
8. **Empaqueta** el bundle (la llave privada se queda solo aquí):
   ```
   device-bundle.tar.gz
   ├── certs/device.key.pem
   ├── certs/device.cert.pem
   ├── certs/AmazonRootCA1.pem
   └── .env
   ```
9. **Imprime** el `certificateArn` y un checksum, y recuerda al operador: *transferir por canal seguro y borrar el bundle del laptop.*

> El script es **idempotente**: si lo corres dos veces para el mismo device, detecta el thing/registro existente y no duplica (salvo `--rotate`, ver abajo).

### Fase C — Entregar el cert al contenedor Docker (en la Raspberry)

1. Operador copia `device-bundle.tar.gz` a la Pi (scp/USB) y lo extrae en `/opt/cam-counter/`:
   ```bash
   sudo mkdir -p /opt/cam-counter/certs
   sudo tar xzf device-bundle.tar.gz -C /opt/cam-counter
   sudo chmod 700 /opt/cam-counter/certs
   sudo chmod 600 /opt/cam-counter/certs/device.key.pem
   sudo rm device-bundle.tar.gz   # borra el bundle
   ```
2. Los certs se montan **read-only** como volumen en `docker-compose.yml`; el `.env` aporta la identidad:

```yaml
services:
  cam-counter-edge:
    image: ghcr.io/jlsaco/cam-counter-edge:latest
    container_name: cam-counter-edge
    env_file: /opt/cam-counter/.env
    volumes:
      - /opt/cam-counter/certs:/certs:ro      # certs read-only, fuera de la imagen
    devices:
      - /dev/hailo0:/dev/hailo0               # passthrough Hailo-8
    restart: unless-stopped
```

> Las llaves se **montan**, no se hornean en la imagen. La imagen Docker es genérica para toda la flota; lo único por-dispositivo son el volumen `certs` y el `.env`.

### `.env` que produce el script (montado, no en git)

```dotenv
# identidad del dispositivo
DEVICE_ID=rpi-001
SITE_ID=sitio-demo
THING_NAME=cam-counter-sitio-demo__rpi-001

# AWS IoT
IOT_ENDPOINT=a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com
AWS_REGION=us-east-1

# rutas de certs (dentro del contenedor, montadas read-only)
CERT_DIR=/certs
DEVICE_CERT=/certs/device.cert.pem
DEVICE_KEY=/certs/device.key.pem
ROOT_CA=/certs/AmazonRootCA1.pem

# topics / shadow (derivados, pero explícitos para el operador)
MQTT_TOPIC_EVENTS=cam-counter/sitio-demo/rpi-001/events
SHADOW_NAME=config

RELEASE_CHANNEL=stable
```

> `.env` lleva identidad y rutas, **no secretos** más allá de apuntar a la llave montada. El `.gitignore` del repo debe excluir `*.env`, `*.key.pem`, `*.tar.gz` y `certs/`.

---

## Rotar / revocar un certificado

**Rotar** (cert comprometido o caducidad — sin downtime perceptible):

```bash
./provision-device.sh --site sitio-demo --device rpi-001 --rotate
```

1. Genera **nueva** llave+CSR y **nuevo** cert activo; adjunta policy + thing.
2. Entrega un nuevo bundle; el operador lo despliega y reinicia el contenedor (`docker compose restart cam-counter-edge`).
3. Verificado el nuevo cert conectando (aparece en IoT como conectado), el script:
   - marca el **viejo** cert `INACTIVE` → espera ventana de gracia → lo `DELETE` (detach policy/thing primero).
   - actualiza `cam-counter-devices` con el nuevo `certificate_id` y `rotated_at`.

**Revocar** (device perdido/robado/dado de baja — corte inmediato):

```bash
./provision-device.sh --site sitio-demo --device rpi-001 --revoke
```

1. `aws iot update-certificate --new-status REVOKED` (corte inmediato del mTLS).
2. Detach policy y thing del cert; `delete-certificate`.
3. En `cam-counter-devices`: `status=revoked`, `revoked_at`, `revoked_by`.
4. (Opcional) detach/borrar el Thing si el device se retira definitivamente.

> Revocar el cert es lo único necesario para cortar el acceso a la nube: sin cert válido el device no puede conectarse por mTLS. No hay llaves estáticas que rotar en IAM — ese es justamente el `raspberry` IAM user que esta iniciativa elimina.

---

## Checklist de seguridad para el operador

- [ ] Nunca subir `device.key.pem`, `*.env`, ni `device-bundle.tar.gz` a git (verificado por `.gitignore` + pre-commit).
- [ ] Transferir el bundle por canal seguro (USB/scp), nunca por email/chat.
- [ ] Borrar `device-bundle.tar.gz` del laptop tras desplegarlo en la Pi.
- [ ] Certs en la Pi con `chmod 600` la llave, montados `:ro` en Docker.
- [ ] Un cert por dispositivo; al dar de baja un device, **revocar** su cert.
- [ ] Confirmar en la consola IoT que el device aparece *connected* tras el primer arranque.