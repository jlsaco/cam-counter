"""`cam_counter` OTA update-agent (pull-based, manifiesto = única fuente).

Reconciliación: lee el canal asignado desde config LOCAL (nunca de la red), descarga el
manifiesto del canal desde S3 vía SigV4 (única fuente de la versión deseada), verifica
sha256 LUEGO firma minisign contra una pubkey fijada, instala atómicamente, hace
health-check de PRODUCTO con ventana de soak y hace rollback seguro a `last_good` ante
cualquier fallo. Offline-tolerante: un solo salto a la versión current del canal.
"""

AGENT_VERSION = "0.1.0"
