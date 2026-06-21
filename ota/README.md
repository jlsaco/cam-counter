# `ota/` — flota OTA pull-based + update-agent

**Esqueleto; se implementa en PRs posteriores.**

Actualización **OTA pull-based** de la flota de Pis. Un tarball **firmado
(Ed25519 / minisign)** se publica al bucket de releases
(`cam-counter-fleet-releases-950639281773`) con un **manifiesto de versión deseada por
canal** (`channels/<channel>/manifest.json`).

El **update-agent** del Pi reconcilia versión deseada vs. actual, verifica `sha256` y firma,
instala de forma **atómica**, hace **health-check** del producto y **auto-rollback** si
falla.

- La **única** fuente de la versión deseada es el **manifiesto del canal en S3**
  (`contracts/channel_manifest.schema.json`), leído vía **SigV4** (nunca presigned URLs).
- El agente **nunca** lee `desired_version` del device-registry para decidir actualizar
  (ese campo es espejo/observabilidad).

Contratos relevantes: `contracts/channel_manifest.schema.json`,
`contracts/bundle_manifest.schema.json`, `contracts/device_registry_item.schema.json`.
