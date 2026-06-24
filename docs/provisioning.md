# Provisioning de dispositivos — `scripts/provision-device.sh`

> Dar de alta la identidad **AWS IoT Core** de un Pi de la flota con **un comando**,
> pensado para un operador **no experto**. El script materializa **sólo lo por-device** y
> **fuera del state de Terraform**: añadir un dispositivo **nunca** produce diff/destroy en
> el `terraform plan` del runner MAD.
>
> Canon de nombres y prefijo de entorno: [`docs/naming-standard.md`](naming-standard.md).
> Contrato del item de registro: [`contracts/device_registry_item.schema.json`](../contracts/device_registry_item.schema.json).

---

## TL;DR

```bash
# Alta de un device (canal 'stable' por defecto):
scripts/provision-device.sh --site casa --device rpi-001 --camera 1 --channel stable

# Previsualizar sin tocar AWS (valida slugs, computa nombres, imprime .env e item):
scripts/provision-device.sh --site casa --device rpi-001 --dry-run

# Rotar el certificado de un device ya provisionado:
scripts/provision-device.sh --site casa --device rpi-001 --rotate

# Revocar (desactivar + borrar) el certificado de un device:
scripts/provision-device.sh --site casa --device rpi-001 --revoke
```

El resultado de un alta es un **bundle** `provisioning/<thing-name>/device-bundle.tar.gz`
(certs + `.env`) que se transfiere al Pi **por un canal seguro**.

---

## Qué hace (flujo normal)

1. **Valida los slugs** `site_id`, `device_id`, `channel` contra el regex canónico
   `^[a-z0-9][a-z0-9-]{1,62}$` **antes** de componer cualquier nombre/topic/clave.
2. **Genera llave privada + CSR en LOCAL** (`openssl genrsa` / `openssl req`). **La llave
   privada NUNCA viaja a AWS** ni a git: a IoT sólo se envía el **CSR**.
