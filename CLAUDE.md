# CLAUDE.md — Documento maestro del monorepo `cam-counter`

> Este archivo es la fuente de verdad de arquitectura y convenciones para **todo** el
> monorepo `cam-counter`. Lo leen tanto las personas que contribuyen como los agentes
> automatizados. Si algo de aquí entra en conflicto con un documento más antiguo, **manda
> este archivo**. Es un documento de BOOTSTRAP: describe convenciones que regirán toda la
> iniciativa apilada de PRs; no crea recursos ni mueve archivos por sí mismo.

---

## 1. Identidad del proyecto y entorno AWS

- **Producto**: `cam-counter` evoluciona de un detector de personas (Raspberry Pi 5 +
  acelerador Hailo-8 + YOLOv8s, cámara EZVIZ por RTSP) hacia un **producto de conteo de
  personas que cruzan una línea-umbral configurable**, en tiempo real, en el borde
  (*edge-first*), **multi-cámara y multi-sitio**, con UI local en LAN servida desde el Pi
  y una flota de Pis actualizable por **OTA**.
- **Monorepo**: todo (borde, backend, UI, infraestructura, contratos, OTA, CI) vive en
  este único repositorio `github.com/jlsaco/cam-counter` (rama por defecto `main`,
  usuario de git `jlsaco`).
- **Cuenta AWS**: `950639281773`.
- **Región**: `us-east-1`.
- **Prefijo de recursos**: `cam-counter-` (todos los recursos AWS del producto lo usan).

---

## 2. Arquitectura integrada (resumen)

La iniciativa se compone de **cinco subsistemas** que conviven en el monorepo:

1. **Edge-counting** (paquete Python en el Pi). Pipeline:
   `captura → detect (Hailo) → track → count → present + clip + sink`.
   - `count` = detección de **cruce de línea** con **histéresis** (banda muerta para evitar
     rebotes) e **idempotencia por track** (un mismo `track_id` no recuenta el mismo cruce).
   - Un **`DummyDetector`** permite ejercitar **toda** la lógica de conteo en x86 sin
     hardware Hailo ni cámara, de modo que la verificación corre en CI x86.
   - `present` emite **MJPEG** como vídeo en vivo; `clip` graba el recorte del evento;
     `sink` persiste en **SQLite (modo WAL)**.
2. **API + UI local** (mismo Pi).
   - **FastAPI** sirve una **SPA React/Vite/Tailwind** *same-origin* (sin CORS).
   - El vídeo en vivo es el stream **MJPEG**; la **línea de conteo** se dibuja como
     **overlay SVG** en **coordenadas normalizadas** 0..1.
   - SQLite **WAL** compartida entre el proceso de conteo y la API.
   - **Hot-reload de configuración** vía `config_version`: cambiar la línea u otros
     parámetros NO reinicia el servicio; el pipeline relee la config al detectar un
     `config_version` mayor. La configuración de la línea vive **en local**.
3. **Cloud AWS (Terraform)**.
   - Bucket de **media** nuevo, tabla **DynamoDB de eventos**, tabla **device-registry**,
     **IAM por-Pi** de mínimo privilegio, **rol OIDC de plan** (read-only) y **rol OIDC de
     deploy** *gated* por GitHub Environment para **publicar objetos S3 de release**.
4. **Flota OTA pull-based**.
   - Tarball **firmado (Ed25519 / minisign)** publicado al **bucket de releases** con un
     **manifiesto de versión deseada por canal**. El **update-agent** del Pi reconcilia
     versión deseada vs. actual, verifica `sha256` y firma, instala de forma **atómica**,
     hace **health-check** del producto y **auto-rollback** si falla.
5. **CI/CD y versionado**.
   - **Tags SemVer anotados** como **única** fuente de verdad de versión (ver §6).

---

## 3. Convenciones de identificadores

- `site_id`, `device_id`, `camera_id` son **slugs ASCII en minúscula** que cumplen el
  regex **`^[a-z0-9][a-z0-9-]{1,62}$`**.
- **PROHIBIDOS** los caracteres `#` y `/`:
  - `#` delimita claves compuestas en DynamoDB (PK/SK).
  - `/` delimita rutas (*keys*) en S3.
- `camera_id` **global único** = `{device_id}-cam{N}`.
- `event_id` **determinista** = `sha1` en hex-minúscula de
  `site_id|device_id|camera_id|track_id|crossing_seq`. El `sha1` se usa **sólo para
  deduplicación**, **no** con propósito criptográfico.
