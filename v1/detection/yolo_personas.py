#!/usr/bin/env python3
"""
Detección de PERSONAS en vivo: cámara EZVIZ/Hikvision (RTSP) -> Hailo-8 (YOLOv8s).

Lee el stream RTSP, corre YOLOv8 en el acelerador Hailo, filtra la clase
"person" (id 0 de COCO) y publica el vídeo anotado como stream MJPEG por HTTP,
visible desde cualquier navegador de la red en  http://<ip-de-la-pi>:8080/

Uso:
  python3 yolo_personas.py "rtsp://admin:CODIGO@192.168.1.10:554/H.264"
"""
import sys, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import numpy as np
import cv2
from hailo_platform import (HEF, VDevice, HailoStreamInterface, InferVStreams,
                            ConfigureParams, InputVStreamParams, OutputVStreamParams,
                            FormatType)

HEF_PATH   = "/usr/share/hailo-models/yolov8s_h8.hef"
PERSON_ID  = 0          # COCO: 0 = person
CONF_THR   = 0.45       # umbral de confianza
HTTP_PORT  = 8080

if len(sys.argv) < 2:
    sys.exit("Uso: python3 yolo_personas.py <url_rtsp>")
RTSP_URL = sys.argv[1]
# Forzar TCP en RTSP (más estable que UDP sobre WiFi)
import os
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

# --- frame compartido para el servidor MJPEG ---
_lock = threading.Lock()
_jpeg = [None]

class MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencia logs
        pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        while True:
            with _lock:
                buf = _jpeg[0]
            if buf is not None:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                self.wfile.write(buf)
                self.wfile.write(b"\r\n")
            time.sleep(0.03)

def serve():
    HTTPServer(("0.0.0.0", HTTP_PORT), MJPEGHandler).serve_forever()

def parse_nms(raw):
    """Normaliza la salida 'HAILO NMS BY CLASS' a lista de la clase person."""
    out = raw[0] if isinstance(raw, (list, np.ndarray)) and len(raw) else raw
    # out: lista/array de 80 elementos; cada uno (N,5)=[ymin,xmin,ymax,xmax,score]
    dets = out[PERSON_ID]
    return np.array(dets) if dets is not None and len(dets) else np.empty((0, 5))

def main():
    threading.Thread(target=serve, daemon=True).start()
    print(f"[i] Stream MJPEG en  http://0.0.0.0:{HTTP_PORT}/   (abre desde tu navegador)")

    hef = HEF(HEF_PATH)
    with VDevice() as target:
        cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
        ng = target.configure(hef, cfg)[0]
        ng_params = ng.create_params()
        in_info  = hef.get_input_vstream_infos()[0]
        out_name = hef.get_output_vstream_infos()[0].name
        H, W, _  = in_info.shape
        in_params  = InputVStreamParams.make(ng,  format_type=FormatType.UINT8)
        out_params = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)

        cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            sys.exit("[!] No se pudo abrir el RTSP. ¿RTSP habilitado y código/credenciales correctos?")
        print("[i] RTSP abierto. Procesando en Hailo...")

        with InferVStreams(ng, in_params, out_params) as pipe, ng.activate(ng_params):
            fps_t, n = time.time(), 0
            lost = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    lost += 1
                    if lost > 50:
                        print("[!] Stream perdido (cámara reiniciada o IP cambiada). Saliendo para que systemd re-resuelva.")
                        sys.exit(3)
                    time.sleep(0.2); continue
                lost = 0
                h0, w0 = frame.shape[:2]
                inp = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), (W, H))
                res = pipe.infer({in_info.name: np.expand_dims(inp, 0)})
                people = parse_nms(res[out_name])

                count = 0
                for d in people:
                    ymin, xmin, ymax, xmax, score = d[:5]
                    if score < CONF_THR:
                        continue
                    count += 1
                    p1 = (int(xmin * w0), int(ymin * h0))
                    p2 = (int(xmax * w0), int(ymax * h0))
                    cv2.rectangle(frame, p1, p2, (0, 255, 0), 2)
                    cv2.putText(frame, f"persona {score:.2f}", (p1[0], p1[1]-6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                n += 1
                fps = n / (time.time() - fps_t + 1e-6)
                cv2.putText(frame, f"Personas: {count}   {fps:.1f} FPS",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 255), 2)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with _lock:
                        _jpeg[0] = buf.tobytes()

if __name__ == "__main__":
    main()
