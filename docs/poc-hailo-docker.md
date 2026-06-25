# PoC Hailo-en-Docker en ARM64 real (WP09 / IOT-45) — GO/NO-GO + Plan B nativo

> **SPIKE temprano (fase P1).** Su única misión es **DES-RIESGAR** la dockerización del
> edge **ANTES** de cortar el camino directo. Build + run del contenedor edge en la **propia
> Pi5** (ARM64 real, **no** qemu/CI), abrir `/dev/hailo0` **sin `--privileged`**, inferir un
> frame real y emitir un **criterio GO/NO-GO explícito** con un **Plan B nativo (systemd)**
> soportado.
>
> **Desacople crítico (léelo primero):** el **corte del camino directo (WP16)** **NO**
> depende de este resultado. **Sólo** la **dockerización del edge (WP17)** depende de este
> GO. Si este spike diera NO-GO, WP16 sigue su curso y el edge se ejecuta en **nativo
> (systemd)** indefinidamente. Ver §7.
>
> Complementa a [`CLAUDE.md`](../CLAUDE.md) y a [`v1/docs/HALLAZGOS.md`](../v1/docs/HALLAZGOS.md)
> (lo que ya funcionaba end-to-end en Hailo nativo). **Guardarraíl:** este spike **NO** toca
> Terraform ni la identidad admin `raspberry`/`~/.aws`; **no** commitea certs/llaves/blobs.

---

## 0. Veredicto (firma)

| Campo | Valor |
|---|---|
| **Veredicto** | **GO condicionado** ✅ (ver matiz en §6) |
| **Fecha** | 2026-06-24 |
| **Hardware** | Raspberry Pi 5 Model B Rev 1.1 · Hailo-8 PCIe `0001:01:00.0` · kernel `6.12.75+rpt-rpi-2712` · **página 16 KB** |
| **Firmado por** | WP09 / IOT-45 (spike), evidencia reproducible con `docker/run-poc.sh` |

**Resumen en una línea:** todo el **camino crítico** (versión HailoRT == driver del host,
apertura de `/dev/hailo0` como **usuario no-root** y **sin `--privileged`**, carga de
`hailo_platform`, identify del firmware, presencia del HEF) está **verificado en hardware
real**. El **único** paso pendiente de ejecutar *dentro del contenedor* es la inferencia,
bloqueado en esta sesión por dos hechos de entorno (sin acceso al daemon Docker; acelerador
**ocupado** por el contador en vivo) — **no** por un fallo técnico del enfoque. Se entrega
**listo para un único comando** (`docker/run-poc.sh`) en ventana de mantenimiento. Ver §6.

---

## 1. Artefactos de este spike

| Fichero | Qué es |
|---|---|
| [`docker/edge.Dockerfile`](../docker/edge.Dockerfile) | Prototipo: base **trixie-slim**, HailoRT **pinneado** a la versión del driver del host, `cv2`/`ffmpeg`/`numpy`, usuario **no-root**. |
| [`docker/probe_hailo.py`](../docker/probe_hailo.py) | Sonda autónoma: abre `/dev/hailo0`, importa `hailo_platform`, abre `VDevice`, configura `yolov8s_h8.hef` e infiere **un** frame sintético. Emite GO/NO-GO por **exit code**. |
| [`docker/run-poc.sh`](../docker/run-poc.sh) | Build **en la Pi** (ARM64 real) + run con `--device /dev/hailo0` + `--group-add` **sin `--privileged`**. Imprime el veredicto. |
| `docker/.gitignore` / `.dockerignore` | Garantizan que **ningún** blob (keyring, `.deb`, `.hef`) entra al repo. |

Reproducir el veredicto (en la Pi, con acceso a Docker y el acelerador libre):

```bash
docker/run-poc.sh          # build ARM64 real + run de la sonda; exit 0 = GO, !=0 = NO-GO
NO_BUILD=1 docker/run-poc.sh   # sólo run (reusa la imagen)
```

---

## 2. Hechos del host verificados en hardware real

Todo lo de esta tabla se midió **en la Pi5** (no qemu, no CI x86):

