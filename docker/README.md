# `docker/` — PoC Hailo-en-Docker (WP09 / IOT-45)

Artefactos del **spike** que des-riesga la dockerización del edge **antes** de cortar el
camino directo. **No** es la imagen de producción. Doc completa con el veredicto **GO/NO-GO**,
el **Plan B nativo (systemd)** y el desacople de WP16/WP17:
**[`../docs/poc-hailo-docker.md`](../docs/poc-hailo-docker.md)**.

| Fichero | Qué hace |
|---|---|
| `edge.Dockerfile` | Imagen prototipo: base **trixie-slim**, **HailoRT pinneado** a la versión del driver del host (`ARG HAILORT_VERSION`), `cv2`/`ffmpeg`/`numpy`, usuario **no-root**. El driver del kernel vive en el **host**. |
| `probe_hailo.py` | Sonda autónoma: abre `/dev/hailo0`, importa `hailo_platform`, configura `yolov8s_h8.hef` e infiere **un** frame. Veredicto por **exit code** (0 = GO). |
| `run-poc.sh` | Build **en la Pi** (ARM64 real) + run con `--device /dev/hailo0` + `--group-add`, **sin `--privileged`**. |

```bash
# En la Pi5 (ARM64 real), con Docker accesible y el acelerador LIBRE:
docker/run-poc.sh            # build + run → imprime GO/NO-GO
NO_BUILD=1 docker/run-poc.sh # sólo run
```

> **El VDevice de Hailo es exclusivo:** para correr el PoC hay que **liberar** el acelerador
> antes (parar el contador nativo en vivo). Ver §4 y §6 de la doc. **Guardarraíl:** no toca
> Terraform ni `~/.aws`; **cero** blobs/secretos en git (`.gitignore`/`.dockerignore`).
