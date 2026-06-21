# `ota/` — flota OTA pull-based + update-agent + release/promote firmados

Actualización **OTA pull-based** de la flota de Raspberry Pi. Un tarball **firmado
(Ed25519 / minisign)** se publica al bucket de releases
(`cam-counter-fleet-releases-950639281773`) con un **manifiesto de versión deseada por canal**
(`channels/<channel>/manifest.json`). El **update-agent** del Pi reconcilia versión deseada
vs. actual, verifica `sha256` **y** firma, instala de forma **atómica**, hace **health-check
de PRODUCTO con soak** y **auto-rollback** si falla.

> **Regla de oro:** la **única** fuente de la versión deseada es el **manifiesto del canal en
> S3**, leído vía **SigV4** (nunca presigned URLs). El agente **jamás** lee `desired_version`
> del device-registry para decidir (ese campo es espejo/observabilidad).

---

## Estructura

```
ota/
  agent/        update-agent (Python, sólo stdlib para verificar firmas)
    ed25519.py     Ed25519 (RFC 8032) puro — interoperable con minisign
    minisign.py    parse/verify de firmas minisign (+ firma efímera para tests)
    config.py      config LOCAL (canal, rutas, soak); overridable por env
    interfaces.py  Protocols inyectables (S3 / systemd / health / clock / registry)
    clients.py     implementaciones de producción (boto3 SigV4 / systemctl / urllib / DynamoDB)
    health.py      health-check de PRODUCTO con ventana de SOAK
    installer.py   instalación atómica + swap de symlink + retención + .part + failed-markers
    agent.py       orquestador de reconciliación
    __main__.py    entrypoint (`python3 -m agent`)
    keys/cam-counter-release.pub   pubkey minisign FIJADA (pública)
  packaging/make-release.sh        empaquetador determinista del borde (desde v1/)
  systemd/      cam-counter-update.{service,timer}  (oneshot + jitter + Persistent)
  tools/        validate_manifest.py · fleet_status.py · selftest_release_bucket.py
  tests/        suite pytest (x86, sin Pi/Hailo/cámara) + integración real gated
```

Relacionados (fuera de `ota/`): `scripts/publish_manifest.py` (publica/repunta el manifiesto),
`scripts/build_edge_artifact.sh` (wrapper de build de CI), `.github/workflows/release.yml` y
`.github/workflows/promote.yml`, `terraform/modules/fleet-releases/` (el bucket).

---

## Layout en dispositivo (immutable + activación atómica)

```
/opt/cam-counter/
  releases/<version>/   inmutable, una carpeta por versión instalada
  current               symlink atómico -> releases/<version> activa
  shared/               estado mutable que SOBREVIVE a cada upgrade/rollback
    ota/last_good       última versión que pasó el health-check (objetivo de rollback)
    ota/failed-<ver>    marcador por versión fallida (no se reintenta)
    agent.toml          config LOCAL del agente (canal asignado, etc.)
```

- `releases/` y `current` viven en el **MISMO filesystem** (`/opt/cam-counter`): el `rename`
  del symlink (`os.replace`) es **atómico**. La activación nunca deja un estado a medias.
- `shared/` **puede ser otro mount** (sólo datos: DB SQLite, config de línea, `CAM_IP`,
  sysroot box64, lib SDK). **Nunca** se cruza con un rename atómico.
- Las **deps pesadas del host** (box64, ffmpeg, hailo-all, el `.hef`) quedan **FUERA** del
  payload OTA (se instalan en provisioning). El **`native_blob`** se referencia por
  `key`+`sha256` en el manifiesto, vive en S3 y **NO va embebido** en el tarball.
- Ficheros `.part` (descargas/extracciones a medias) se **descartan al ARRANQUE del agente**
  (no sólo al boot del SO).

---

## Flujo del update-agent (`python3 -m agent`)

