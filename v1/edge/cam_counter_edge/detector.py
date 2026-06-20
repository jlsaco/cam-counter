"""Detector de personas sobre Hailo-8 (wrapper extraído del pipeline v1).

Extrae el wrapper de Hailo del ``infer_loop`` de
``v1/detection/yolo_personas_mt.py`` (HEF + VDevice + InferVStreams + lectura
de la salida NMS-por-clase) a una clase ``Detector`` testeable.

Propiedades clave:
- **Import perezoso de ``hailo_platform``**: NUNCA se importa a nivel de módulo.
  Sólo se importa dentro de los métodos que abren/usan hardware, de modo que
  ``import cam_counter_edge.detector`` funciona en x86 SIN Hailo (CI).
- **``VDevice`` inyectable**: el constructor NO abre hardware; acepta un
  ``vdevice`` ya creado (o lo abre perezosamente en el primer ``detect``).
- **Función pura ``parse_nms_class``**: traduce la salida NMS de Hailo a
  ``Detection`` (reorden + normalización) y es testeable sin Hailo.

Formato de salida del modelo (confirmado en docs/HALLAZGOS.md): NMS en chip
(``HAILO NMS BY CLASS``, 80 clases COCO, **clase 0 = persona**); cada caja llega
como ``[ymin, xmin, ymax, xmax, score]`` normalizada 0..1. Este detector la
reordena al orden del sistema ``[xmin, ymin, xmax, ymax]``.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from .types import PERSON_CLASS_ID, Detection

if TYPE_CHECKING:  # pragma: no cover - sólo para type-checkers, nunca en runtime
    import numpy as np

# Valores por defecto heredados del pipeline en producción (yolo_personas_mt.py).
HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"
PERSON_ID = PERSON_CLASS_ID  # clase 0 = persona
CONF = 0.45  # umbral de confianza por defecto


def _clamp01(value: float) -> float:
    """Recorta defensivamente un float al rango normalizado [0, 1]."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def parse_nms_class(
    class_dets: Any,
    conf: float = CONF,
    class_id: int = PERSON_ID,
) -> list[Detection]:
    """Convierte la salida NMS de UNA clase en una lista de ``Detection``.

    Función PURA (sólo numpy/Python; sin Hailo) para poder testear el
    reorden/normalización sin hardware.

    Args:
        class_dets: iterable de filas ``[ymin, xmin, ymax, xmax, score]``
            normalizadas 0..1 (el bloque por-clase que devuelve la NMS de Hailo,
            es decir ``arr[class_id]``).
        conf: umbral mínimo de confianza; las filas por debajo se descartan.
        class_id: clase COCO a etiquetar en el ``Detection`` resultante.

    Returns:
        Lista de ``Detection`` con ``bbox_norm = [xmin, ymin, xmax, ymax]`` en
        0..1, en el mismo orden de entrada.
    """
    detections: list[Detection] = []
    if class_dets is None:
        return detections
    for row in class_dets:
        # Orden de Hailo: [ymin, xmin, ymax, xmax, score].
        ymin, xmin, ymax, xmax, score = (float(v) for v in row[:5])
        if score < conf:
            continue
        # Reorden al orden del sistema [xmin, ymin, xmax, ymax] + clamp 0..1.
        bbox_norm = [
            _clamp01(xmin),
            _clamp01(ymin),
            _clamp01(xmax),
            _clamp01(ymax),
        ]
        detections.append(
            Detection(bbox_norm=bbox_norm, class_id=class_id, confidence=score)
        )
    return detections


def _import_hailo() -> Any:
    """Importa ``hailo_platform`` de forma PEREZOSA.

    Se llama sólo desde los métodos que necesitan hardware. En x86/CI (sin
    Hailo) lanza un ``RuntimeError`` claro en lugar de romper el import del
    módulo.
    """
    try:
        import hailo_platform  # noqa: PLC0415  (import perezoso intencional)
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "hailo_platform no está disponible: el Detector real requiere "
            "hardware Hailo (Raspberry Pi). En x86/CI usa DummyDetector."
        ) from exc
    if hailo_platform is None:  # pragma: no cover - import bloqueado en tests
        raise RuntimeError("hailo_platform está deshabilitado en este entorno.")
    return hailo_platform


class Detector:
    """Detector de personas sobre Hailo-8.

    El constructor NO abre hardware ni importa ``hailo_platform``. El pipeline
    Hailo se configura perezosamente en el primer ``detect`` (o vía
    ``with Detector() as d:``). Acepta un ``vdevice`` ya inicializado para
    desacoplar la apertura del hardware (útil multi-cámara: un ``VDevice``
    compartido entre detectores).
    """

    def __init__(
        self,
        hef_path: str = HEF_PATH,
        conf: float = CONF,
        person_id: int = PERSON_ID,
        vdevice: Any = None,
    ) -> None:
        self.hef_path = hef_path
        self.conf = conf
        self.person_id = person_id
        self._vdevice = vdevice  # inyectado; None => se abre perezosamente
        self._stack: ExitStack | None = None
        self._pipe: Any = None
        self._in_name: str | None = None
        self._out_name: str | None = None
        self._in_shape: tuple[int, int, int] | None = None

    def _ensure_pipeline(self) -> None:
        """Configura el pipeline Hailo la primera vez (import perezoso)."""
        if self._pipe is not None:
            return
        hailo = _import_hailo()
        stack = ExitStack()
        hef = hailo.HEF(self.hef_path)
        target = (
            self._vdevice
            if self._vdevice is not None
            else stack.enter_context(hailo.VDevice())
        )
        cfg = hailo.ConfigureParams.create_from_hef(
            hef, interface=hailo.HailoStreamInterface.PCIe
        )
        ng = target.configure(hef, cfg)[0]
        ng_params = ng.create_params()
        in_info = hef.get_input_vstream_infos()[0]
        self._in_name = in_info.name
        self._out_name = hef.get_output_vstream_infos()[0].name
        self._in_shape = in_info.shape  # (H, W, C)
        inp = hailo.InputVStreamParams.make(ng, format_type=hailo.FormatType.UINT8)
        outp = hailo.OutputVStreamParams.make(ng, format_type=hailo.FormatType.FLOAT32)
        self._pipe = stack.enter_context(hailo.InferVStreams(ng, inp, outp))
        stack.enter_context(ng.activate(ng_params))
        self._stack = stack

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        """Infiere personas en un frame BGR y devuelve ``Detection`` normalizados.

        Reproduce el flujo del ``infer_loop`` original: BGR->RGB, resize al
        tamaño de entrada del modelo, inferencia Hailo, toma la clase persona y
        delega el reorden/normalización en ``parse_nms_class``.
        """
        self._ensure_pipeline()
        import cv2  # noqa: PLC0415  (perezoso; sólo en el Pi)
        import numpy as np  # noqa: PLC0415  (perezoso; sólo en el Pi)

        assert self._in_shape is not None  # garantizado por _ensure_pipeline
        height, width, _ = self._in_shape
        x = cv2.resize(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB), (width, height))
        res = self._pipe.infer({self._in_name: np.expand_dims(x, 0)})
        out = res[self._out_name]
        arr = out[0] if isinstance(out, (list, np.ndarray)) else out
        class_dets = arr[self.person_id]
        return parse_nms_class(class_dets, conf=self.conf, class_id=self.person_id)

    def close(self) -> None:
        """Libera los recursos Hailo (VDevice/InferVStreams) si se abrieron."""
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self._pipe = None

    def __enter__(self) -> Detector:
        self._ensure_pipeline()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
