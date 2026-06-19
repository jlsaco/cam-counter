#!/usr/bin/env python3
"""Perfila el pipeline: decode RTSP vs resize vs inferencia Hailo vs encode JPEG."""
import sys, time, os, numpy as np, cv2
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
from hailo_platform import (HEF, VDevice, HailoStreamInterface, InferVStreams,
                            ConfigureParams, InputVStreamParams, OutputVStreamParams, FormatType)

URL = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 100
HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"

hef = HEF(HEF_PATH)
with VDevice() as target:
    cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
    ng = target.configure(hef, cfg)[0]; ng_params = ng.create_params()
    in_info = hef.get_input_vstream_infos()[0]; out_name = hef.get_output_vstream_infos()[0].name
    H, W, _ = in_info.shape
    inp_p = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
    out_p = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    if not cap.isOpened(): sys.exit("no abre RTSP")

    t = {"read":0.0,"resize":0.0,"infer":0.0,"encode":0.0}
    with InferVStreams(ng, inp_p, out_p) as pipe, ng.activate(ng_params):
        # warmup
        for _ in range(5): cap.read()
        cnt=0; t0=time.time()
        while cnt < N:
            a=time.time(); ok,frame=cap.read(); b=time.time()
            if not ok: continue
            x=cv2.resize(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB),(W,H)); c=time.time()
            pipe.infer({in_info.name: np.expand_dims(x,0)}); d=time.time()
            ok2,_=cv2.imencode(".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,80]); e=time.time()
            t["read"]+=b-a; t["resize"]+=c-b; t["infer"]+=d-c; t["encode"]+=e-d
            cnt+=1
        wall=time.time()-t0
    print(f"\n=== Perfil sobre {cnt} frames ({frame.shape[1]}x{frame.shape[0]}) ===")
    for k in ["read","resize","infer","encode"]:
        ms=t[k]/cnt*1000
        print(f"  {k:8s}: {ms:6.1f} ms/frame   (max {1000/ms:6.1f} FPS si fuera el único)")
    print(f"  {'TOTAL':8s}: {wall/cnt*1000:6.1f} ms/frame   -> {cnt/wall:.1f} FPS reales")
    inf_ms=t['infer']/cnt*1000
    print(f"\n  >> Hailo ocupado {inf_ms:.1f} ms de {wall/cnt*1000:.1f} ms = {100*t['infer']/wall:.0f}% del tiempo")
    print(f"  >> Hailo libre ~{100-100*t['infer']/wall:.0f}% -> margen para {int(wall/t['infer'])-1 if t['infer']>0 else 0}+ cámaras más a esta carga")
