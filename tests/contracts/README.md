# `tests/contracts/` — gate de reconciliación de contratos (WP02)

Red de seguridad que ata el **payload MQTT de cruces** y el **desired de la named
shadow `line-config`** a sus contratos canónicos de `contracts/`, **verbatim**, y
**falla cerrado** ante cualquier campo inventado o drift. Es el guard de la
decisión documentada en [`docs/contracts-reconciliation.md`](../../docs/contracts-reconciliation.md).

## Qué se valida

| Contrato | Ejemplo canónico | Es, verbatim, el… |
|---|---|---|
| `crossing_event.schema.json` | `examples/crossing_event/valid/full.json` | payload MQTT de `cam-counter/{device_id}/events/crossing` |
| `line_config.schema.json` | `examples/line_config/valid/full.json` | `desired` de la named shadow `line-config-{camera_id}` |

- **`valid/*.json`** → DEBEN validar. Incluyen un ejemplo *full* (todos los
  campos) y otro *minimal* (sólo `required`).
- **`invalid/*.json`** → DEBEN fallar, **cada uno por su motivo declarado** en
  `INVALID_REASONS` (ver `test_contracts.py`). Cubren los campos **inventados**
  que motivaron este WP (`count_delta`, `line_config_version`,
  `direction_positive`, `version`) y los `required` reales que el contrato exige
  (`track_id`, `crossing_seq`, `camera_id`).
- **`event_id` determinista**: se recomputa `sha1(site|device|camera|track_id|crossing_seq)`
  desde la tupla de identidad de cada ejemplo válido y debe coincidir.

`additionalProperties:false` en ambos contratos es lo que hace que un campo
inventado **falle la validación** en vez de colarse silenciosamente.

## Cómo correrlo

```bash
make validate-contracts          # crea .venv-contracts, instala deps y corre el gate
# o, con jsonschema + pytest ya instalados:
python -m pytest tests/contracts -q
```

En CI lo ejecuta `.github/workflows/contracts.yml` en cada PR que toque
`contracts/`, `tests/contracts/` o el propio workflow.

## Añadir o cambiar ejemplos

1. Si añades un `invalid/*.json`, **declara su motivo** en `INVALID_REASONS`
   (`test_contracts.py`); `test_invalid_reasons_cover_all_invalid_examples`
   falla si te lo dejas.
2. **No** edites los contratos para “arreglar” un ejemplo: por defecto los
   contratos NO se tocan (sin bump de `schema_version`). Si de verdad hace falta
   enriquecerlos, sigue el plan de bump de `docs/contracts-reconciliation.md`.
