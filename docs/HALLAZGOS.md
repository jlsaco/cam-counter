# Hailo-8 + cámara EZVIZ/Hikvision en Raspberry Pi 5 — Hallazgos

_Documento de lo que **realmente funcionó** para: (1) poner operativo el módulo Hailo-8 y
(2) llevar el vídeo de una cámara EZVIZ cloud-locked al Hailo para detectar personas con YOLO._

Fecha: 2026-06-19 · Equipo: Raspberry Pi 5 (kernel 6.12 de **16 KB de página**), Debian 13 trixie.

---

## 1. Hardware detectado

| Componente | Detalle |
|---|---|
| Placa | Raspberry Pi 5 Model B |
| Acelerador IA | **Hailo-8** en PCIe `0001:01:00.0` (AI HAT) |
| Cámara | **EZVIZ / Hikvision DS-2CV2Q21G1-IDW** en WiFi, IP `192.168.1.10` |

La cámara se identificó por MAC `ac:1c:26` (**Hangzhou EZVIZ**, marca de consumo de Hikvision)
y por abrir los puertos `80`, `8000` (SDK Hikvision) y `9010` (EZVIZ). RTSP (554) y ONVIF: **cerrados de fábrica**.

---

## 2. Hailo-8 — lo que funcionó ✅

Instalación **limpia y directa**, sin reinicio:

```bash
sudo apt install -y hailo-all          # driver PCIe + HailoRT + modelos + Python API
hailortcli fw-control identify         # verifica: Hailo-8, firmware 4.23.0
```

- El driver `hailo_pci` venía **precompilado** para el kernel actual (no DKMS) → `/dev/hailo0` apareció solo.
- Enlace PCIe: **Gen3 x1 (8 GT/s)** — óptimo, sin tocar `config.txt`.
- Benchmark YOLOv8s: **~310 FPS** (`hailortcli benchmark`).
- **Detección de personas validada** sobre imagen estática (`test_hailo_person.py` con `bus.jpg`):
  detectó **4 personas** (scores 0.93 / 0.89 / 0.87 / 0.43). Salida del modelo = **NMS en chip**
  (`HAILO NMS BY CLASS`, 80 clases COCO, clase 0 = persona; cajas `[ymin,xmin,ymax,xmax,score]` normalizadas).
- Modelos `.hef` listos en `/usr/share/hailo-models/` (yolov8s, yolov6n, pose, seg, caras…).

**Conclusión:** el lado Hailo funciona end-to-end. Script de detección: `/home/pi/yolo_personas.py`
(lee RTSP → Hailo → filtra personas → publica MJPEG en `http://<ip-pi>:8080/`).

---

## 3. Cámara EZVIZ — el reto y lo que funcionó

EZVIZ **desactiva el RTSP por software** (orientación a nube). No se puede por HTTP/ISAPI normal
(puerto 80 devuelve 404; ONVIF no responde). Caminos descartados:

- **Nube EZVIZ Open Platform**: requiere internet → descartado (objetivo = borde sin internet).
- **Flashear firmware Hikvision (TFTP)**: riesgo de brickeo → descartado.
- **App EZVIZ** (LAN Live View → Local Service Settings → Enable RTSP): funciona pero es manual y
  **se desactiva en cada reinicio**.

### Lo que funcionó: activar RTSP **localmente desde la propia Pi** con el SDK nativo Hikvision

Herramienta base: `ylemoigne/ezviz-enable-rtsp` (Java + JNA + SDK Hikvision x86-64). Hace:
`NET_DVR_Login_V40` (puerto 8000) → `NET_DVR_STDXMLConfig` con un PUT ISAPI que enciende el flag RTSP.

El SDK es **x86-64** y la Pi es **ARM64 con kernel de 16 KB de página** → cadena de obstáculos resueltos:

| # | Problema | Causa | Solución que funcionó |
|---|---|---|---|
| 1 | `failed to map segment` con qemu-user | Kernel de **16 KB**; las `.so` x86 alinean a 4 KB. **No** se puede cambiar a kernel de 4 KB porque **el módulo Hailo solo existe para el kernel de 16 KB** | Usar **box64** (cargador ELF propio que maneja el desajuste de páginas) |
| 2 | Conflicto multiarch `libc6:amd64` | libc parcheada de RPi OS (`+rpt1`) incompatible | Construir un **sysroot amd64** extrayendo los `.deb` con `dpkg-deb -x` (sin instalar) |
| 3 | JVM crashea bajo box64 (NPE en MethodHandle) | Dynarec emula memoria débil; el JVM asume orden fuerte (TSO) de x86 | `BOX64_DYNAREC=0` (intérprete puro) — lento (~35 s) pero **estable y correcto** |
| 4 | `STDXMLConfig` error 17 (param) y error 11 aleatorios | **Bug de GC de JNA**: `StringPointer`/`BYTE_ARRAY` no guardan referencia → el GC libera la memoria nativa del puntero | Guardar referencias + `reachabilityFence` + **EpsilonGC** (`-XX:+UseEpsilonGC`) |
| 5 | Login "exitoso" pero userId inválido | El `LONG` de Hikvision es de **32 bits**; el binding lo leía como `NativeLong` de 64 bits → `-1` se leía como `4294967295` | Castear a `int`: `(int) userId.longValue()` |
| 6 | Endpoint equivocado (era para modelo C3X) | — | Sondeo: el correcto es `PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json` |
| 7 | **Login err 9 persistente (la clave)** | La struct `NET_DVR_USER_LOGIN_INFO` estaba **incompleta**: faltaban `byProxyType/byUseUTCTime/byLoginMode/byHttps/iProxyID/byVerifyMode`. El `write()` de JNA escribía el layout mal → memoria de login corrupta → la cámara no responde (err 9). El log interno del SDK (`NET_DVR_SetLogToFile(3,...)`) lo reveló: `PRO_LoginHikDevice fail[err=9]` | **Completar la struct** con los campos reales. Con eso: `byLoginMode=0, byHttps=0` → **login OK (userId=0, lastErr=0)** |
| 8 | Login intermitente tras varios intentos | Cada login hacía `dwTryTimes=4` conexiones → el testing disparó el **lockout anti-fuerza-bruta de Hikvision (~30 min)**, y cada intento durante el lock **reinicia su timer** | `NET_DVR_SetConnectTime(4000, 1)` (1 sola conexión/login) + **silencio total 30+ min** antes de un único intento |

**Herramienta de diagnóstico clave:** `NET_DVR_SetLogToFile(3, "/home/pi/ezviz_rtsp/sdklog", false)` → el SDK escribe en
`sdklog/SdkLog_*.log` la razón EXACTA de cada fallo (fue lo que destrabó todo).

| 9 | **PUT de activación fallaba (err 11)** aunque login y GET funcionaban | El cuerpo JSON iba en `lpInBuffer` (separado de la URL). Esta firmware EZVIZ espera el formato **combinado** del autor original: URL + cuerpo juntos en `lpRequestUrl` (`PUT ...\r\n{json}\r\n`), con `lpInBuffer` vacío | Modo `EZVIZ_BODY_IN_URL=1`. Resultado: `statusCode:1 "OK"` → **rtsp pasó a 1** |
| 10 | Login intermitente bajo box64 | El handshake usa crypto que box64 emula con fallos ocasionales | **Reintentar el login dentro del mismo proceso** (`EZVIZ_LOGIN_RETRIES=20`) hasta acertar, luego el PUT |

## ✅✅✅ SOLUCIÓN FINAL QUE FUNCIONA

**Parámetros mágicos para activar RTSP en esta cámara:**
```
EZVIZ_LOGINMODE=0  EZVIZ_HTTPS=0  EZVIZ_BODY_IN_URL=1  EZVIZ_LOGIN_RETRIES=20
PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json
  body: {"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}
```
Comando: `bash /home/pi/ezviz_rtsp/rtsp_enable_final.sh`