- **Validar el regex ANTES** de construir cualquier clave de S3 o DynamoDB.

---

## 4. Coordenadas

- **Toda** la geometría (línea de conteo, bounding boxes en contratos, overlays) usa
  **floats normalizados 0..1** relativos al **frame original de inferencia**, con origen
  **arriba-izquierda**.
- **Nunca** se almacenan píxeles en los contratos persistidos: los píxeles dependen de la
  resolución y romperían la portabilidad entre cámaras/resoluciones.

---

## 5. Cero secretos + OIDC + modelo de despliegue (F2/F3)

- **Cero secretos en git**: no hay claves AWS de larga vida en el repositorio. Las
  credenciales de cámara **nunca** se commitean (van por `env` / **SSM** / **SQLite**).
  `gitleaks` correrá en CI más adelante.
- **GitHub Actions asume AWS vía OIDC**: proveedor `token.actions.githubusercontent.com`,
  confianza acotada a `repo:jlsaco/cam-counter` (con condiciones de `sub` por
  ref/tag/environment) y `aud=sts.amazonaws.com`, y **SÓLO para PLAN** (read-only) con el
  rol de plan.
- **Quién ejecuta `terraform apply` de infraestructura**: lo ejecuta el **RUNNER MAD** de
  forma **AUTÓNOMA** (`terraform apply -auto-approve`) dentro de los PRs de infra, usando
  las credenciales de **su ENTORNO** (jamás commiteadas), contra la cuenta **REAL**
  `950639281773` / `us-east-1`. Este apply **no es humano**.
- **El apply de infra NO es responsabilidad humana ni está condicionado a un gate de
  merge**: la asume el runner MAD de forma autónoma, **antes** del merge humano. Dicho con
  el término habitual: **NO** está *merge-gated*; lo ejecuta el runner. Esta convención
  **reemplaza** cualquier idea previa de que ese paso fuese tarea de una persona.
- **GitHub Actions CI permanece SOLO-PLAN**: `terraform fmt` / `validate` / `plan` de sólo
  lectura vía OIDC. CI **NUNCA** ejecuta `terraform apply` de infraestructura (esto cierra
  la escalada de privilegios desde PRs no confiables).
- **Apply autónomo acotado y justificado (F2)**: el runner aplica **sólo** los módulos
  curados y enumerados de cada PR de la pila; **nunca** HCL arbitrario de terceros, y el
  plan se inspecciona para ser estrictamente aditivo (F1).
- **MATIZACIÓN (release vs. infra)**: los **workflows de release / promote SÍ publican
  OBJETOS S3** (artefactos OTA y manifiestos de canal) con el **rol de deploy
  `cam-counter-gha-deploy`** *gated* por GitHub Environment. Publicar objetos S3 con un rol
  de deploy gated **NO es `terraform apply` de infraestructura**: son cosas distintas y
  ambas son legítimas. Por eso "CI plan-only" se refiere a **infraestructura**, y no marca
  como violación a los workflows de release.
- **Idempotencia + verificación real**: cada apply de infra es **idempotente** (un segundo
  apply = "0 to add, 0 to change, 0 to destroy") y su Definition of Done **verifica
  recursos reales** (`aws s3api head-bucket`, `aws dynamodb describe-table`,
  `aws iam get-role` / `get-open-id-connect-provider`, etc.).
- **TAGS (F3)**: `default_tags` **capitalizados** exactamente
  `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }` **MÁS** tags lógicos
  en **minúscula** `project = "cam-counter"` y `managed_by = "mad-runner"` en **todos** los
  recursos. La verificación de `managed_by=mad-runner` busca la clave en **minúscula**. La
  clave **capitalizada** `ManagedBy` **siempre** vale `terraform`; **nunca** se usa la clave
  capitalizada con el valor del runner. Ver §6 (F3).
- **Teardown**: existe `terraform destroy` / script de teardown documentado (los recursos
  son de bajo costo: S3 + DynamoDB on-demand).
- **Credenciales por-Pi**: STS de **corta vida** desde un rol de **mínimo privilegio** por
  dispositivo.

---

## 6. Invariantes transversales de la pila de infra (F1 / F2 / F3)

Estas son **invariantes DURAS**, aplicables a **TODOS** los PRs de infra de la pila
(**PR02** state backend, **PR03** OIDC + roles, **PR04** media bucket + tablas
events/devices + IAM per-Pi, … , **PR11** releases bucket) y a la OVERVIEW.

