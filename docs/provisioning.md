# Provisioning de devices IoT — `scripts/provision-device.sh`

> Genera y vincula la **identidad IoT de UN Pi** de la flota con un solo comando, pensado para
> un operador no experto. Materializa lo **per-device FUERA del state de Terraform**: agregar,
> rotar o revocar un device **nunca** produce diff/destroy en el plan de Terraform de MAD.
>
> Complementa a [`naming-standard.md`](naming-standard.md) (canon de nombres) y a
> [`CLAUDE.md`](../CLAUDE.md) §3 (slugs) / §5 (cero secretos). Si algo contradice el
> naming-standard, **manda el naming-standard**.

---

## 1. Qué hace (flujo normal)

```
scripts/provision-device.sh --site <slug> --device <slug> [--camera N] [--channel stable|canary]
```

1. **Valida los slugs** (`^[a-z0-9][a-z0-9-]{1,62}$`) de `site`, `device`, `channel` y de cada
   `camera_id` derivado **antes** de componer cualquier nombre, key o topic.
2. **Genera llave privada + CSR en local** con `openssl` (RSA-2048). **La llave privada NUNCA
   viaja a AWS**: solo se envía el CSR.
3. `aws iot create-certificate-from-csr --set-as-active` → certificado X.509 del device.
4. `create-thing` (o `update-thing` si ya existe) con **thing-type** `cam-counter-edge-device` y
   atributos `site_id` / `device_id` / `channel`.
5. `add-thing-to-thing-group` a los grupos **por sitio** (`cam-counter-site-{site}`) y **por canal
   OTA** (`cam-counter-channel-{channel}`).
6. `attach-policy cam-counter-device-policy` **al certificado** + `attach-thing-principal` (cert↔Thing).
7. **Conditional put** del item en `cam-counter-devices` conforme al contrato
   [`device_registry_item.schema.json`](../contracts/device_registry_item.schema.json).
8. Descarga `AmazonRootCA1.pem`.
9. Genera el `.env` (solo claves `CAMCOUNTER_*`, **sin** credenciales AWS) y empaqueta
   `device-bundle.tar.gz` (certs + `.env` + `INSTALL.txt`).
10. Imprime **certificateArn**, **sha256 del bundle** y el recordatorio de canal seguro.

La salida vive en `out/provisioning/<thing_name>/` — **gitignored**; ningún cert/llave/bundle/`.env`
se versiona jamás.

### Nombres derivados (mismo canon que la policy de WP06)

| Cosa | Valor |
|---|---|
| **Thing name** = MQTT client-id | `cam-counter-{site}-{device}` |
| **Thing type** | `cam-counter-edge-device` |
| **Grupo por sitio** | `cam-counter-site-{site}` |
| **Grupo por canal OTA** | `cam-counter-channel-{channel}` |
| **IoT policy** (attach al cert) | `cam-counter-device-policy` |
| **Role alias** (en el `.env`) | `cam-counter-edge-s3-role-alias` |
| **Topic base** | `cam-counter/{device_id}` |

> **Topic ↔ policy (clave para no fallar en silencio):** el `.env` deriva los topics del
> **`device_id`** (`cam-counter/{device_id}/...`, naming-standard §3), **no** del thing name.
> Para que la policy de WP06 autorice ese publish, el script fija `device_id` como **atributo del
> Thing**, de modo que la variable de policy `${iot:Connection.Thing.Attributes[device_id]}`
> resuelve al mismo `device_id`. Si el topic del `.env` y la variable de la policy no coincidieran,
> IoT Core **denegaría el publish en silencio**.

---

## 2. Flags

| Flag | Default | Descripción |
|---|---|---|
| `--site <slug>` | (requerido) | `site_id` (slug ASCII minúscula). |
| `--device <slug>` | (requerido) | `device_id` (slug ASCII minúscula, único global). |
| `--camera N` | `1` | Nº de cámaras lógicas → `camera_ids = {device}-cam1..N`. |
| `--channel <stable\|canary>` | `stable` | Canal OTA. **Solo** valores del enum del contrato. |
| `--rotate` | — | Emite un cert nuevo y **retira** (INACTIVE + detach) los anteriores del Thing. |
| `--revoke` | — | Pone los certs del Thing en **REVOKED**, los desvincula y marca el device `offline`. |
| `--dry-run` | — | Solo local: valida slugs y genera llave/CSR/`.env`. **No** llama a AWS de forma mutante. |
| `--help` | — | Ayuda. |

`--channel` se valida contra el enum del contrato (`canary` \| `stable`). El roadmap menciona
otros canales; **'beta' no existe** en el contrato y el script lo **rechaza** para no escribir un
item con un canal inválido.

