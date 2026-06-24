# Reconciliación de contratos — `crossing_event` + `line_config` *verbatim* (WP02)

> **Fuente de verdad de contratos.** Los JSON Schemas canónicos de `contracts/`
> (`crossing_event.schema.json`, `line_config.schema.json`) son la **única**
> definición de los payloads del producto. Este documento **no** los modifica:
> los **reconcilia** con el transporte (MQTT IoT Core + named shadows) y elimina
> los campos **inventados** por specs derivadas. Manda `CLAUDE.md` (§3 ids, §4
> coords, §8 contratos) y `docs/naming-standard.md` (topics §3, shadows §4).

## 0. Resumen ejecutivo (decisiones)

1. **El payload MQTT de eventos de cruce ES `crossing_event.schema.json` VERBATIM.**
   Se publica tal cual en `cam-counter/{device_id}/events/crossing`. El **clip va
   aparte a S3**; el evento sólo lleva la **referencia** `clip_key`.
2. **El `desired` de la named shadow `line-config` ES `line_config.schema.json`
   VERBATIM.** Multi-cámara se resuelve con **una named shadow por cámara**:
   `line-config-{camera_id}`.
3. **Campos INVENTADOS eliminados / PROHIBIDOS**: `count_delta`,
   `line_config_version`, `direction_positive`, `min_confidence`, `version` (en
   `line_config`). No existen en ningún contrato y `additionalProperties:false`
   los rechaza. El campo de versión de línea correcto es **`line_version`**
   (espejo del `config_version` de la línea en vigor).
4. **`event_id` determinista** = `sha1(site_id|device_id|camera_id|track_id|crossing_seq)`
   con **`ts_event_ms` inmutable por `event_id`** (persistido con el evento, no
   recomputado al publicar).
5. **Idempotencia edge→cloud unificada**: device y Lambda usan la **misma**
   condición exacta `attribute_not_exists(PK) AND attribute_not_exists(SK)`.
6. **Sin bump por defecto**: `schema_version` permanece `1`. Enriquecer un
   contrato es un **bump controlado** (§6), fuera de alcance salvo necesidad
   demostrada.

El gate `tests/contracts/` + `.github/workflows/contracts.yml` + `make
validate-contracts` **falla cerrado** ante cualquier desviación de lo anterior.

---

## 1. Evento de cruce → MQTT `events/crossing` (payload = contrato verbatim)

- **Topic** (ver `naming-standard.md` §3): `cam-counter/{device_id}/events/crossing`,
  `device → cloud`. Lo enruta la IoT Rule `cam_counter_crossing_ingest`
  (`cam-counter/+/events/crossing`) a la Lambda `cam-counter-events-ingest`.
- **Payload**: el objeto `CrossingEvent` de `crossing_event.schema.json`, **sin
  envoltorio ni campos extra**. `additionalProperties:false` ⇒ un campo de más
  hace **fallar cerrado** la validación en la Lambda y el device no publicaría
  nada inválido.
- **`required` que el contrato EXIGE** (todos presentes en el payload):
  `event_id`, `site_id`, `device_id`, `camera_id`, `track_id`, `crossing_seq`,
  `direction`, `ts_event_ms`, `ts_event_iso`, `schema_version`.
- **Opcionales** admitidos: `positive_label`, `negative_label`, `label`,
  `line_version`, `confidence`, `clip_key`, `clip_status`, `synced`,
  `created_at`. (`synced` es flag **sólo-local** SQLite; **no** se persiste en la
  nube aunque viaje en el sobre — la Lambda lo ignora.)

### 1.1 El clip va aparte; el evento sólo lo referencia

El binario del clip/gif/snapshot **no** viaja por MQTT: se sube a S3 con la clave
`media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}`
(`CLAUDE.md` §7). El evento transporta únicamente la **referencia** `clip_key` y
el `clip_status` (`pending|uploading|uploaded|failed`). Así el payload MQTT se
mantiene pequeño y el media sigue su propio ciclo de subida.

### 1.2 `track_id` + `crossing_seq` SON obligatorios en el payload

> Resuelve la nota [ALTA] del revisor.

`event_id` se **recomputa cloud-side** para detectar duplicados; esa recomputación
necesita la **tupla de identidad completa**. Por eso `track_id` y `crossing_seq`
viajan en el payload (el contrato ya los marca `required`). Sin ellos el
`event_id` no sería reproducible en la nube y la deduplicación idempotente se
rompería. El test `test_event_id_is_deterministic` recomputa el `sha1` desde la
tupla y exige que coincida con el del ejemplo.

