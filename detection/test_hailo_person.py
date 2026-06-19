#!/usr/bin/env python3
"""Valida la detección de personas con YOLOv8 en el Hailo sobre una imagen estática."""
import sys, numpy as np, cv2
from hailo_platform import (HEF, VDevice, HailoStreamInterface, InferVStreams,
                            ConfigureParams, InputVStreamParams, OutputVStreamParams, FormatType)

HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"
IMG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/people.jpg"
PERSON_ID = 0

hef = HEF(HEF_PATH)
with VDevice() as target:
    cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
    ng = target.configure(hef, cfg)[0]
    ng_params = ng.create_params()
    in_info = hef.get_input_vstream_infos()[0]
    out_info = hef.get_output_vstream_infos()[0]
    H, W, _ = in_info.shape
    print(f"Modelo: in={in_info.name}{in_info.shape}  out={out_info.name}")

    in_params = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
    out_params = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)

    img = cv2.imread(IMG)
    h0, w0 = img.shape[:2]
    inp = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (W, H))

    with InferVStreams(ng, in_params, out_params) as pipe, ng.activate(ng_params):
        res = pipe.infer({in_info.name: np.expand_dims(inp, 0)})

    out = res[out_info.name]
    print(f"Tipo salida: {type(out)}")
    arr = out[0] if isinstance(out, (list, np.ndarray)) else out
    print(f"Clases: {len(arr)}  (esperado 80)")
    # NMS BY CLASS: arr[class_id] = array (N,5) [ymin,xmin,ymax,xmax,score] normalizado
    persons = arr[PERSON_ID]
    persons = np.array(persons) if len(persons) else np.empty((0, 5))
    print(f"\n--- Personas detectadas (clase 0): {len(persons)} ---")
    n = 0
    for d in persons:
        ymin, xmin, ymax, xmax, score = d[:5]
        if score < 0.4:
            continue
        n += 1
        print(f"  persona #{n}: score={score:.2f}  bbox=({int(xmin*w0)},{int(ymin*h0)})-({int(xmax*w0)},{int(ymax*h0)})")
        cv2.rectangle(img, (int(xmin*w0), int(ymin*h0)), (int(xmax*w0), int(ymax*h0)), (0,255,0), 3)
    cv2.imwrite("/home/pi/deteccion_resultado.jpg", img)
    print(f"\n✅ {n} personas con score>=0.4. Imagen anotada: /home/pi/deteccion_resultado.jpg")