1. **Arranque:** descarta `.part`.
2. Lee el **canal asignado** de la config **LOCAL** (`shared/agent.toml`), nunca de la red.
3. `GET channels/<channel>/manifest.json` del bucket de releases **vía SigV4** (boto3/IAM).
4. Si `desired == current` → NOOP (heartbeat `healthy`).
5. Si `desired` tiene **failed-marker** → SKIP (no se reintenta esa versión).
6. Descarga `artifact` + `.minisig`; verifica **`sha256` PRIMERO**, **firma minisign DESPUÉS**
   contra la **pubkey fijada**. Cualquier fallo → **no instala**.
7. Extrae a temp (mismo FS), **rename atómico** a `releases/<version>/`, **swap del symlink**
   `current`, `systemctl restart cam-counter.service`.
8. **Health-check de PRODUCTO con SOAK** (ventana ~90–120 s): exige servicio `active`,
   **`frames_processed` creciente por cámara**, `last_inference_ts` reciente,
   `db_schema_version` esperado, `config_version` cargado, `app_version` == versión instalada
   y **sin crash-loop** (`NRestarts` estable). Un **200 estático no basta**: "200 pero
   frames=0" se trata como **fallo** y dispara rollback.
9. **Éxito:** `last_good = <version>`, conserva ≥ 2 releases (nunca borra `last_good`),
   heartbeat `reported_version`/`status=online`/`last_update_status=healthy`.
10. **Fallo:** revierte el symlink a `last_good`, reinicia, escribe **failed-marker por
    versión**, heartbeat `last_update_status=rolled_back`/`failed` + `last_update_error`.

Offline-tolerante: un **solo salto** a la versión current del canal al reconectar (no una
cola de versiones). El agente **nunca** requiere DynamoDB para actualizarse.

### Contrato de health (`/api/health`)

```json
{ "status": "ok|degraded", "app_version": "...", "db_schema_version": 3,
  "frames_flowing": true,
  "cameras": [ { "camera_id": "...", "frames_processed": 123,
                 "last_inference_ts": 1700000000000, "config_version": 1 } ] }
```

---

## Versión = tags git (única fuente)

`scripts/version.py` deriva la versión SemVer vía `git describe` (sin archivo `VERSION`). La
**MISMA** cadena fluye por: `bundle-manifest` del artefacto, `version` del channel-manifest,
`desired/reported_version` del registry y `/api/device` `app_version`. **Drift = comparación
de strings.** El CI de release usa `actions/checkout` con `fetch-depth: 0` para que
`git describe` vea los tags.

---

## CI: release y promote (publican OBJETOS S3, NO `terraform apply`)

- **`release.yml`** (on tag `v*`): gate de tests → build determinista → **firma minisign con
  clave traída en runtime de Secrets Manager vía OIDC** (jamás en git/logs) → upload a
  `releases/<version>/` → `publish_manifest.py` auto-apunta **canary** (sequence monótono +
  `If-Match`) y espeja `desired_version` en el registry → valida el manifiesto contra el
  schema.
- **`promote.yml`** (`workflow_dispatch`, **prod-Environment-gated**): **REPOINT-only** (sin
  rebuild) canary→stable, **rechazando** si el gate de salud de canary no se cumple
  (`fleet-status --require-healthy-version`); o **rollback** de stable a una versión previa ya
  publicada.

Ambos publican objetos S3 con el **rol de deploy OIDC `cam-counter-gha-deploy`** gated por
Environment. **Publicar objetos S3 ≠ `terraform apply` de infra**: son cosas distintas y
legítimas. GitHub Actions CI permanece **plan-only** para infraestructura.

> **Firma:** la clave privada minisign vive **SÓLO** en AWS Secrets Manager (passwordless,
> creada con `minisign -G -W`), nunca se committea. La **pubkey** fijada
> (`agent/keys/cam-counter-release.pub`) sí va en el repo/imagen (es pública). El secreto de
> cámara `RWCHBY` está QUEMADO y **no** aparece en el repo ni en el artefacto.

---

## Apply autónomo del bucket de releases (RUNNER MAD) — F1/F2/F3

El bucket `cam-counter-fleet-releases-950639281773` lo crea
`terraform/modules/fleet-releases/` (privado, BlockPublicAccess all-true, SSE-S3,
BucketOwnerEnforced, política deny TLS-only) con wiring en `terraform/environments/prod`. Lo
**APLICA AUTÓNOMAMENTE el RUNNER MAD** con las credenciales de su entorno (jamás committeadas),
compartiendo el state remoto `environments/prod` (lock DynamoDB de PR02).