**Stream RTSP de la cámara:** `rtsp://admin:<CODIGO>@<IP>:554/Streaming/Channels/101`  (HEVC/H.265, 1920×1080)
> 🔐 `<CODIGO>` es el **código de verificación** (secreto) de la pegatina; **no** se versiona.
> Apórtalo por `export CAM_PASS='...'` o por el fichero gitignored `rtsp-enable/CAM_PASS`.
> El código que vivió en el historial de git está **ROTADO/INVÁLIDO** (factory-reset = única
> recuperación del SDK), así que se usa siempre una credencial nueva.
> ⚠️ La cámara usa **DHCP** y cambió de IP varias veces (.10 → .8). Por eso todo resuelve la IP por **MAC `ac:1c:26`**.
> Recomendado: fijar una **reserva DHCP** en el router para esta MAC.

**Detección de personas:** RTSP → Hailo YOLOv8 → filtra clase 0 → **stream MJPEG anotado en `http://<IP-Pi>:8080/`**.
Validado en vivo: overlay `Personas: N + FPS`, ~16 FPS de procesamiento sobre 1080p. ✅

## Servicios systemd (persistencia para el borde, sin internet)

- **`hailo-personas.service`** (enabled, arranca al boot): ejecuta `start_detection.sh`, que
  (1) resuelve la cámara por MAC, (2) garantiza RTSP activo (`rtsp_enable_final.sh`), (3) lanza la detección.
  `Restart=always` + el detector **sale si pierde el stream** (cámara reiniciada / IP cambiada) →
  systemd lo reinicia y re-resuelve todo. **Auto-reparable.**
- La cámara apaga el RTSP en cada reinicio; el wrapper lo reactiva solo al reconectar (login funciona
  al primer intento sobre cámara recién arrancada).

## Rendimiento y optimización (hallazgos)

**El "límite de 15 FPS" es de la CÁMARA, no del Pi ni del Hailo.** Medido:
- Stream de la cámara: `r_frame_rate=15/1` → **la cámara solo emite 15 frames/seg**.
- Hailo: 6.66 ms/inferencia (**310 FPS** de capacidad), ocupado solo **~22%**.
- CPU: **~27% usado / 73% libre** (4 núcleos). RAM: 1.6/7.9 GB.
- Temperatura Hailo: 37°C en uso real, **59°C a 100%** de carga forzada (límite seguro ~85°C).

**No se puede subir el FPS de la cámara localmente:** la firmware EZVIZ bloquea el ISAPI
(todos los endpoints `/ISAPI/Streaming/...` y `/ISAPI/System/Video/...` dan err=23); solo
expone `servicesSwitch`. El framerate solo se cambiaría desde la app EZVIZ (si lo permite).

**Decode HW del Pi 5:** existe (`/dev/video19`, `rpi-hevc-dec`, GStreamer `v4l2slh265dec`)
pero el decodificador *stateless* da "Unsupported pixel format" con este stream → no se usó
(no era el cuello de botella real; la cámara lo es).

**Optimización aplicada — pipeline multi-hilo** (`yolo_personas_mt.py`, ya en producción):
decode / inferencia / encode corren en **3 núcleos en paralelo** (antes 1 secuencial).
Resultado: **latencia 50 ms → 23 ms** (2× más reactivo). El FPS sigue en ~15 (tope de la cámara).
Los recursos libres (Hailo 78%, CPU 73%) sirven para **añadir más cámaras** (caben 3-4).

## Comandos útiles
```
sudo systemctl status hailo-personas      # estado
sudo journalctl -u hailo-personas -f      # logs en vivo
bash /home/pi/ezviz_rtsp/rtsp_enable_final.sh   # forzar activación RTSP
# Ver detección: navegador -> http://192.168.1.9:8080/
```

## ⚠️ Lección crítica para repetir esto sin romper la cámara
NO martillear el login. Cada intento fallido cuenta para el lockout anti-fuerza-bruta de Hikvision,
y **box64 inestable al inicio envía credenciales corruptas**. Arrancar SIEMPRE con `BOX64_DYNAREC=0`
y la struct de login completa, y usar 1 conexión por login (`NET_DVR_SetConnectTime(_,1)`).

### Comando/datos confirmados que funcionan

