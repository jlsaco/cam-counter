# ota — Flota OTA pull-based (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

**Actualización OTA pull-based** de la flota de Pis. El **update-agent** del Pi reconcilia
la versión **deseada** (del manifiesto del canal en S3) vs. la **actual**, descarga el
tarball **firmado (Ed25519 / minisign)**, verifica `sha256` y firma, instala de forma
**atómica**, hace **health-check** del producto y **auto-rollback** si falla.

Reglas (ver `CLAUDE.md`):

- La **única** fuente de la versión deseada es el **manifiesto del canal en S3**
  (`channels/<channel>/manifest.json`); el agente lo lee vía **SigV4** (nunca presigned).
- El agente **NUNCA** lee `desired_version` del device-registry para decidir actualizar.
- Bucket de releases: `cam-counter-fleet-releases-950639281773`.

Contratos relacionados en `contracts/`: `channel_manifest.schema.json`,
`bundle_manifest.schema.json`, `device_registry_item.schema.json`.

> Aquí solo queda el esqueleto; el update-agent llega en PRs posteriores.