### F1 — State aditivo y monótono
- Hay **UN** solo state de producción compartido por toda la pila apilada:
  `terraform/environments/prod`, con **lock en DynamoDB**.
- El runner MAD **SÓLO** ejecuta `terraform apply` desde la **rama apilada MÁS ALTA** que
  contiene **todo** el HCL acumulado hasta ese punto.
- **NUNCA** se reaplica una rama inferior después de que una superior ya aplicó (p. ej.,
  reaplicar PR03 tras PR04 haría que el plan proponga **destruir** los recursos de PR04 que
  no están en la config inferior).
- **ANTES de cada apply**: ejecutar `terraform plan` e **inspeccionarlo**. Si aparece
  **cualquier `destroy` o `replace`** de un recurso ya existente, **ABORTAR** el apply. El
  plan de cada PR de infra debe ser **estrictamente aditivo** (solo "to add"). Esta
  inspección es **paso explícito** e ítem del Definition of Done de cada PR de infra.

### F2 — Apply autónomo acotado y justificado
- El runner MAD aplica HCL **antes** del merge humano **exclusivamente** porque el HCL
  proviene de **esta pila controlada y curada** y porque el plan se inspecciona para ser
  estrictamente aditivo (F1).
- El runner **NO** aplica HCL arbitrario de terceros: el apply autónomo se **restringe** a
  los módulos enumerados de cada PR de infra.
- **GitHub Actions CI permanece plan-only**, lo que **cierra la escalada** de privilegios
  desde PRs no confiables.
- **Trade-off ACEPTADO y declarado**: el HCL se materializa en AWS real **antes** del gate
  humano de merge; se acepta porque proviene de la pila curada y el **plan aditivo (F1)** es
  el *guard* que impide cambios destructivos.

### F3 — Tags unificados
- Esquema **idéntico** en PR02 / PR03 / PR04 / PR11 y en la OVERVIEW.
- `default_tags` **CAPITALIZADOS** exactamente:
  `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }`.
- **MÁS** tags lógicos en **MINÚSCULA** en **todos** los recursos:
  `project = "cam-counter"` y `managed_by = "mad-runner"`.
- La verificación de `managed_by=mad-runner` busca la clave en **MINÚSCULA**
  (`managed_by`).
- La clave **capitalizada** `ManagedBy` **SIEMPRE** vale `terraform`; **nunca** se atribuye
  el valor del runner a la clave capitalizada.

---

## 7. Regla de los TRES buckets S3 (nunca mezclados)

1. **`cam-counter-rpi-artifacts-950639281773`** — bucket **EXISTENTE** de backup de
   binarios de operación. **RESERVADO: NO TOCAR**, **NO reutilizar** para media del
   producto.
2. **`cam-counter-media-950639281773`** — bucket **NUEVO** de **media del producto**
   (clips / gifs / snapshots).
   - Convención de claves de media:
     `media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}`.
3. **`cam-counter-fleet-releases-950639281773`** — bucket **NUEVO** de **artefactos OTA +
   manifiestos de canal**.

Todos los buckets **NUEVOS**: privados, **BlockPublicAccess** all-true, **SSE-S3**,
**bucket-owner-enforced** (Object Ownership), y **política `deny` TLS-only** (deniega
peticiones no cifradas).

---

## 8. Contratos compartidos (descritos EN PROSA)

> Los cuatro contratos se **formalizarán como ficheros JSON dentro del directorio
> `contracts/` en PR01**. Aún **no existen**; por eso aquí se describen **en prosa** y **sin
> enlaces a archivos** (un verificador de enlaces fallaría con enlaces colgantes).

### (A) CrossingEvent
- Evento de cruce, **snake_case**, `schema_version = 1`.
- Campos: `event_id` (**determinista**, ver §3), `site_id`, `device_id`, `camera_id`,
  `track_id`, `direction` (`'in' | 'out'`, valor de **cable/almacenado**; los términos
  humanos `subieron` / `bajaron` los llevan `positive_label` / `negative_label`), `label`,
  `line_version`, `ts_event_ms`, `ts_event_iso`, `confidence`, `clip_key`, `clip_status`,
  `schema_version`, `synced`, `created_at`.
- Persistencia:
  - **SQLite**: `UNIQUE(event_id)`.
  - **DynamoDB**: `PK = CAM#{site_id}#{device_id}#{camera_id}`,
    `SK = TS#{ts_event_ms:013d}#{event_id}`, **GSI1 por sitio**.