- **Login**: `admin` / `<CODIGO>` (código de verificación) en `192.168.1.10:8000`. ✅ (verificado en "run A")
- **Endpoint y esquema** (confirmado con un GET exitoso que devolvió el estado real):
  ```
  GET  /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json
       → {"servicesSwitch":{"rtsp":0,"upnp":1,"web":1,"hiksdk":1}}
  ```
- **Activar RTSP**:
  ```
  PUT  /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json
       body: {"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}
  ```
- **Lanzador**: `bash /home/pi/ezviz_rtsp/enable_rtsp_now.sh`  (box64 + sysroot + flags estables).

### ⚠️ Lección importante (operacional)

El SDK original **no hacía logout ni cleanup** (`NET_DVR_Logout` / `NET_DVR_Cleanup` comentados).
Probar muchas veces seguidas **saturó las sesiones SDK de la cámara** → empezó a rechazar logins con
**error 9** (`NETWORK_RECV_ERROR`). Se corrigió descomentando logout/cleanup. Si vuelve a pasar:
**reiniciar la cámara** (app EZVIZ o quitar/poner corriente) la recupera al instante.

---

## 4. Estado final y bloqueo persistente

- ✅ Hailo operativo y detectando personas (validado sobre imagen: 4/4 personas).
- ✅ Toolchain de activación RTSP construido y **probado funcionando** a las 00:41 (login OK + GET que
  devolvió el estado real `{"rtsp":0,...}`; endpoint y PUT confirmados).
- ❌ **Login SDK persistentemente roto (error 9 = NETWORK_RECV_ERROR)** desde ~00:46. El servicio SDK
  de la cámara acepta TCP en 8000 pero **no responde al handshake de login**. Comprobado que:
  - sobrevive a **power-cycle físico** de la cámara,
  - sobrevive a **90 min de silencio total** (2 ventanas de 45 min sin tocarla),
  - falla en **primer intento limpio sobre cámara recién reiniciada**.
  → No es un lock temporal ni saturación de sesiones. La hipótesis que encaja: la cámara
  **desactivó/bloqueó su servicio hiksdk de forma persistida en flash** como autoprotección tras
  los logins fallidos iniciales (box64 inestable envió credenciales corruptas). **No reactivable por SDK.**

### Causa raíz del daño (lección)

Los **primeros intentos con box64 dynarec inestable** (antes de fijar `BOX64_DYNAREC=0`) enviaban
credenciales/paquetes corruptos. Hikvision/EZVIZ interpretó esto como ataque y bloqueó el canal SDK.
**Para repetir esto en otra cámara: arrancar YA con `BOX64_DYNAREC=0` y nunca martillear el login.**

### Única recuperación: FACTORY RESET de la cámara

1. Botón RESET de la cámara ~10-15 s (con corriente) hasta que reinicie/indicador.
2. Re-emparejar en la app EZVIZ (scan del QR / código).
3. **Inmediatamente** (cámara limpia, 1 solo intento): `bash /home/pi/ezviz_rtsp/enable_rtsp_now.sh`
   — ahora el activador ya está corregido (LONG 32-bit, logout+cleanup, endpoint correcto) y debe
   funcionar al primer intento como funcionó a las 00:41.

### Al abrir el 554 (objetivo final, ya listo)

```bash
python3 /home/pi/yolo_personas.py "rtsp://admin:<CODIGO>@192.168.1.10:554/H.264"
# abrir http://192.168.1.9:8080/  -> detección de personas en vivo
```

## 5. Archivos clave

| Archivo | Qué es |
|---|---|
| `/home/pi/yolo_personas.py` | Detección de personas: RTSP → Hailo → MJPEG |
| `/home/pi/test_hailo_person.py` | Test de Hailo sobre imagen estática (validación) |
| `/home/pi/ezviz_rtsp/enable_rtsp_now.sh` | Activador RTSP (box64 + SDK Hikvision) |
| `/home/pi/ezviz_rtsp/auto_enable_loop.sh` | Reintento en background hasta activar RTSP |
| `/home/pi/ezviz_rtsp/x64root/` | Sysroot amd64 + JRE x86-64 para box64 |
| `/home/pi/ezviz_rtsp/lib/` | SDK nativo Hikvision (x86-64) |
