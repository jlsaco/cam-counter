# Detección de personas con Hailo-8 + cámara EZVIZ — Raspberry Pi 5

Sistema de **detección de personas en tiempo real** sobre el vídeo de una cámara
**EZVIZ / Hikvision** (Wi-Fi), procesado en el acelerador de IA **Hailo-8** con **YOLOv8**,
en una **Raspberry Pi 5**. Pensado para funcionar en el **borde, sin internet**.

```
Cámara EZVIZ ──RTSP/HEVC──▶ Raspberry Pi 5 ──▶ Hailo-8 (YOLOv8) ──▶ filtra "persona"
 (192.168.1.x)               decode + pipeline    310 FPS / 6.6 ms      ──▶ MJPEG en :8080
```

## ▶️ Ver la detección en vivo

Con el servicio corriendo, abre en cualquier navegador de la red local:

```
http://<IP-de-la-Pi>:8080/
```

Verás el vídeo con cajas verdes sobre las personas y un overlay `Personas: N   FPS   lat ms`.

## 🧩 Estado actual (funcionando)

| Componente | Detalle |
|---|---|
| Acelerador | Hailo-8 (310 FPS YOLOv8s, ~22 % de uso, ~37 °C) |
| Cámara | EZVIZ/Hikvision DS-2CV2Q21G1-IDW, HEVC 1080p **@15 FPS** (tope de la cámara) |
| Pipeline | Multi-hilo (decode/inferencia/encode en 3 núcleos) · **latencia ~23 ms** |
| Persistencia | Servicio `systemd` (`hailo-personas`), arranca al boot, se auto-repara |

> El límite de **15 FPS lo impone la cámara** (su firmware), no el Pi ni el Hailo
> (que están al ~22-27 %). Hay holgura para **3-4 cámaras** más. Ver `docs/HALLAZGOS.md`.

## 📁 Estructura

```
hailo-ezviz-personas/
├── README.md
├── docs/
│   ├── HALLAZGOS.md          ← bitácora técnica: qué funcionó y por qué (LÉEME)
│   └── images/               ← capturas de la detección en vivo
├── detection/                ← detección de personas (Python + Hailo)
│   ├── yolo_personas_mt.py   ← PRODUCCIÓN: pipeline multi-hilo
│   ├── yolo_personas.py      ← variante de un solo hilo
│   ├── test_hailo_person.py  ← prueba del Hailo sobre una imagen
│   └── profile_pipeline.py   ← perfilador (decode/infer/encode)
├── rtsp-enable/              ← herramienta que activa el RTSP de la cámara EZVIZ
│   ├── start_detection.sh    ← arranque robusto (resuelve cámara + activa RTSP + lanza)
│   ├── rtsp_enable_final.sh  ← activa RTSP (login SDK + PUT servicesSwitch)
│   ├── enable_rtsp_now.sh    ← lanzador del SDK Hikvision bajo box64
│   ├── src/ + build.gradle   ← código Java del activador (modificado; versionado)
│   ├── lib/                  ← SDK nativo Hikvision x86-64  (ignorado en git, ver abajo)
│   └── x64root/              ← sysroot amd64 + JRE para box64 (ignorado en git)
└── systemd/
    └── hailo-personas.service
```

## 🔧 Por qué la activación del RTSP es "tan complicada"

Esta cámara EZVIZ trae el **RTSP desactivado** y **bloquea el ISAPI** local; solo se reactiva
mandando un comando por el **SDK propietario de Hikvision** (puerto 8000). Ese SDK es **x86-64**
y la Pi es **ARM64 con páginas de 16 KB**, así que corre bajo **box64** + un sysroot amd64.
Toda la odisea (10 obstáculos resueltos: box64, OpenSSL, struct de login de 32 bits, formato del
PUT, lockout anti-fuerza-bruta…) está documentada en **`docs/HALLAZGOS.md`**.

Parámetros que finalmente funcionan:
```
EZVIZ_LOGINMODE=0  EZVIZ_HTTPS=0  EZVIZ_BODY_IN_URL=1  EZVIZ_LOGIN_RETRIES=20
PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json
  {"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}
```

## 🛠️ Operación

