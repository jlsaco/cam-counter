# security: purgar/rotar el secreto de la cámara (RWCHBY) y parametrizar credenciales antes de cualquier gate de gitleaks

# PR00b — Remediación del secreto de la cámara (RWCHBY) y parametrización de credenciales

Eres un agente Claude CLI que trabaja en el repositorio `cam-counter` (clonado en el directorio de trabajo actual). Este prompt es TOTALMENTE AUTOCONTENIDO: no tienes más contexto que el aquí escrito. Trabaja solo con las herramientas disponibles (bash, edición de ficheros, git, gh). NO inventes información del repo; verifica leyendo los ficheros.

PROHIBIDO ABSOLUTAMENTE: dejar cualquier secreto, contraseña, código de verificación o credencial literal en cualquier fichero versionado del repo. Cero secretos en git.

---

## 1) Contexto

`cam-counter` es un proyecto que corre en una Raspberry Pi 5 con acelerador Hailo-8 y una cámara EZVIZ/Hikvision (modelo DS-2CV2Q21G1-IDW) por RTSP. El borde detecta personas con YOLOv8s y sirve un MJPEG. El repo está en GitHub como `jlsaco/cam-counter`, rama por defecto `main`.

El proyecto va a evolucionar hacia un producto de conteo de personas con flota de Pis actualizable por OTA. Como parte de esa evolución, se introducirá CI en GitHub Actions, incluyendo un gate de detección de secretos con `gitleaks`. PERO antes de activar ese gate hay que limpiar el repo de un secreto que ya está committeado.

HECHO VERIFICADO (compruébalo tú mismo con `grep -rn 'RWCHBY' . --include='*.sh' --include='*.md' --include='*.py' | grep -v '.git/'`): el código de verificación de la cámara `RWCHBY` (que es a la vez la contraseña del usuario `admin` para el SDK Hikvision y para el stream RTSP) está hardcodeado en estos ficheros VIVOS del working tree:

- `rtsp-enable/start_detection.sh` (línea ~23): dentro de la URL RTSP `rtsp://admin:RWCHBY@$CAM:554/Streaming/Channels/101`.
- `rtsp-enable/enable_rtsp_now.sh` (líneas ~7 comentario y ~16): `CAM_PASS="${4:-RWCHBY}"` (valor por defecto del 4º argumento).
- `rtsp-enable/rtsp_enable_final.sh` (línea ~13): pasa `RWCHBY` como argumento posicional a `enable_rtsp_now.sh`.
- `rtsp-enable/_scripts_exploracion/setup_and_enable.sh` (línea ~10): `CAM_PASS="${4:-RWCHBY}"`.
- `rtsp-enable/_scripts_exploracion/oneshot_enable.sh` (línea ~24): pasa `RWCHBY` como argumento.
- `rtsp-enable/_scripts_exploracion/reboot_watcher.sh` (líneas ~39 y ~47): pasa `RWCHBY` como argumento (dos veces).
- `README.md` (líneas ~100 y ~170): tabla con el código de verificación y un comando `ffprobe` con la URL literal.
- `docs/HALLAZGOS.md` (líneas ~89, ~141, ~194): URLs y credenciales literales en la narrativa técnica.

NOTA: `detection/yolo_personas.py` ya usa el placeholder `CODIGO` (no contiene el literal), así que NO necesita cambios por este motivo; verifícalo igualmente.

CONSIDERACIONES IMPORTANTES sobre la estructura:
- Los scripts de `rtsp-enable/_scripts_exploracion/` son scripts de exploración históricos que usan rutas absolutas tipo `/home/pi/ezviz_rtsp` (NO son los scripts vivos de producción). Aun así contienen el literal y deben limpiarse para que `gitleaks` quede verde.
- Los scripts VIVOS de producción son `start_detection.sh`, `enable_rtsp_now.sh` y `rtsp_enable_final.sh`. `start_detection.sh` deriva sus rutas de la ubicación del propio script (`BASE`, `REPO`) y NO usa rutas absolutas; es el punto de arranque del pipeline (lo invoca el servicio systemd `hailo-personas`).
- El `.gitignore` actual ya ignora `rtsp-enable/CAM_IP`, `rtsp-enable/RTSP_URL`, `rtsp-enable/*.flag`, `rtsp-enable/*.log` y el sysroot/lib pesados. Reutiliza ese patrón para el fichero de credenciales.