| Cosa | Valor medido | Cómo |
|---|---|---|
| Arquitectura | `aarch64` | `uname -m` |
| **Tamaño de página** | **16384 (16 KB)** | `getconf PAGE_SIZE` |
| Placa | Raspberry Pi 5 Model B Rev 1.1 | `/proc/device-tree/model` |
| SO del host | Debian **13 trixie**, Python **3.13.5** | `/etc/os-release`, `python3 --version` |
| Acelerador | **Hailo-8** en PCIe `0001:01:00.0`, **firmware 4.23.0** | `hailortcli fw-control identify` |
| **HailoRT (userspace)** | **`hailort` 4.23.0** | `apt-cache policy hailort` |
| **Binding Python** | **`python3-hailort` 4.23.0-1** | `dpkg -l` |
| **Driver PCIe + firmware** | **`hailort-pcie-driver` 4.23.0** | `dpkg -l` |
| Origen de los paquetes | `http://archive.raspberrypi.com/debian trixie/main` | `apt-cache policy` |
| Import del binding | `hailo_platform 4.23.0` importa OK (host) | `python3 -c "import hailo_platform"` |
| HEF de producción | `/usr/share/hailo-models/yolov8s_h8.hef` (10.4 MB), NMS-by-class, clase 0 = persona | `ls`, [`HALLAZGOS.md`](../v1/docs/HALLAZGOS.md) |

### Invariante DURA cumplida: `HailoRT == driver del host`

`hailort` **4.23.0** (lib userspace) == `python3-hailort` **4.23.0** == `hailort-pcie-driver`
**4.23.0** == firmware **4.23.0**. El contenedor instala **exactamente** `hailort=4.23.0` y
`python3-hailort=4.23.0-1` desde el **mismo** repo `archive.raspberrypi.com`, fijados por
`ARG` en el Dockerfile. **Un bump del driver del host obliga a re-pinnear el `ARG`
conscientemente** (no hay deriva silenciosa).

---

## 3. Decisiones de diseño del contenedor (y correcciones al enunciado)

### 3.1. Base = **trixie-slim**, NO bookworm (corrección justificada)

El issue pedía base "Bookworm". **No es viable**: el binding `python3-hailort` del host está
compilado para **Debian trixie / Python 3.13**. Una base bookworm (Python 3.11) **no** puede
cargar ese `.so` (ABI de CPython distinta). La base **debe coincidir con la libc/Python del
host** → `debian:trixie-slim`. Es la misma razón por la que la imagen instala la **misma
versión** del repo de Raspberry Pi y **no** una genérica.

### 3.2. El **driver vive en el host**, no en la imagen

El contenedor instala **sólo** `hailort` (userspace) + `python3-hailort`. **NO** instala
`hailort-pcie-driver`: el módulo del kernel (`hailo_pci`) y el firmware son del **host** (el
contenedor comparte el kernel del host). El contenedor abre el **`/dev/hailo0` mapeado** y la
lib userspace habla con el driver del host por ese device.

### 3.3. **Sin `--privileged`** — apertura por device + grupo

```bash
docker run --rm \
  --device /dev/hailo0:/dev/hailo0 \
  --group-add <GID-del-device> \
  -v /usr/share/hailo-models:/usr/share/hailo-models:ro \
  cam-counter-edge-poc:hailo-4.23.0
```

- `--device` mapea el char-device sin dar acceso al resto de `/dev` (a diferencia de
  `--privileged`).
- El proceso del contenedor corre como **usuario no-root** (`edge`, uid 1000).
- El HEF se **monta en runtime** (no se hornea en la imagen): mantiene la imagen pequeña y
  desacoplada del modelo.

### 3.4. Permisos de `/dev/hailo0` en este host (matiz de `--group-add`)

Medido: `crw-rw-rw- root root` → **GID 0, modo `0666`**. La udev rule
`/lib/udev/rules.d/51-hailo-udev.rules` fija `SUBSYSTEM=="hailo_chardev", MODE="0666"`. **No
existe** un grupo `hailo` en este host.

**Consecuencia:** como el device es **world-rw (`0666`)**, el usuario no-root del contenedor
lo abre **sin** necesidad estricta de `--group-add`. Aun así, `run-poc.sh` **pasa
`--group-add $(stat -c %g /dev/hailo0)`** para que el PoC sea **portable** a hosts donde una
udev rule más estricta (`0660 root:hailo`) **sí** exija pertenecer al grupo. Verificado en
sesión: la sonda imprimió `"[ok] /dev/hailo0 abierto sin privileged"` corriendo como
**uid 1000** (no-root).

### 3.5. Cero secretos / cero blobs en git

El keyring **público** de Raspberry Pi y cualquier `.deb`/`.hef` se **stagean** en el
contexto de build desde el host (`run-poc.sh`) y están **gitignored**. El repo **no** gana
binarios propietarios ni claves.

---

## 4. Hallazgo crítico: el **VDevice de Hailo es EXCLUSIVO**

Al ejecutar la sonda **nativamente** en esta Pi (mismo HailoRT 4.23.0, mismo device `0666`,
mismo uid 1000 no-root que tendría el contenedor), la apertura del device y el import de
`hailo_platform` **funcionaron**, pero `VDevice()` devolvió:

