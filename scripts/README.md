# scripts — Utilidades del monorepo

Scripts de apoyo (sin dependencias externas salvo lo indicado). Hoy contiene:

## `version.py`

Deriva la **versión SemVer canónica** del repo a partir de **tags git anotados** vía
`git describe --tags --always` (sin archivo `VERSION` commiteado). La misma cadena fluye
por bundle-manifest, channel-manifest, device-registry y `/api/device`.

- Sin args o `--version`: imprime solo el string de versión.
- `--json`: imprime `{ "version", "git_sha", "is_dirty", "is_release" }`.
- **Degrada limpio sin tags** (hoy no hay tags): produce `0.0.0-dev.<N>+g<sha>` y
  **nunca lanza** excepción. Robusto a shallow clones.

```bash
python3 scripts/version.py            # -> p.ej. 0.0.0-dev.5+g1a2b3c4
python3 scripts/version.py --json     # -> objeto JSON con las 4 claves
```

Test del camino sin-tag: `scripts/test_version.py`
(`python3 -m pytest scripts/test_version.py -q` o `python3 scripts/test_version.py`).

## `install_hailo_service.sh`

Instalador **idempotente** de la unit systemd `hailo-personas`. La unit versionada
(`v1/systemd/hailo-personas.service`) usa el placeholder `__CAM_COUNTER_REPO__`; este
script lo **renderiza** a la ruta real del clon (`git rev-parse --show-toplevel`) y la
instala en `/etc/systemd/system/`, hace `daemon-reload` y `enable --now`. No hay rutas
absolutas hardcodeadas ni `v1/` fijo.

```bash
sudo scripts/install_hailo_service.sh             # instala (Pi con systemd)
DEST=/tmp/unit.service scripts/install_hailo_service.sh   # solo render (sin root/systemd)
```

## `verify_toolchain.sh`

Verificación de la toolchain del entorno (heredado del bootstrap).