DECISIÓN DE PRODUCTO YA TOMADA (no la re-litigues): se trata `RWCHBY` como QUEMADO/inválido. La documentación del proyecto (`docs/HALLAZGOS.md`) ya indica que el factory-reset es la única vía de recuperación del SDK, por lo que de todas formas existirá un código de verificación nuevo. NO tienes que rotar la credencial en la cámara física (no tienes acceso a ella); tu trabajo es PURGAR el literal del repo y PARAMETRIZAR la credencial para que se inyecte desde fuera de git.

---

## 2) Objetivo

Remediar el secreto de la cámara en el repositorio ANTES de que exista cualquier gate de `gitleaks`, de modo que:

1. Ningún fichero versionado del working tree contenga el literal `RWCHBY`.
2. La credencial (`CAM_PASS`) se obtenga en TODOS los scripts desde una variable de entorno o desde un fichero gitignored bajo el subárbol de `rtsp-enable/` (estilo `shared/`), nunca como literal.
3. `README.md` y `docs/HALLAZGOS.md` muestren un placeholder (p.ej. `<CODIGO>`) en lugar del secreto, conservando la narrativa técnica.
4. Se añada un fichero `.gitleaks.toml` con una política documentada, y `gitleaks` corra en VERDE contra el working tree / diff del PR.
5. El pipeline `hailo-personas` siga siendo lanzable: `start_detection.sh` debe resolver `CAM_PASS` y construir la URL RTSP correctamente cuando se le proporciona la credencial.

---

## 3) Alcance y NO-alcance

### En alcance (lo que SÍ haces)
- Parametrizar `CAM_PASS` en TODOS los scripts `.sh` que hoy llevan el literal `RWCHBY` (los 6 listados arriba).
- Introducir un mecanismo único y documentado para resolver `CAM_PASS`: prioridad a la variable de entorno `CAM_PASS`; si no está, leer de un fichero gitignored (propón `rtsp-enable/CAM_PASS` o `rtsp-enable/secrets/CAM_PASS`); si tampoco está, fallar con un mensaje claro (NO usar un literal por defecto).
- Reemplazar el literal en `README.md` y `docs/HALLAZGOS.md` por `<CODIGO>` (o equivalente), manteniendo el sentido técnico de cada frase. Añadir una breve nota de cómo aportar la credencial (env o fichero) y que `RWCHBY` está rotado/inválido.
- Añadir `.gitleaks.toml` con la política de escaneo del working tree/diff (ver Paso 5).
- Actualizar `.gitignore` para ignorar el fichero de credenciales elegido (si añades uno nuevo).
- Documentar explícitamente la estrategia de historia (ver más abajo): por defecto NO reescribir la historia con `git filter-repo` (preserva los SHAs y la promesa de `--follow` del futuro PR01 que moverá el árbol a `v1/`), y en su lugar configurar `gitleaks` para escanear solo working tree / diff, con un allowlist comentado indicando que el secreto histórico está rotado e inválido. Deja esta decisión escrita en el `.gitleaks.toml` y/o en un breve doc.

### Fuera de alcance (lo que NO tocas)
- NO modifiques la LÓGICA del subsistema `rtsp-enable/` (el flujo login SDK Hikvision + PUT servicesSwitch). Solo cambias de dónde viene la credencial, no cómo se usa.
- NO reescribas la historia de git con `git filter-repo` ni `filter-branch` salvo que lo justifiques expresamente; la decisión por defecto es CONSERVAR la historia (rotación documentada + gitleaks sobre working tree). Si por alguna razón fuerte decides reescribir, documéntalo en el cuerpo del PR; pero recuerda que rompería SHAs y la futura migración con historia.
- NO muevas ficheros a `v1/` (eso es trabajo del PR01 posterior).
- NO crees infraestructura AWS, ni Terraform, ni workflows de CI completos (eso es de PRs posteriores). Este PR solo añade el `.gitleaks.toml` como artefacto de política; NO añadas el job de GitHub Actions todavía.
- NO toques `detection/yolo_personas_mt.py` salvo que contenga el literal (verifica; si no lo contiene, no lo edites).
- NO toques `systemd/hailo-personas.service` (sus rutas las gestiona otro PR).
- NO crees ficheros de informe/resumen `.md` extra (CHANGELOG de tarea, etc.). El cuerpo del PR es la entrega.

