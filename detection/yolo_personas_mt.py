#!/usr/bin/env python3
"""
Detección de PERSONAS multi-hilo (aprovecha varios núcleos del Pi 5).
Pipeline en 3 etapas paralelas conectadas por colas:
  [Hilo captura/decode] -> [Hilo inferencia Hailo + anotado] -> [Hilo encode/MJPEG]
Cada etapa corre en su propio núcleo, así el throughput lo marca la etapa más lenta
(no la suma), y la latencia baja. Descarta frames viejos para ir siempre "en vivo".
"""
import sys, time, threading, queue, os
from http.server import BaseHTTPRequestHandler, HTTPServer
import numpy as np, cv2
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
from hailo_platform import (HEF, VDevice, HailoStreamInterface, InferVStreams,
                            ConfigureParams, InputVStreamParams, OutputVStreamParams, FormatType)

HEF_PATH="/usr/share/hailo-models/yolov8s_h8.hef"; PERSON_ID=0; CONF=0.45; HTTP_PORT=8080
if len(sys.argv)<2: sys.exit("uso: yolo_personas_mt.py <url_rtsp>")
URL=sys.argv[1]

frames_in   = queue.Queue(maxsize=2)   # captura -> inferencia
frames_out  = queue.Queue(maxsize=2)   # inferencia -> encode
_jpeg=[None]; _lock=threading.Lock()
stats={"cap":0,"inf":0,"out":0,"lat":0.0}
stop=threading.Event()

# ---------- Hilo 1: captura + decode ----------
def capture():
    cap=cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # mínimo buffer -> menor latencia
    if not cap.isOpened(): print("[!] no abre RTSP"); stop.set(); return
    print("[i] RTSP abierto (captura en hilo propio)")
    lost=0
    while not stop.is_set():
        ok, frame = cap.read()
        if not ok:
            lost+=1
            if lost>50: print("[!] stream perdido, salgo"); stop.set(); break
            time.sleep(0.1); continue
        lost=0; stats["cap"]+=1
        ts=time.time()
        # descartar el frame viejo si la cola está llena (ir en vivo)
        if frames_in.full():
            try: frames_in.get_nowait()
            except queue.Empty: pass
        try: frames_in.put_nowait((frame, ts))
        except queue.Full: pass
    cap.release()

# ---------- Hilo 2: inferencia Hailo + anotado (hilo principal) ----------
def infer_loop():
    hef=HEF(HEF_PATH)
    with VDevice() as target:
        cfg=ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
        ng=target.configure(hef,cfg)[0]; ng_params=ng.create_params()
        in_info=hef.get_input_vstream_infos()[0]; out_name=hef.get_output_vstream_infos()[0].name
        H,W,_=in_info.shape
        inp=InputVStreamParams.make(ng, format_type=FormatType.UINT8)
        outp=OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
        with InferVStreams(ng, inp, outp) as pipe, ng.activate(ng_params):
            fps_t=time.time(); n=0
            while not stop.is_set():
                try: frame, ts = frames_in.get(timeout=1)
                except queue.Empty: continue
                h0,w0=frame.shape[:2]
                x=cv2.resize(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB),(W,H))
                res=pipe.infer({in_info.name: np.expand_dims(x,0)})
                out=res[out_name]; arr=out[0] if isinstance(out,(list,np.ndarray)) else out
                dets=np.array(arr[PERSON_ID]) if len(arr[PERSON_ID]) else np.empty((0,5))
                count=0
                for d in dets:
                    ymin,xmin,ymax,xmax,sc=d[:5]
                    if sc<CONF: continue
                    count+=1
                    cv2.rectangle(frame,(int(xmin*w0),int(ymin*h0)),(int(xmax*w0),int(ymax*h0)),(0,255,0),2)
                    cv2.putText(frame,f"persona {sc:.2f}",(int(xmin*w0),int(ymin*h0)-6),
                                cv2.FONT_HERSHEY_SIMPLEX,0.6,(0,255,0),2)
                n+=1; stats["inf"]+=1; stats["lat"]=(time.time()-ts)*1000
                fps=n/(time.time()-fps_t+1e-6)
                cv2.putText(frame,f"Personas: {count}   {fps:.1f} FPS   lat {stats['lat']:.0f}ms",
                            (10,30),cv2.FONT_HERSHEY_SIMPLEX,0.9,(0,200,255),2)
                if frames_out.full():
                    try: frames_out.get_nowait()
                    except queue.Empty: pass
                try: frames_out.put_nowait(frame)
                except queue.Full: pass

# ---------- Hilo 3: encode JPEG + servidor MJPEG ----------
def encode_loop():
    while not stop.is_set():
        try: frame=frames_out.get(timeout=1)
        except queue.Empty: continue
        ok,buf=cv2.imencode(".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,80])
        if ok:
            with _lock: _jpeg[0]=buf.tobytes()
            stats["out"]+=1

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type","multipart/x-mixed-replace; boundary=frame"); self.end_headers()
        try:
            while not stop.is_set():
                with _lock: b=_jpeg[0]
                if b:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+b+b"\r\n")
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError): pass

def serve(): HTTPServer(("0.0.0.0",HTTP_PORT),H).serve_forever()

threading.Thread(target=serve,daemon=True).start()
print(f"[i] MJPEG multi-hilo en http://0.0.0.0:{HTTP_PORT}/")
threading.Thread(target=capture,daemon=True).start()
threading.Thread(target=encode_loop,daemon=True).start()
infer_loop()   # bloquea en el hilo principal
