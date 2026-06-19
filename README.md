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

## 🔁 Reconstruir los binarios ignorados por git

`lib/`, `x64root/` y los `.jar` no están en git (pesados / de terceros). Para regenerarlos:

```bash
# Dependencias del sistema
sudo apt install -y box64 default-jdk-headless qemu-user-static

# 1) Jar del activador (desde rtsp-enable/, con el código en src/)
cd rtsp-enable && ./gradlew --no-daemon jar collectDeps

# 2) SDK nativo Hikvision (lib/): del proyecto base ylemoigne/ezviz-enable-rtsp
#    git clone https://github.com/ylemoigne/ezviz-enable-rtsp  → copiar su carpeta lib/

# 3) Sysroot amd64 + JRE x86-64 (x64root/): ver pasos en docs/HALLAZGOS.md
#    (dpkg-deb -x de libc6/libstdc++6/zlib1g amd64 + JRE Temurin 21 x64)
```

## 🙌 Créditos

Activador de RTSP basado en [`ylemoigne/ezviz-enable-rtsp`](https://github.com/ylemoigne/ezviz-enable-rtsp),
extendido para esta cámara/firmware y para correr bajo box64 en la Pi 5.
Modelos YOLOv8 y runtime de [Hailo](https://hailo.ai/).