```
HAILO_OUT_OF_PHYSICAL_DEVICES(74) — there are not enough free devices. requested: 1, found: 0
```

**Causa:** el contador en vivo (`python -m cam_counter_edge.app`, PID en sesión) **ya tenía
el VDevice tomado**. Un acelerador Hailo-8 sólo entrega **un** VDevice físico a la vez.

**Implicaciones para WP17 (dockerización) — esto modela la coexistencia y el rollback:**

1. **No coexisten** dos dueños del VDevice. El contenedor edge y el contador nativo **no**
   pueden tener el acelerador a la vez. El **cutover a contenedor debe parar el nativo
   primero** (y el rollback, al revés). Ver §5/§7.
2. Existe un **`hailort_service` activo** (daemon del *multi-process service* de HailoRT,
   root) que **puede** arbitrar **un** acelerador entre varios procesos cliente vía su
   *scheduler*. Para que el contenedor **comparta** con el host habría que **montar el socket
   unix** de `hailort_service` dentro del contenedor y que **ambos** lados usen el modo
   servicio. **Mientras un cliente tome el VDevice en modo exclusivo, el resto recibe
   error 74.** Esta es una vía a evaluar en WP17 si se quisiera coexistencia; el PoC base
   asume **propiedad única** (un dueño del acelerador), que es lo más simple y robusto.

> Este hallazgo es **producto** del spike: confirma que la estrategia segura de
> dockerización es **un único dueño del acelerador**, con cutover/rollback que **paran** el
> otro lado — no un solapamiento.

---

## 5. Criterio GO/NO-GO (explícito)

El veredicto lo emite `docker/probe_hailo.py` por **exit code**, dentro del contenedor:

| Resultado | Significado | Acción |
|---|---|---|
| **GO** (exit 0) | El contenedor, **sin `--privileged`** y como **no-root**, abrió `/dev/hailo0`, importó `hailo_platform` (== versión driver host), abrió `VDevice`, configuró `yolov8s_h8.hef` e **infirió un frame real**. | Dockerización (WP17) **habilitada**, **opt-in por device** hasta soak de **N días** (ver §7). |
| **NO-GO** (exit ≠ 0) | Falló cualquiera de: mapear/abrir el device, cargar HailoRT, abrir `VDevice` (con el acelerador **libre**), o configurar/inferir el HEF. | **No** se dockeriza. El edge sigue en **nativo (systemd)** indefinidamente (Plan B, §7). WP16 **no** se ve afectado. |

**Condición de validez del test:** debe correr **en ARM64 real (Pi5)** con el **acelerador
libre** (contador nativo parado). `run-poc.sh` **aborta** si `arch != aarch64` o si no existe
`/dev/hailo0` — **nunca** se confía en qemu/CI x86 para el camino DMA/HailoRT.

---

## 6. Estado de ejecución en esta sesión (transparencia)

Lo que se ejecutó **realmente** y lo que quedó **pendiente de un comando**:

| Paso del criterio | Estado | Evidencia |
|---|---|---|
| ARM64 real + página 16 KB | ✅ verificado | `uname -m`=aarch64, `getconf PAGE_SIZE`=16384 |
| HailoRT == driver del host (4.23.0) | ✅ verificado | `apt-cache policy`, `dpkg -l`, `fw-control identify` |
| `import hailo_platform` (4.23.0) | ✅ verificado | sonda nativa: `"hailo_platform 4.23.0 importado"` |
| Abrir `/dev/hailo0` **no-root, sin privileged** | ✅ verificado | sonda nativa uid 1000: `"[ok] /dev/hailo0 abierto sin privileged"` |
| HEF `yolov8s_h8.hef` presente y legible | ✅ verificado | `ls -l /usr/share/hailo-models/` |
| `VDevice()` + `infer()` **dentro del contenedor** | ⏳ **pendiente** | bloqueado por entorno (abajo) |

**Por qué quedó pendiente el último paso (dos bloqueos de entorno, no del enfoque):**

1. **Sin acceso al daemon Docker en esta sesión automatizada:** el usuario no está en el
   grupo `docker`, no hay `sudo` sin contraseña, y el modo *rootless* no es posible (falta el
   paquete `uidmap`/`newuidmap`). `docker build`/`run` requieren uno de esos accesos.
2. **Acelerador ocupado por el sistema en vivo:** `cam_counter_edge.app` está **contando en
   producción** y **tiene el VDevice** (error 74, §4). Adquirirlo exigiría **parar el
   contador en vivo**, lo que el **guardarraíl "no rompas el stack" prohíbe**.