---

## 4) Restricciones

- Stack del borde: Python y Bash. Mantén compatibilidad con `bash` (los scripts usan `#!/usr/bin/env bash`).
- Edge-first / offline-tolerante: la credencial debe poder vivir en LOCAL en el Pi (fichero bajo `rtsp-enable/`, gitignored) sin depender de la nube.
- Multi-cámara: aunque hoy hay una cámara, no introduzcas suposiciones que rompan futuras multi-cámara; mantén el cambio mínimo y centrado solo en sacar el secreto.
- Cero secretos en el repo: nada de claves de larga vida ni contraseñas en ficheros versionados. Las credenciales se inyectan por env o por fichero gitignored (en el futuro vendrán de SSM/Secrets Manager, pero eso es otro PR).
- Convenciones AWS (solo informativas para este PR, NO crees recursos): cuenta `950639281773`, región `us-east-1`, prefijo `cam-counter-`.
- El mecanismo de resolución de `CAM_PASS` debe FALLAR de forma explícita (exit no-cero + mensaje) si la credencial no está disponible, en lugar de caer a un literal por defecto.

---

## 5) Rama base y rama nueva

- Rama base (de la que partes y contra la que abrirás el PR): `feat/00-bootstrap` (la rama del PR anterior, PR00). Este PR está APILADO sobre ella.
- Rama nueva a crear: `feat/00b-secret-remediation`.

Procedimiento de ramas:
```
# Asegúrate de tener la rama base local actualizada
git fetch origin
# Si feat/00-bootstrap existe en remoto, básate en ella; si NO existe aún (PR00 sin pushear),
# usa origin/main como fallback y DOCUMÉNTALO en el cuerpo del PR.
git checkout feat/00-bootstrap 2>/dev/null || git checkout -b feat/00-bootstrap origin/feat/00-bootstrap 2>/dev/null || { echo "AVISO: feat/00-bootstrap no existe; basando en main"; git checkout main; }
git checkout -b feat/00b-secret-remediation
```
Determina al inicio si `feat/00-bootstrap` existe (local o remoto) y deja constancia de la base real usada en el cuerpo del PR.

---

## 6) Pasos sugeridos