```bash
# Estado / logs del servicio
sudo systemctl status hailo-personas
sudo journalctl -u hailo-personas -f

# Forzar activación del RTSP (si la cámara lo apagó tras reiniciar)
bash rtsp-enable/rtsp_enable_final.sh

# Reiniciar la detección
sudo systemctl restart hailo-personas
```

La cámara usa **DHCP** (cambió de IP varias veces: .10 → .8); todo la resuelve por su
**MAC `ac:1c:26`**. Recomendado: fijar una **reserva DHCP** en el router para esa MAC.

Credenciales del stream: usuario `admin`, contraseña = **código de verificación** de la
pegatina de la cámara. URL: `rtsp://admin:<codigo>@<IP>:554/Streaming/Channels/101` (HEVC 1080p).

## 🔁 Binarios ignorados por git — backup en S3 (restaurar en otra Raspberry)

`lib/` (SDK Hikvision), `x64root/` (sysroot amd64 + JRE) y los `.jar` no están en git
(pesados / de terceros / regenerables). **Una copia exacta de los que funcionan —
incluido el jar recompilado con los arreglos de este proyecto — está respaldada en S3:**

| | |
|---|---|
| **Bucket** | `cam-counter-rpi-artifacts-950639281773` (privado, región `us-east-1`) |
| **Objeto** | `rtsp-enable/cam-counter-rtsp-binaries.tar.gz` (~71 MB) |
| **Checksum** | `rtsp-enable/cam-counter-rtsp-binaries.tar.gz.sha256` |
| **Cuenta AWS** | `950639281773` |
| Contiene | `lib/` + `x64root/` + todos los `.jar` (activador + dependencias) |

### Restaurar en una Raspberry Pi nueva (camino rápido — recomendado)

```bash
# 0) Dependencias del sistema
sudo apt install -y box64 qemu-user-static ffmpeg python3-opencv
#    (y el stack de Hailo: sudo apt install -y hailo-all)

# 1) Clonar este repositorio
git clone https://github.com/jlsaco/cam-counter.git
cd cam-counter/rtsp-enable

# 2) Descargar y extraer los binarios desde S3 (requiere awscli con credenciales)
aws s3 cp s3://cam-counter-rpi-artifacts-950639281773/rtsp-enable/cam-counter-rtsp-binaries.tar.gz .
# (opcional) verificar integridad:
aws s3 cp s3://cam-counter-rpi-artifacts-950639281773/rtsp-enable/cam-counter-rtsp-binaries.tar.gz.sha256 .
sha256sum -c cam-counter-rtsp-binaries.tar.gz.sha256   # debe decir: OK
tar xzf cam-counter-rtsp-binaries.tar.gz               # crea lib/ x64root/ *.jar

# 3) Instalar el servicio (ajusta las rutas del .service a la nueva ubicación)
sudo cp ../systemd/hailo-personas.service /etc/systemd/system/
sudo sed -i "s|/home/pi/Documents/hailo-ezviz-personas|$(cd .. && pwd)|g" /etc/systemd/system/hailo-personas.service
sudo systemctl daemon-reload && sudo systemctl enable --now hailo-personas
```

> ⚠️ El archivo `systemd/hailo-personas.service` tiene rutas absolutas a
> `/home/pi/Documents/hailo-ezviz-personas`. Si clonas en otra ruta, ajústalas (el `sed`
> de arriba lo hace). Los scripts `*.sh` ya derivan su ubicación solos.

### Reconstruir desde cero (sin el backup S3)

```bash
sudo apt install -y box64 default-jdk-headless qemu-user-static
cd rtsp-enable && ./gradlew --no-daemon jar collectDeps    # jar + deps (desde src/)
# lib/  -> de https://github.com/ylemoigne/ezviz-enable-rtsp (su carpeta lib/)
# x64root/ -> dpkg-deb -x de libc6/libstdc++6/zlib1g:amd64 + JRE Temurin 21 x64
#             (pasos detallados en docs/HALLAZGOS.md)
```

## 🙌 Créditos

Activador de RTSP basado en [`ylemoigne/ezviz-enable-rtsp`](https://github.com/ylemoigne/ezviz-enable-rtsp),
extendido para esta cámara/firmware y para correr bajo box64 en la Pi 5.
Modelos YOLOv8 y runtime de [Hailo](https://hailo.ai/).
