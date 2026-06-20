# v1/edge — Pipeline de conteo edge (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

Paquete Python del **conteo de personas en el borde**. Pipeline previsto:

```
captura → detect (Hailo) → track → count → present + clip + sink
```

- `count` = detección de **cruce de línea** con **histéresis** (banda muerta anti-rebote)
  e **idempotencia por track** (un mismo `track_id` no recuenta el mismo cruce);
  `crossing_seq` es un contador **monótono persistido por cámara**.
- Un **`DummyDetector`** permite ejercitar toda la lógica de conteo en x86 sin Hailo ni
  cámara, de modo que la verificación corre en CI x86.
- `present` emite **MJPEG**; `clip` graba el recorte del evento; `sink` persiste en
  **SQLite (modo WAL)**. **Edge-first / tolerante a offline**: cuenta y persiste en local
  aunque no haya internet.

El detector histórico Hailo/YOLO vive en `v1/detection`. Los contratos en `contracts/`.

> Aquí solo queda el esqueleto; el counter llega en PRs posteriores.