1. Verifica el estado inicial: `grep -rn 'RWCHBY' . --include='*.sh' --include='*.md' --include='*.py' | grep -v '.git/'`. Anota cada ocurrencia.
2. Diseña el helper de resolución de credencial. Opción recomendada: una función/snippet común que (a) use `$CAM_PASS` si está exportada; (b) si no, lea la primera línea de `rtsp-enable/CAM_PASS` (gitignored) si existe; (c) si tampoco, imprima un error claro a stderr y salga con código no-cero. Puedes implementarlo inline en cada script o en un pequeño `rtsp-enable/_lib_credentials.sh` que los demás hagan `source`. Mantén el cambio simple y consistente.
3. Edita `rtsp-enable/enable_rtsp_now.sh`: elimina el `:-RWCHBY` del default; resuelve `CAM_PASS` por env/fichero; si llega vacío, falla. Actualiza el comentario de uso (línea ~7) para no mostrar el literal.
4. Edita `rtsp-enable/start_detection.sh`: deja de incrustar `RWCHBY` en la URL; resuelve `CAM_PASS` y construye `rtsp://admin:${CAM_PASS}@$CAM:554/Streaming/Channels/101`. Si `CAM_PASS` no está, falla con mensaje claro antes del `exec`.
5. Edita `rtsp-enable/rtsp_enable_final.sh`: no pases el literal; deja que `enable_rtsp_now.sh` resuelva `CAM_PASS` (no le pases el 4º argumento, o pásale `"$CAM_PASS"` ya resuelto). Asegúrate de que la cadena de llamadas sigue funcionando.
6. Edita los 3 scripts de `rtsp-enable/_scripts_exploracion/` (`setup_and_enable.sh`, `oneshot_enable.sh`, `reboot_watcher.sh`): quita todos los literales `RWCHBY`. Como son scripts históricos/exploración, basta con sustituir el literal por `"$CAM_PASS"` (resuelto por env/fichero) o por el mismo mecanismo; no necesitas refactor profundo, pero NO puede quedar el literal.
7. Edita `README.md`: en la tabla (línea ~100) sustituye `RWCHBY` por `<CODIGO>` y aclara que es la contraseña real de la pegatina (no la pongas). En el `ffprobe` (línea ~170) usa `<CODIGO>` o, mejor, indica leer de la variable/fichero `CAM_PASS`. Añade una nota corta: "El código de verificación `RWCHBY` mostrado en el historial está ROTADO/INVÁLIDO; aporta la credencial real vía `export CAM_PASS=...` o el fichero gitignored `rtsp-enable/CAM_PASS`."
8. Edita `docs/HALLAZGOS.md` (líneas ~89, ~141, ~194): sustituye el literal por `<CODIGO>` conservando la narrativa técnica.
9. Crea el fichero de credencial gitignored (NO lo versiones) solo si quieres probar localmente; añade su ruta a `.gitignore` (p.ej. `rtsp-enable/CAM_PASS` o `rtsp-enable/secrets/`). NUNCA lo añadas a git.
10. Crea `.gitleaks.toml` en la raíz con: configuración para escanear sin historia (modo working tree / diff), y un `[[rules]]`/`[allowlist]` comentado que documente que la historia anterior a la rotación contiene `RWCHBY` ya invalidado. La intención es que `gitleaks detect --no-git --source .` y/o `gitleaks protect --staged` salgan en VERDE contra el árbol actual. Comenta en el toml la decisión de NO reescribir historia.
11. Re-ejecuta el grep de verificación: NO debe quedar ninguna ocurrencia de `RWCHBY` en ficheros versionados.
12. Verifica sintaxis bash de todos los scripts tocados con `bash -n`.
13. Prueba funcionalmente `start_detection.sh` en modo aislado: simula que `CAM_PASS` está disponible y comprueba que se construye la URL (sin ejecutar la cámara real). Por ejemplo, exporta `CAM_PASS=PRUEBA123`, y verifica que el script falla limpio SIN `CAM_PASS` y resuelve la URL CON `CAM_PASS`. Como `start_detection.sh` hace tareas de red al inicio, valida la lógica de resolución de credencial de forma acotada (extrae/ejercita la parte de resolución, o usa `bash -n` + inspección, o un dry-run controlado). Lo crítico es demostrar: sin `CAM_PASS` -> falla con mensaje; con `CAM_PASS` -> URL con la credencial inyectada.
14. Ejecuta `gitleaks` (ver Verificación). Si `gitleaks` no está instalado en el entorno, instálalo o usa el contenedor oficial; deja documentado el comando exacto que valida verde.

---

## 7) Definition of Done (objetivo y verificable)

1. `grep -rn 'RWCHBY' . --include='*.sh' --include='*.md' --include='*.py' | grep -v '.git/'` NO devuelve ninguna línea (working tree limpio del literal).
2. En cada script tocado, `CAM_PASS` se resuelve desde env o desde el fichero gitignored, y NO hay ningún valor por defecto literal; si falta la credencial, el script falla con mensaje claro y exit no-cero.
3. `README.md` y `docs/HALLAZGOS.md` muestran un placeholder (`<CODIGO>`), no el literal, y conservan la narrativa técnica; incluyen la nota de que `RWCHBY` está rotado/inválido y cómo aportar la credencial.
4. Existe `.gitleaks.toml` en la raíz con política documentada (escaneo de working tree/diff + allowlist comentado sobre la historia con secreto rotado).
5. `gitleaks` corre en VERDE contra el working tree / diff del PR (documenta el comando exacto).
6. `bash -n` pasa en todos los scripts tocados.
7. El pipeline `hailo-personas` sigue siendo lanzable: con `CAM_PASS` provisto, `start_detection.sh` construye la URL RTSP correcta; sin él, falla limpio.
8. `.gitignore` ignora el fichero de credenciales; ese fichero NO está en el índice de git (`git ls-files` no lo lista).
9. La decisión de historia (conservar historia + gitleaks sobre working tree, sin `filter-repo`) está documentada en el `.gitleaks.toml` y/o en el cuerpo del PR.

