#!/usr/bin/env python3
"""hailo_probe.py — probe del SPIKE WP09 (issue #45): abrir /dev/hailo0 e inferir
un frame real DENTRO de un contenedor en una Pi5 (ARM64), SIN --privileged.

Que prueba este probe (criterio del spike):
  1. Que el contenedor ve el nodo de dispositivo /dev/hailo0 (inyectado con
     `--device`, sin --privileged) y lo puede ABRIR.
  2. Que el HailoRT del contenedor es COMPATIBLE con el driver del kernel host:
     `VDevice()` solo abre si runtime y driver casan; si hay desajuste, HailoRT
     lanza un error explicito. Por eso un `VDevice()` exitoso ES la prueba de
     "HailoRT(contenedor) == driver(host)".
  3. Que una INFERENCIA real (YOLOv8s) ejecuta en el chip y devuelve la salida
     NMS-por-clase esperada (80 clases COCO, clase 0 = persona).

Reusa el flujo de inferencia validado en v1/edge/cam_counter_edge/detector.py
(BGR->RGB, resize a la entrada del modelo, InferVStreams, NMS por clase).

Diseno reproducible: si no se aporta imagen (CAMCOUNTER_PROBE_IMAGE), genera un
frame sintetico determinista, de modo que el probe NO depende de ningun binario
commiteado. Para evidencia con personas reales, montar una imagen y apuntarla.

Salida: imprime un bloque JSON con el veredicto y sale 0 (GO) o !=0 (NO-GO).
Config por entorno (canon CAMCOUNTER_*):
  CAMCOUNTER_HEF_PATH      ruta al HEF (default yolov8s_h8.hef)
  CAMCOUNTER_PROBE_IMAGE   ruta a imagen de prueba (vacio => frame sintetico)
  CAMCOUNTER_PROBE_CONF    umbral de confianza para contar personas (default 0.4)
"""
from __future__ import annotations

import json
import os
import sys
import traceback

HEF_PATH = os.environ.get("CAMCOUNTER_HEF_PATH", "/usr/share/hailo-models/yolov8s_h8.hef")
PROBE_IMAGE = os.environ.get("CAMCOUNTER_PROBE_IMAGE", "").strip()
CONF = float(os.environ.get("CAMCOUNTER_PROBE_CONF", "0.4"))
PERSON_ID = 0  # clase COCO 0 = persona


def _device_node_report() -> dict:
    """Inspecciona /dev/hailo0 desde dentro del contenedor (sin abrir HW aun)."""
    path = "/dev/hailo0"
    info: dict = {"path": path, "present": os.path.exists(path)}
    if info["present"]:
        st = os.stat(path)
        info.update(
            {
                "mode_octal": oct(st.st_mode & 0o777),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "readable": os.access(path, os.R_OK),
                "writable": os.access(path, os.W_OK),
            }
        )
    return info


def _load_frame(width: int, height: int):
    """Carga la imagen de prueba o genera un frame sintetico determinista (BGR)."""
    import numpy as np  # noqa: PLC0415

    if PROBE_IMAGE:
        import cv2  # noqa: PLC0415

        img = cv2.imread(PROBE_IMAGE)
        if img is None:
            raise FileNotFoundError(f"No se pudo leer CAMCOUNTER_PROBE_IMAGE={PROBE_IMAGE}")
        return img, "image"
    # Frame sintetico determinista: gradiente diagonal (sin RNG => reproducible).
    yy, xx = np.mgrid[0:height, 0:width]
    chan = ((xx + yy) % 256).astype("uint8")
    frame = np.stack([chan, (chan // 2).astype("uint8"), (255 - chan).astype("uint8")], axis=-1)
    return frame, "synthetic"


def main() -> int:
    result: dict = {
        "spike": "WP09 hailo-in-docker",
        "device_node": _device_node_report(),
        "hailort_runtime": None,
        "vdevice_opened": False,
        "inference_ran": False,
        "num_classes": None,
        "persons_detected": None,
        "frame_source": None,
        "verdict": "NO-GO",
        "error": None,
    }

    try:
        import hailo_platform  # noqa: PLC0415
        from hailo_platform import (  # noqa: PLC0415
            HEF,
            VDevice,
            ConfigureParams,
            HailoStreamInterface,
            InferVStreams,
            InputVStreamParams,
            OutputVStreamParams,
            FormatType,
        )
        import numpy as np  # noqa: PLC0415
        import cv2  # noqa: PLC0415

        result["hailort_runtime"] = getattr(hailo_platform, "__version__", "?")
        result["cv2_version"] = cv2.__version__

        if not os.path.exists(HEF_PATH):
            raise FileNotFoundError(
                f"HEF no encontrado: {HEF_PATH}. Montar /usr/share/hailo-models "
                "del host read-only o ajustar CAMCOUNTER_HEF_PATH."
            )

        hef = HEF(HEF_PATH)
        # VDevice() abre /dev/hailo0 y NEGOCIA con el driver del host: si runtime
        # y driver no casan, esto lanza. Exito => compatibilidad probada.
        with VDevice() as target:
            result["vdevice_opened"] = True
            cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
            ng = target.configure(hef, cfg)[0]
            ng_params = ng.create_params()
            in_info = hef.get_input_vstream_infos()[0]
            out_info = hef.get_output_vstream_infos()[0]
            h, w, _ = in_info.shape
            result["model_input"] = {"name": in_info.name, "shape": list(in_info.shape)}
            result["model_output"] = out_info.name

            frame, src = _load_frame(w, h)
            result["frame_source"] = src
            x = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), (w, h))

            in_params = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
            out_params = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
            with InferVStreams(ng, in_params, out_params) as pipe, ng.activate(ng_params):
                res = pipe.infer({in_info.name: np.expand_dims(x, 0)})

            out = res[out_info.name]
            arr = out[0] if isinstance(out, (list, np.ndarray)) else out
            result["inference_ran"] = True
            result["num_classes"] = len(arr)

            persons = arr[PERSON_ID]
            persons = np.array(persons) if len(persons) else np.empty((0, 5))
            n = int(sum(1 for d in persons if float(d[4]) >= CONF))
            result["persons_detected"] = n

        # GO si: dispositivo presente, VDevice abierto e inferencia con 80 clases.
        if (
            result["device_node"].get("present")
            and result["vdevice_opened"]
            and result["inference_ran"]
            and result["num_classes"] == 80
        ):
            result["verdict"] = "GO"
    except Exception as exc:  # noqa: BLE001 — el probe reporta cualquier fallo como NO-GO
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()

    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
