# `scripts/` — utilidades del monorepo

Scripts transversales (sin lógica de producto). Hoy:

## `version.py`
Deriva el **string de versión SemVer** del proyecto a partir de **tags git anotados**
(`git describe --tags --always`), única fuente de verdad de la versión. **Degrada limpio**
cuando no hay tags (nunca lanza excepción): produce `0.0.0-dev.<N>+g<sha>`, donde `<N>` es
el nº de commits y `<sha>` el hash corto de HEAD.

```bash
python3 scripts/version.py            # imprime sólo el string de versión
python3 scripts/version.py --json     # {version, git_sha, is_dirty, is_release}
```

El **mismo** string fluye por bundle-manifest, channel-manifest, device-registry y
`/api/device`. Robusto a *shallow clones* (los jobs de release usan
`actions/checkout` con `fetch-depth: 0`). Sin dependencias externas.

## `install_hailo_service.sh`
Instalador **idempotente** de la unit systemd `hailo-personas`. La unit versionada
(`v1/systemd/hailo-personas.service`) usa el placeholder `__CAM_COUNTER_REPO__`; el
instalador resuelve la **ruta real del clon** (vía `git rev-parse --show-toplevel`),
renderiza la unit a `/etc/systemd/system/`, hace `daemon-reload` y `enable --now`. En x86
sin systemd sólo **renderiza** la unit (modo dry-run) sin fallar, útil para CI.

```bash
sudo bash scripts/install_hailo_service.sh         # instalación real en la Pi
NO_SYSTEMCTL=1 UNIT_DEST=/tmp/u.service \
  bash scripts/install_hailo_service.sh            # render dry-run (x86/CI)
```

## `verify_toolchain.sh`
Verifica que la toolchain requerida (python3, ffmpeg, gradle/gradlew, …) está presente.

## `test_version.py`
Test del camino **sin-tag** de `version.py` (crea un repo git temporal, 1 commit, sin tag).

```bash
python3 -m pytest scripts/test_version.py -q   # o: python3 scripts/test_version.py
```