---

## 3. Idempotencia

- **Re-ejecutar `provision`** sobre un Thing que ya tiene cert(s) **no crea un cert nuevo** (evita
  duplicar/orfanar certs cuya llave privada ya no se puede recuperar). Re-asegura policy/grupos/item
  y termina. Para reemplazar el cert usa `--rotate`.
- El **item de registry** se inserta con `attribute_not_exists(PK)`: un segundo run **no
  sobrescribe** la identidad.
- `create-thing`/`add-thing-to-thing-group`/`attach-policy`/`attach-thing-principal` son
  idempotentes (re-aplicar no duplica).

### Doble escritor del item `cam-counter-devices`

Este script es el **escritor AUTORITATIVO en bootstrap** (conditional put). El hook
`cam-counter-devices-register` (WP08) hace **upsert tolerante** a item preexistente desde
status/telemetry (`UpdateItem`, nunca reescribe la identidad). No se duplican esquemas: ambos
respetan `device_registry_item.schema.json`.

---

## 4. Rotación y revocación

```
scripts/provision-device.sh --site <slug> --device <slug> --rotate   # cert nuevo, los viejos a INACTIVE
scripts/provision-device.sh --site <slug> --device <slug> --revoke   # certs a REVOKED + device offline
```

- **`--rotate`**: requiere un Thing existente. Emite un cert nuevo (CSR nuevo, llave nueva), lo
  activa y lo adjunta; luego **desvincula y desactiva (INACTIVE)** todos los certs anteriores.
  Regenera el bundle con la nueva identidad.
- **`--revoke`**: pone **REVOKED** todos los certs del Thing, los desvincula del Thing y de la
  policy, y marca `status=offline` en el registry. El Thing y el item se **conservan para
  auditoría** (el operador puede borrarlos manualmente después).

---

## 5. Requisitos previos (infra estable — WP06)

El script ejecuta un **preflight** que aborta **sin tocar nada** si falta la infra estable, que
crea el módulo Terraform `iot-core` y **aplica el runner MAD** (issue #42 / WP06) **antes** de
provisionar devices:

- thing-type `cam-counter-edge-device`
- policy `cam-counter-device-policy`
- grupos `cam-counter-site-{site}` y `cam-counter-channel-{channel}`
- role-alias `cam-counter-edge-s3-role-alias` (solo se referencia en el `.env`; su ausencia es
  un *warning*, no aborta)

> **Credenciales:** el script usa las credenciales **del operador** (`aws-cli` configurado),
> **no** la identidad admin `raspberry` del runner MAD ni `terraform apply`. No crea ni modifica
> infra de Terraform: solo recursos **per-device** fuera del state.

---

## 6. El `.env` del bundle (solo `CAMCOUNTER_*`, sin credenciales AWS)

El `.env` generado contiene **exclusivamente** claves `CAMCOUNTER_*` (las que lee el código;
`CC_*` está prohibido). **No** contiene `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`: la subida de
clips a S3 va por el **role-alias** (WP04), cambiando el cert X.509 por credenciales STS de corta
vida. Incluye identidad, endpoint ATS, rutas de certs en `/etc/cam-counter/certs/`, los cuatro
topics derivados del `device_id` y `CAMCOUNTER_SYNC_TRANSPORT=direct`.

---

## 7. Instalación en el Pi

```bash
tar -xzf device-bundle.tar.gz
sudo mkdir -p /etc/cam-counter/certs
sudo cp certs/device.cert.pem certs/device.private.key certs/AmazonRootCA1.pem /etc/cam-counter/certs/
sudo chmod 600 /etc/cam-counter/certs/device.private.key
cp .env <ruta de despliegue del servicio>/.env
```

**Distribuye el bundle SOLO por un canal seguro** (p. ej. `scp` directo al Pi): contiene la
**llave privada** del device. Nunca por git, email ni chat.

---

## 8. Ejemplo

```bash
# Provisionar el Pi de ejemplo (sitio-demo / rpi-001) con 2 cámaras en el canal stable:
scripts/provision-device.sh --site sitio-demo --device rpi-001 --camera 2 --channel stable
# → Thing 'cam-counter-sitio-demo-rpi-001' visible en la consola de AWS IoT Core.
```

> El device de ejemplo `cam-counter-sitio-demo-rpi-001` se provisiona en cuanto el runner MAD
> aplica el módulo `iot-core` (WP06). Antes de eso, el preflight aborta limpiamente indicando qué
> recurso de infra falta. Para validar el camino local sin AWS: añade `--dry-run`.