- El `event_id` determinista **+** *conditional put* en DynamoDB es lo que hace
  **IDEMPOTENTE** la sincronización edge→cloud: un reintento del **mismo** `event_id`
  **no duplica**. Este contrato se validará contra **DynamoDB REAL** en PR10.

### (B) Version string
- **SemVer canónico** derivado de **tags git anotados** vía `scripts/version.py`
  (`git describe`, **sin** archivo `VERSION` commiteado).
- La **MISMA** cadena fluye por: bundle-manifest, manifiesto de canal, device-registry y
  `/api/device` (`app_version`).
- El CI de release usa `actions/checkout` con `fetch-depth: 0` (para que `git describe` vea
  los tags).

### (C) Device registry
- **DynamoDB** `cam-counter-devices`, `PK = DEVICE#{device_id}`, **GSI1 por canal**
  (`CHANNEL#{release_channel}`).
- Campos: `device_id`, `site_id`, `camera_ids`, `release_channel`
  (`'canary' | 'stable'`), `desired_version` (lo escribe **cloud**; es **espejo /
  observabilidad**), `reported_version` (lo escribe el **Pi**), `last_good_version`,
  `last_update_status`, `last_seen_at`, `agent_version`, `status`, `hardware`.
- **IMPORTANTE**: el update-agent **NUNCA** lee `desired_version` del registry para decidir;
  la **única** fuente de la versión deseada es el **manifiesto del canal en S3**.

### (D) Release manifest
- **UN único** bucket de releases `cam-counter-fleet-releases-950639281773`, con un objeto
  **por canal**: `channels/<channel>/manifest.json`.
- Contenido: `{ schema_version, channel, version, sequence, artifact: { key, sha256,
  size_bytes, sig_key }, native_blob, min_agent_version, released_at, released_by, git_sha,
  previous_version }`.
- **Escritores**: SÓLO los **workflows de release / promote** (regla de **escritor único**,
  `ETag` `If-Match`), con el **rol de deploy gated por Environment**.
- **Lector**: el **agente OTA** vía **SigV4** (nunca URLs *presigned*).

---

## 9. Modelo de PRs apilados (stacked) + política de merge OBLIGATORIA

- La iniciativa se entrega como una **pila de PRs apilados**: cada PR **ramifica sobre la
  rama del PR anterior** (**PR00 ramifica sobre `main`**), y se abre con `base` = rama del
  PR anterior para que el diff sea limpio y revisable **de abajo hacia arriba**.
- Un humano revisa y mergea **de abajo hacia arriba**.

### Política de merge OBLIGATORIA: **NUNCA `squash`**
- Se permite **merge-commit** o **rebase-merge**; **PROHIBIDO `squash`**.
- **Por qué**: `squash` reescribe la historia de la base y **desincroniza** las ramas
  apiladas superiores.

### Procedimiento de rebase de la pila tras cada merge
- Tras mergear `PRn` a `main`, para **cada** rama superior `PRn+1, PRn+2, …` ejecutar:
  ```bash
  git rebase --onto main <base-antigua> <rama-superior>
  git push --force-with-lease
  ```
- El detalle operativo vive en `.claude/README.md`.

### Nota sobre los PRs de infra
- Dentro de los PRs de infra (**PR02, PR03, PR04, … , PR11**), el **runner MAD** ejecuta
  `terraform apply -auto-approve` de **forma AUTÓNOMA** (no humano). GitHub Actions CI
  permanece **plan-only**. Rige el **state aditivo y monótono (F1)**: sólo se aplica desde
  la rama apilada **más alta**, **nunca** se reaplica una inferior tras una superior, y se
  **aborta** ante **cualquier `destroy` / `replace`** en el plan.

---

## 10. Stack del proyecto (referencia)

- **Infra**: Terraform (módulos por servicio, estado remoto en **S3** + **lock en
  DynamoDB**, prefijo `cam-counter-`, `us-east-1`, cuenta `950639281773`).
- **Borde y backend**: **Python**.
- **UI**: **React + TypeScript + Vite + Tailwind**.
- **E2E**: **Playwright** contra la UI local.
- **Edge-first / tolerante a offline**: el Pi cuenta y **persiste en LOCAL (SQLite)** aunque
  no haya internet; la nube sólo recibe sincronización/histórico.
- **Versionado**: tags git **SemVer anotados** (`vX.Y.Z`, prereleases `-rc.N`) son la
  **única** fuente de verdad; un futuro `scripts/version.py` lo derivará vía `git describe`
  (sin archivo `VERSION` commiteado). **Hoy NO hay tags**; el sistema debe **degradar
  limpio** cuando no hay tags.