- **F1 (state aditivo/monótono):** se aplica **sólo desde la rama apilada más alta** (esta
  rama acumula todo el HCL de PR02–PR04 + el bucket de releases). **Antes** de aplicar se
  inspecciona el `terraform plan` del **ROOT COMPLETO**: debe ser **estrictamente aditivo**
  (sólo el bucket de releases "to add"; **0 to change, 0 to destroy**). Ante **cualquier**
  `destroy`/`replace` de PR02–PR04 → **ABORTA**.
- **F2 (apply autónomo acotado y justificado):** el apply pre-merge se justifica porque el HCL
  proviene de esta pila curada, se restringe al módulo del bucket de releases y el plan se
  inspecciona como aditivo. GitHub Actions CI permanece **plan-only** (cierra la escalada de
  privilegios desde PRs no confiables).
- **F3 (tags):** `default_tags` capitalizados `{Project, ManagedBy=terraform, Env}` + tags
  lógicos en **MINÚSCULA** `project=cam-counter` y `managed_by=mad-runner` (la verificación
  busca la clave en minúscula; `ManagedBy` capitalizado **siempre** vale `terraform`).

**Verificación contra AWS real** (lo ejecuta el runner):

```bash
terraform -chdir=terraform/environments/prod plan -out=tf-prod.plan      # ROOT COMPLETO
terraform -chdir=terraform/environments/prod show tf-prod.plan | grep -E 'will be destroyed|must be replaced'  # F1: vacío
terraform -chdir=terraform/environments/prod apply -auto-approve tf-prod.plan
aws s3api head-bucket             --bucket cam-counter-fleet-releases-950639281773
aws s3api get-public-access-block --bucket cam-counter-fleet-releases-950639281773
aws s3api get-bucket-encryption   --bucket cam-counter-fleet-releases-950639281773
aws s3api get-bucket-policy       --bucket cam-counter-fleet-releases-950639281773
aws s3api get-bucket-tagging      --bucket cam-counter-fleet-releases-950639281773  # incluye managed_by=mad-runner
terraform -chdir=terraform/environments/prod plan -detailed-exitcode                # idempotencia: exit 0
```

**Validación end-to-end contra el bucket REAL** (gated por credenciales; skip si no hay,
PASS real si hay):

```bash
python3 ota/tools/selftest_release_bucket.py --bucket cam-counter-fleet-releases-950639281773 --cleanup
```

Publica un artefacto + manifiesto de PRUEBA bajo prefijos `_selftest`, valida el manifiesto
contra el schema, lo lee de vuelta (incluida una lectura con el **rol per-Pi least-privilege**
para validar el IAM acotado) y **LIMPIA** todo (no toca los canales productivos
`canary`/`stable`).

### Teardown

Bajo costo (S3 + DynamoDB on-demand). Los recursos llevan `project=cam-counter` y
`managed_by=mad-runner` (minúscula) para trazar/limpiar lo que aplicó el runner:

```bash
aws s3 rm s3://cam-counter-fleet-releases-950639281773 --recursive
terraform -chdir=terraform/environments/prod destroy -target=module.fleet_releases
```

---

## Checklist de smoke on-Pi (distinto de CI)

CI verde **nunca** promueve a la flota por sí solo. Antes de promover, en un Pi real:

- `curl -s http://127.0.0.1:8000/api/health` → `status=ok`, `frames_flowing=true`, cada cámara
  con `frames_processed` creciente.
- Cruce manual frente a la cámara → el contador incrementa (`/api/counters`).
- `hailo` presente y `frames>0` (no DummyDetector).
- `systemctl status cam-counter-update.timer` activo (jitter + Persistent).

> **Cutover:** `hailo-personas` sigue ejecutable; el cutover a `cam-counter-edge` lo decide el
> orquestador humano (no este PR). **Rollback de cutover = re-habilitar `hailo-personas`.**