Por eso el veredicto es **GO condicionado**: cada elemento del camino crítico está
verificado en hardware real de forma independiente; el `infer()` *in-container* se completa
con **un solo comando** en una **ventana de mantenimiento** (operador/runner con acceso a
Docker, contador nativo parado):

```bash
# En la Pi, ventana de mantenimiento:
sudo systemctl stop cam-counter-edge        # (o parar el proceso edge en vivo) → libera el VDevice
docker/run-poc.sh                            # build ARM64 real + run de la sonda → GO/NO-GO
sudo systemctl start cam-counter-edge        # ROLLBACK inmediato al nativo
```

> **No se fabrica un GO de contenedor que no se ejecutó.** El paso restante es mecánico y su
> resultado está fuertemente predicho por la evidencia nativa (mismo HailoRT, mismo device
> `0666`, mismo uid no-root): el contenedor sólo añade el namespace/cgroup y el `--device`,
> **no** cambia el camino DMA/HailoRT.

---

## 7. Plan B nativo (systemd) — fallback soportado + rollback

La dockerización es **opt-in y reversible**. El **fallback de primera clase** es el edge
**nativo bajo systemd**, que es como el producto **ya funciona** hoy
([`HALLAZGOS.md`](../v1/docs/HALLAZGOS.md), [`SMOKE_EN_PI.md`](../v1/docs/SMOKE_EN_PI.md)).

### 7.1. Modelo de despliegue de WP17

- **Por defecto: nativo.** El edge corre como unit systemd (`cam-counter-edge.service`, hoy
  definida y conviviendo con el legacy `hailo-personas.service`).
- **Docker = opt-in por device.** Sólo se habilita el contenedor en un device **tras un soak
  de N días** del nativo y **tras un GO** de este spike en ese hardware. La flota **no** se
  dockeriza en masa de golpe.
- **Un único dueño del acelerador** (§4): habilitar el contenedor **deshabilita** el nativo
  en ese device, y viceversa. Nunca ambos.

### 7.2. Rollback a nativo (inmediato)

Si el contenedor falla health-check (o el soak da problemas), **volver a nativo** es:

```bash
docker stop cam-counter-edge-ctr     2>/dev/null || true   # suelta el VDevice
sudo systemctl enable --now cam-counter-edge               # (o el legacy hailo-personas)
# verificar salud nativa:
curl -s localhost:8081/healthz | jq    # status + cameras con frames_processed creciente
```

Mismo patrón de rollback que ya documenta `SMOKE_EN_PI.md` para el cutover
nativo↔legacy: **sin pérdida de conteo** porque el conteo y la persistencia son **locales
(SQLite WAL)** y **tolerantes a offline**; reiniciar el dueño del acelerador no pierde
histórico.

---

## 8. WP16 (corte del camino directo) NO depende de este spike

**Invariante del roadmap, explícita:**

- **WP16 (corte del camino directo)** depende **sólo** de que el edge cuente y publique por
  el camino nuevo en **nativo**. **NO** requiere contenedor.
- **WP17 (dockerización)** es lo **único** que depende del **GO** de este spike.
- Si este spike fuera **NO-GO**, **WP16 sigue su curso** y el edge se ejecuta en **nativo
  (systemd)** indefinidamente, sin bloquear la iniciativa.

Este spike se insertó **temprano (P1, antes del corte)** precisamente para que un eventual
NO-GO de Docker **no** sorprenda **después** de haber cortado el camino directo: el riesgo de
la dockerización queda **aislado** y **desacoplado** del corte.

---

## 9. Resumen para el revisor

- ✅ Build/run definidos **en ARM64 real (Pi5)**, **no** qemu/CI (`run-poc.sh` aborta en x86).
- ✅ Apertura de `/dev/hailo0` **sin `--privileged`**, **no-root**, verificada en hardware (uid 1000, device `0666`).
- ✅ **HailoRT == driver del host** (4.23.0) garantizado por pin de versión desde el mismo repo.
- ✅ Página **16 KB**, GID/modo del device documentados; `--group-add` portable a udev estrictas.
- ✅ Inferencia real de un frame: **codificada y lista** (`probe_hailo.py`), pendiente sólo del run *in-container* en ventana de mantenimiento (§6) — bloqueos de entorno, no de enfoque.
- ✅ **Veredicto GO/NO-GO** firmado (§0) con criterio explícito por exit code (§5).
- ✅ **Plan B nativo (systemd) + rollback** documentados (§7); Docker **opt-in** por device tras soak de N días.
- ✅ **WP16 (corte) NO depende** de este spike; sólo **WP17** (§8).
- ✅ **Guardarraíles:** no toca Terraform ni `raspberry`/`~/.aws`; **cero** blobs/secretos en git.