### 1.3 `ts_event_ms` INMUTABLE por `event_id` (y por qué importa de verdad)

> Resuelve la nota [ALTA] del revisor.

`ts_event_ms` **no** forma parte del `event_id` (el id sólo depende de la tupla de
identidad). Debe **persistirse junto al evento en el momento del conteo** y
**nunca recomputarse al publicar** (ni en reintentos). Razón concreta, no
cosmética:

- `event_id` es determinista e **idempotente por sí mismo** (un reintento del
  mismo cruce produce el mismo id).
- Pero la clave DynamoDB es `PK = CAM#{site_id}#{device_id}#{camera_id}` y
  **`SK = TS#{ts_event_ms:013d}#{event_id}`**: el `ts_event_ms` **forma parte de
  la SK**.
- Si un reintento recomputara `ts_event_ms` (p. ej. “ahora”), el `event_id`
  seguiría igual pero la **SK cambiaría** ⇒ el *conditional put* sobre
  `(PK, SK)` **no** vería el item previo y **duplicaría** el cruce.
- Conclusión: `ts_event_ms` es **autoritativo y congelado** en el instante del
  cruce. `ts_event_iso` es su espejo legible y se deriva del mismo valor.

### 1.4 `direction` cableado vs. etiquetas humanas

`direction` ∈ `{in, out}` es el **único valor almacenado** del sentido. Los
términos humanos (`subieron`/`bajaron`) **no** se almacenan como tales: viven en
`positive_label`/`negative_label`, y `label` es el resuelto en el momento del
evento. El sentido positivo lo fija `line_config.positive_side` (ver §2).

---

## 2. Config de línea → named shadow `line-config` (desired = contrato verbatim)

- **Shadow** (ver `naming-standard.md` §4): named shadow **por cámara**
  `line-config-{camera_id}`, topics reservados
  `$aws/things/{thingName}/shadow/name/line-config-{camera_id}/...`.
- **`desired`**: el objeto de `line_config.schema.json`, **verbatim** (sin
  envoltorio). Lo escribe cloud/UI (Lambda `cam-counter-line-publish`).
- **`reported`**: el mismo contrato, escrito por el device tras aplicar la config
  (hot-reload). La reconciliación de shadow reemplaza el polling.
- **Nombres de campo correctos** (NO los inventados):
  - `config_version` — **NO** `version` ni `line_config_version`. Contador
    monótono; el pipeline relee al observar uno mayor (hot-reload sin reinicio) y
    se refleja en `crossing_event.line_version`.
  - `positive_side` ∈ `{-1, 1}` — **NO** `direction_positive`. Qué semiplano
    cuenta como `direction='in'`.
  - `required`: `site_id`, `device_id`, `camera_id`, `config_version`, `line`,
    `positive_side`, `schema_version`.

### 2.1 Multi-cámara: una named shadow por cámara

> Resuelve la nota [MEDIA] del revisor: el contrato `line_config` es **por
> cámara** (`camera_id` required) pero una shadow es **por *thing*** (por device).

Un *thing* (= un Pi/`device_id`) puede tener **N cámaras** (`camera_id =
{device_id}-cam{N}`, `CLAUDE.md` §3). Para que cada `desired` sea el contrato
**verbatim** (un único `line_config`, sin mapas que romperían
`additionalProperties:false`), se usa **una named shadow por cámara**:

```
$aws/things/{thingName}/shadow/name/line-config-{camera_id}
```

- El nombre `line-config` de `naming-standard.md` §4 es el **dominio/prefijo**; la
  instancia concreta por cámara es `line-config-{camera_id}`. Para un device de
  una sola cámara queda `line-config-{device_id}-cam1`.
- **Alternativa descartada (map por `camera_id`)**: una sola shadow
  `line-config` cuyo `desired` fuese `{ "<camera_id>": {line_config}, ... }`. Se
  descarta porque el `desired` **dejaría de ser el contrato verbatim** (sería un
  envoltorio) y obligaría a un schema-wrapper aparte. Se prefiere la shadow por
  cámara: `desired` == `line_config` exacto.
- **Restricción de longitud**: el nombre de shadow de AWS IoT admite
  `[a-zA-Z0-9:_-]` hasta **64 chars**. `camera_id` puede llegar a 63, así que
  `line-config-{camera_id}` podría exceder 64 en casos patológicos. En la práctica
  los `camera_id` son cortos (`rpi5-puerta-cam1`); si algún día se acercara al
  límite, se acortaría el `camera_id`, no el contrato.