---

## 8) Verificación (comandos concretos)

Ejecuta y muestra la salida de cada uno:

```bash
# 1) No queda el literal en ningún fichero versionado relevante
grep -rn 'RWCHBY' . --include='*.sh' --include='*.md' --include='*.py' | grep -v '.git/' | grep . \
  && { echo 'SECRET STILL PRESENT'; exit 1; } || echo 'OK: sin literal RWCHBY'

# 2) start_detection.sh parametriza la credencial
bash -n rtsp-enable/start_detection.sh && grep -q 'CAM_PASS' rtsp-enable/start_detection.sh && echo 'OK start_detection'

# 3) Sintaxis de todos los scripts tocados
for f in rtsp-enable/start_detection.sh rtsp-enable/enable_rtsp_now.sh rtsp-enable/rtsp_enable_final.sh \
         rtsp-enable/_scripts_exploracion/setup_and_enable.sh rtsp-enable/_scripts_exploracion/oneshot_enable.sh \
         rtsp-enable/_scripts_exploracion/reboot_watcher.sh; do bash -n "$f" && echo "OK syntax $f"; done

# 4) Resolución de credencial: sin CAM_PASS falla, con CAM_PASS resuelve (ajusta al mecanismo que implementes)
( unset CAM_PASS; bash -n rtsp-enable/enable_rtsp_now.sh )  # debe pasar sintaxis
# Demuestra el fallo limpio sin credencial y el éxito con CAM_PASS=PRUEBA123 en la parte de resolución.

# 5) gitleaks en verde (working tree / diff). Usa el binario si está, o el contenedor oficial:
gitleaks detect --no-git --source . --config .gitleaks.toml \
  || gitleaks protect --staged --config .gitleaks.toml
# Si no hay binario local:
# docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect --no-git --source /repo --config /repo/.gitleaks.toml

# 6) Existe el fichero de política
test -f .gitleaks.toml && echo 'OK .gitleaks.toml'

# 7) El fichero de credencial gitignored NO está versionado
git ls-files | grep -E 'rtsp-enable/(secrets/|CAM_PASS$)' && { echo 'ERROR: credencial versionada'; exit 1; } || echo 'OK: credencial no versionada'
```

Todos deben pasar. Si `gitleaks` reporta hallazgos, ajústalos vía `.gitleaks.toml` (allowlist documentado) o corrige el fichero, hasta verde.

---

## 9) Entrega: abrir el Pull Request

1. Haz commit de los cambios con un mensaje claro en español. Termina el mensaje de commit con:
   ```
   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   ```
2. Empuja la rama: `git push -u origin feat/00b-secret-remediation`.
3. Abre el PR con `gh pr create`, con `--base feat/00-bootstrap` (la rama del PR anterior; si esa rama no existe en remoto, usa `--base main` y EXPLÍCALO en el cuerpo). Título sugerido:
   `security: purga/rotación del secreto de cámara (RWCHBY) + parametrización de credenciales`
4. El cuerpo del PR (en español) debe incluir, de forma clara:
   - QUÉ: se purga el literal `RWCHBY` del working tree, se parametriza `CAM_PASS` (env o fichero gitignored), se añade `.gitleaks.toml`.
   - POR QUÉ: el secreto estaba committeado y bloquearía permanentemente el futuro gate de `gitleaks`; se trata como QUEMADO/inválido (factory-reset es la única recuperación del SDK).
   - DECISIÓN DE HISTORIA: se CONSERVA la historia (no `filter-repo`) para preservar SHAs y la futura migración con `--follow`; `gitleaks` escanea solo working tree / diff con allowlist documentado del secreto histórico rotado.
   - CÓMO VERIFICAR: pega los comandos de la sección 8 y su salida en verde.
   - Base real usada (`feat/00-bootstrap` o fallback a `main`).
   - Termina el cuerpo con:
     ```
     🤖 Generated with [Claude Code](https://claude.com/claude-code)
     ```
5. DEVUELVE como salida final la URL del PR creado (la imprime `gh pr create`). Esa URL es el resultado entregable de esta tarea.

Recuerda: NO debe quedar ningún secreto literal en ningún fichero versionado. Verifícalo una última vez antes de hacer push.
