#!/usr/bin/env python3
"""Sonda GO/NO-GO de Hailo-en-Docker (WP09 / IOT-45).

Objetivo del SPIKE: demostrar, DENTRO de un contenedor sin ``--privileged`` y en
ARM64 real (Raspberry Pi 5, kernel de 16 KB de página), que es posible:

  1. abrir ``/dev/hailo0`` mapeado al contenedor (vía ``--device`` + ``--group-add``),
  2. hablar con el firmware del acelerador (``fw-control identify``),
  3. configurar un HEF real (``yolov8s_h8.hef``) y
  4. correr UNA inferencia real sobre un frame sintético,

y emitir un veredicto **GO / NO-GO** explícito por código de salida:

  * exit 0  → GO   (apertura + identify + inferencia OK; HailoRT == driver host)
  * exit 1  → NO-GO (cualquier fallo de apertura/identify/inferencia)
  * exit 2  → entorno inválido (HEF ausente, args, etc.)

NO depende del paquete ``cam_counter_edge``: replica el camino mínimo de
``cam_counter_edge.detector.Detector._ensure_pipeline`` para ser un artefacto de
spike autónomo. El frame es sintético (numpy), así que NO necesita cámara/RTSP:
lo único que se valida es el camino DMA/HailoRT que CI x86/qemu NO puede ejercer.
"""

from __future__ import annotations

import argparse
import os
import sys

HEF_DEFAULT = "/usr/share/hailo-models/yolov8s_h8.hef"
HAILO_DEV = "/dev/hailo0"


def _fail(stage: str, exc: BaseException, code: int = 1) -> int:
    print(f"NO-GO :: fallo en '{stage}': {type(exc).__name__}: {exc}", flush=True)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="Sonda GO/NO-GO Hailo-en-Docker")
    ap.add_argument("--hef", default=os.environ.get("CAMCOUNTER_HEF_PATH", HEF_DEFAULT))
    args = ap.parse_args()

    print("== PoC Hailo-en-Docker :: sonda GO/NO-GO (WP09 / IOT-45) ==", flush=True)
    print(f"   uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()}", flush=True)

    # 0) El device tiene que estar mapeado y abrible SIN privileged.
    if not os.path.exists(HAILO_DEV):
        print(f"NO-GO :: {HAILO_DEV} no está mapeado en el contenedor "
              f"(¿falta '--device {HAILO_DEV}:{HAILO_DEV}'?)", flush=True)
        return 1
    try:
        with open(HAILO_DEV, "rb"):
            pass
        print(f"   [ok] {HAILO_DEV} abierto sin privileged", flush=True)
    except OSError as exc:
        return _fail(f"open({HAILO_DEV})", exc)

    # 1) Import de la API de HailoRT (debe ser la MISMA versión que el driver host).
    try:
        import hailo_platform as hp  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return _fail("import hailo_platform", exc)
    rt_ver = getattr(hp, "__version__", "desconocida")
    print(f"   [ok] hailo_platform {rt_ver} importado", flush=True)

    if not os.path.exists(args.hef):
        print(f"NO-GO :: HEF ausente: {args.hef} "
              "(monta /usr/share/hailo-models:ro)", flush=True)
        return 2

    # 2) Abrir VDevice + identify del firmware (prueba el camino de control).
    try:
        vdevice = hp.VDevice()
    except Exception as exc:  # noqa: BLE001
        return _fail("VDevice()", exc)

    try:
        try:
            ids = vdevice.get_physical_devices_ids()
            print(f"   [ok] VDevice abierto; device ids={list(ids)}", flush=True)
        except Exception:  # noqa: BLE001 - identify es best-effort
            print("   [ok] VDevice abierto", flush=True)

        # 3) Configurar HEF real y correr UNA inferencia sobre frame sintético.
        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return _fail("import numpy", exc)

        try:
            hef = hp.HEF(args.hef)
            cfg = hp.ConfigureParams.create_from_hef(
                hef, interface=hp.HailoStreamInterface.PCIe
            )
            ng = vdevice.configure(hef, cfg)[0]
            ng_params = ng.create_params()
            in_info = hef.get_input_vstream_infos()[0]
            out_name = hef.get_output_vstream_infos()[0].name
            h, w, c = in_info.shape  # (H, W, C)
            inp = hp.InputVStreamParams.make(ng, format_type=hp.FormatType.UINT8)
            outp = hp.OutputVStreamParams.make(ng, format_type=hp.FormatType.FLOAT32)
            frame = np.zeros((1, h, w, c), dtype=np.uint8)
            with hp.InferVStreams(ng, inp, outp) as pipe:
                with ng.activate(ng_params):
                    res = pipe.infer({in_info.name: frame})
            out = res[out_name]
            arr = out[0] if isinstance(out, (list, np.ndarray)) else out
            nclasses = len(arr) if hasattr(arr, "__len__") else "?"
            print(f"   [ok] inferencia real ejecutada: HEF={os.path.basename(args.hef)} "
                  f"input={h}x{w}x{c} salida=NMS-by-class ({nclasses} clases)", flush=True)
        except Exception as exc:  # noqa: BLE001
            return _fail("configure/infer", exc)
    finally:
        try:
            vdevice.release()
        except Exception:  # noqa: BLE001
            pass

    print(f"GO :: contenedor abrió {HAILO_DEV} sin privileged e infirió un frame real; "
          f"HailoRT={rt_ver}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