---

## 3. Idempotencia unificada device ⇄ Lambda

> Resuelve la nota [MEDIA] del revisor y se alinea con WP05.

La sincronización edge→cloud es idempotente por la **combinación** de:

1. `event_id` **determinista** (mismo cruce ⇒ mismo id), y
2. un **conditional put** en DynamoDB con la **misma condición EXACTA** en device
   y en la Lambda de ingesta:

```
attribute_not_exists(PK) AND attribute_not_exists(SK)
```

- `PK = CAM#{site_id}#{device_id}#{camera_id}`,
  `SK = TS#{ts_event_ms:013d}#{event_id}`.
- Un reintento del **mismo** evento (mismo `event_id` **y** mismo `ts_event_ms`,
  ver §1.3) reescribe la **misma** `(PK, SK)`; la condición la rechaza sin
  duplicar. El rechazo (`ConditionalCheckFailedException`) se trata como
  **éxito idempotente**, no como error.
- Cualquier código que escriba en `cam-counter-events` (device directo vía
  `CAMCOUNTER_SYNC_TRANSPORT=direct`, o Lambda vía `iot`) **debe** usar esta
  condición textual, sin variantes.

---

## 4. Campos inventados — inventario y reemplazo

| Inventado (PROHIBIDO) | Dónde se vio | Reemplazo correcto |
|---|---|---|
| `count_delta` | specs derivadas de evento | (no existe) el conteo se deriva agregando `CrossingEvent` por `direction`; no hay delta en el evento |
| `line_config_version` | specs derivadas de evento | **`line_version`** (entero, espejo del `config_version` en vigor) |
| `direction_positive` | specs derivadas de línea | **`positive_side`** ∈ `{-1, 1}` |
| `min_confidence` | specs derivadas | (no existe en el contrato) la confianza viaja en `confidence` del evento; un umbral de filtrado es parámetro del pipeline, no del contrato |
| `version` (en `line_config`) | specs derivadas de línea | **`config_version`** |

Ninguno aparece en `contracts/`. Como ambos contratos son
`additionalProperties:false`, cualquier reaparición **falla la validación**: el
gate de CI tiene un ejemplo inválido por cada uno (`tests/contracts/.../invalid/`).

---

## 5. El gate (qué corre y dónde)

- **Ejemplos canónicos**: `tests/contracts/examples/{crossing_event,line_config}/valid/`
  — el `full.json` de cada uno **es** el payload MQTT / `desired` de shadow
  verbatim. `minimal_required_only.json` cubre el caso de sólo `required`.
- **Ejemplos inválidos**: `.../invalid/` — uno por campo inventado y por cada
  `required` real omitido; el test exige que **fallen por su motivo declarado**.
- **Test**: `tests/contracts/test_contracts.py` (sólo `jsonschema` + `pytest`,
  sin el paquete del producto).
- **Local**: `make validate-contracts`.
- **CI**: `.github/workflows/contracts.yml` corre en cada PR que toque
  `contracts/`, `tests/contracts/` o el workflow. **No** toca AWS.

---

## 6. Plan de bump de `schema_version` (por defecto NO se ejecuta)

> Guardarraíl: por defecto los contratos **no** se tocan (`schema_version`
> permanece `1`, `additionalProperties:false`). Enriquecer = **bump controlado**.

Si una necesidad **demostrada** exigiera añadir/renombrar un campo:

1. **Aditivo opcional** (campo nuevo opcional, sin tocar `required`): aun así es
   un cambio de contrato; se hace bump de `schema_version` a `2` para que el
   consumidor distinga generaciones. Productores nuevos emiten `2`; los
   consumidores aceptan `{1,2}` durante la transición.
2. **Rename / cambio de tipo / nuevo `required`** = **BREAKING**: bump
   obligatorio, y se mantiene compatibilidad doble-lectura en la Lambda hasta que
   toda la flota reporte la versión nueva (`reported_version`/`agent_version`).
3. **Procedimiento**: actualizar el `.schema.json` (incl. `const` de
   `schema_version`), añadir ejemplos `valid`/`invalid` de la nueva generación,
   actualizar este documento y la tabla de `naming-standard.md` si aplica, y abrir
   el cambio como su propio WP apilado. El gate de contratos debe seguir verde con
   ejemplos de **ambas** generaciones mientras dure la transición.

Mientras tanto, **cualquier** PR que intente colar un campo fuera de contrato
queda bloqueado por el gate (rojo), que es exactamente el objetivo de WP02.