3. `aws iot create-certificate-from-csr --set-as-active` → certificado X.509 del device.
4. `create-thing` con **atributos** `site_id`, `device_id`, `camera_count`,
   `release_channel`. El atributo **`device_id` es obligatorio** (ver
   [Topics y policy](#topics-y-policy-wp06)).
5. `add-thing-to-thing-group` al **grupo de sitio** (`cam-counter-site-{site_id}`) y al
   **grupo de canal** (`cam-counter-channel-{channel}`).
6. `attach-policy cam-counter-device-policy` al **cert**.
7. `attach-thing-principal` (vincula cert ↔ thing).
8. **Conditional put** del item en DynamoDB `cam-counter-devices`
   (`attribute_not_exists(PK)`), conforme al contrato.
9. Descarga `AmazonRootCA1.pem`.
10. Genera `.env` con **sólo claves `CAMCOUNTER_*`** y empaqueta el **bundle**; imprime
    `certificateArn` + `sha256` del bundle + recordatorio de canal seguro.

---

## Flags

| Flag | Default | Descripción |
|---|---|---|
| `--site <slug>` | — (obligatorio) | `site_id`. |
| `--device <slug>` | — (obligatorio) | `device_id`. |
| `--camera <N>` | `1` | Nº de cámaras lógicas. Genera `camera_ids = {device}-cam1..N`. |
| `--channel <c>` | `stable` | Canal OTA. **Sólo `canary` \| `stable`** (enum del contrato). |
| `--rotate` | — | Re-emite cert/llave para un device existente; desactiva el cert anterior. |
| `--revoke` | — | Revoca (INACTIVE→REVOKED→delete) el cert del device. |
| `--dry-run` | — | Valida + computa nombres + imprime `.env`/item, **sin** tocar AWS/red. |
| `--region <r>` | `us-east-1` | Región (o `CAMCOUNTER_AWS_REGION`). |
| `--out-dir <d>` | `./provisioning` | Raíz de salida (o `CAMCOUNTER_PROVISION_OUT`). Gitignored. |

---

## Topics y policy (WP06)

Los topics del `.env` siguen el canon de [`naming-standard.md`](naming-standard.md) §3:

```
cam-counter/{device_id}/events/crossing
cam-counter/{device_id}/status
cam-counter/{device_id}/telemetry
cam-counter/{device_id}/cmd
```

La **device-policy** de WP06 (`cam-counter-device-policy`) es **una sola política** para
toda la flota, parametrizada con una **variable de política**. Para aislar por dispositivo
usando el `device_id` (y no el thing name completo), acota el publish/subscribe a:

```
cam-counter/${iot:Connection.Thing.Attributes[device_id]}/*
```

Por eso este script crea el Thing con el **atributo `device_id`**: el topic del `.env` y el
que concede la policy **derivan del mismo valor**. Si no coincidieran, **IoT denegaría el
publish en silencio**. Este es el motivo de que el atributo `device_id` sea obligatorio.

> El **MQTT client-id** es **idéntico al thing name** (`cam-counter-{site_id}-{device_id}`),
> lo que habilita `${iot:Connection.Thing.IsAttached}` y el aislamiento basado en thing.

---

## El `.env` (sólo `CAMCOUNTER_*`, sin credenciales AWS)

El `.env` generado contiene **exclusivamente** claves `CAMCOUNTER_*` (el prefijo que LEE el
código; `CC_*` está **prohibido**). Incluye identidad (`SITE_ID`/`DEVICE_ID`), rutas a los
certs en `/etc/cam-counter/certs/`, endpoint ATS, topics derivados del `device_id`,
`CAMCOUNTER_ROLE_ALIAS=cam-counter-edge-s3-role-alias` y
`CAMCOUNTER_SYNC_TRANSPORT=direct`.

**No contiene** `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`: la subida de clips a S3 va por
el **role alias** (IoT Credentials Provider, WP04) — el cert mTLS se cambia por credenciales
STS de corta vida. Cero llaves estáticas en el dispositivo.

---

## Instalación en el Pi

```bash
# En tu máquina: transferir el bundle por canal seguro (scp/USB), NUNCA por email/chat.
scp provisioning/cam-counter-casa-rpi-001/device-bundle.tar.gz pi@<ip>:/tmp/

# En el Pi:
sudo mkdir -p /etc/cam-counter/certs
sudo tar -xzf /tmp/device-bundle.tar.gz -C /etc/cam-counter
sudo chmod 600 /etc/cam-counter/certs/device.private.key /etc/cam-counter/certs/device.cert.pem
# El .env del bundle alimenta la config del servicio (cam-counter-edge).
```

---

## Idempotencia

Re-ejecutar el alta con los **mismos flags** no duplica nada:

- Si ya hay un cert **ACTIVO** en el estado local, se **reutiliza** (no se crea otro).
- `create-thing` se vuelve `update-thing` si el Thing ya existe.
- `add-thing-to-thing-group`, `attach-policy` y `attach-thing-principal` son idempotentes.
- El `put-item` usa `attribute_not_exists(PK)`: **no sobreescribe** el item si ya existe
  (preserva el `reported_version`/`status` que escribe el Pi por heartbeat).

**Escritor autoritativo del item de registry**: este script es el escritor **autoritativo
en bootstrap**. El hook `cam-counter-devices-register` (WP08) hace **upsert tolerante** a un
item preexistente (sólo actualiza `reported_version`/`last_seen_at`/`status`). No duplican
esquema.

---

## `--rotate`

1. Genera **nuevo** par llave+CSR y un cert **ACTIVO**; le adjunta la policy y lo vincula al
   Thing.
2. El cert **anterior** se **detacha** (policy + thing-principal) y se marca **INACTIVE** (no
   se borra: permite rollback dentro de la ventana de retención).
3. Se re-empaqueta el bundle con el nuevo cert/llave. Transfiérelo al Pi y reinicia el
   servicio.

## `--revoke`

Localiza el cert (estado local o, en su defecto, vía `list-thing-principals`), lo **detacha**
de policy y Thing, lo marca **REVOKED** y lo **borra** (`--force-delete`). El **Thing y su
item de registry siguen existiendo**; para re-emitir credenciales, vuelve a ejecutar el alta.

---

## Dependencia de WP06 (infra estable)

El **thing type**, los **thing groups**, la **device-policy** y el **role alias** son
recursos de **Terraform (WP06)**; este script **no los crea** (los materializaría fuera del
state y rompería el `terraform apply` de WP06). Si aún **no** están aplicados, el script
**avisa** y continúa con lo que sí puede hacer (thing + cert + registry); una **re-ejecución
idempotente** completa los `attach` (type, groups, policy) cuando WP06 esté aplicado.

---

## Cero secretos en git

`certs/*.pem`, `*.private.key`, `*.csr`, `AmazonRootCA1.pem`, el `.env` y el
`device-bundle.tar.gz` están cubiertos por [`.gitignore`](../.gitignore) (la salida vive bajo
`provisioning/`, ignorada por completo). **Nunca** se commitea un cert, una llave privada ni
un `.env` real. La llave privada se genera local con `chmod 600` y jamás sale del dispositivo
salvo dentro del bundle, que viaja por canal seguro.

> **Guardarraíl:** el script usa las credenciales AWS del entorno tal cual; **no** toca la
> identidad admin del runner (`raspberry` / `~/.aws`).
