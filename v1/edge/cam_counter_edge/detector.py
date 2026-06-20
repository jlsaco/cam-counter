"""Detector de personas respaldado por Hailo, extraído del ``infer_loop`` histórico.

El runtime de Hailo (``hailo_platform``) es un binario **solo del Pi**: NO existe en PyPI
y NO está instalado en los runners x86 de CI. Por eso se importa de forma **PEREZOSA
(lazy)**, únicamente dentro de los métodos que realmente lo necesitan. Importar este
módulo en x86 SIN ``hailo_platform`` DEBE funcionar, de modo que toda la lógica de conteo
posterior (PR06-PR08) se pueda ejercitar en CI con ``DummyDetector`` y con la función pura
``parse_nms_class``.

El ``Detector`` acepta un ``VDevice`` **inyectado** para no abrir hardware en construcción
(y para no acoplarse a una sola cámara/URL global: un mismo proceso podrá servir N cámaras
más adelante). La construcción NO toca hardware; la apertura del dispositivo se difiere a
``open()`` / al primer ``detect()``.
"""

from __future__ import annotations

import contextlib

from .types import DEFAULT_CONF, PERSON_CLASS_ID, Detection, parse_nms_class

# Ruta por defecto del modelo en el Pi (idéntica al pipeline histórico).
DEFAULT_HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"


class Detector:
    """Wrapper testeable del pipeline de inferencia Hailo (HEF / VDevice / InferVStreams).

    El import de ``hailo_platform`` es perezoso: ocurre solo dentro de ``open()`` (vía
    ``_import_hailo``), nunca a nivel de módulo. ``detect()`` reutiliza la función pura
    ``parse_nms_class`` para el parseo/reorden de la salida NMS-por-clase.
    """

    def __init__(
        self,
        hef_path: str = DEFAULT_HEF_PATH,
        conf: float = DEFAULT_CONF,
        vdevice=None,
        person_class_id: int = PERSON_CLASS_ID,
    ) -> None:
        """Construye el Detector SIN abrir hardware.

        Args:
            hef_path: ruta al modelo ``.hef`` en el Pi.
            conf: umbral de confianza (por defecto 0.45).
            vdevice: ``hailo_platform.VDevice`` ya abierto e **inyectado**. Si es None,
                ``open()`` abre uno propio y lo gestiona/cierra. Inyectarlo permite
                compartir un dispositivo entre varias cámaras y testear sin hardware.
            person_class_id: id de clase a extraer de la salida NMS (0 = persona).
        """
        self.hef_path = hef_path
        self.conf = conf
        self.person_class_id = person_class_id
        self._vdevice = vdevice
        self._owns_vdevice = vdevice is None
        self._stack: contextlib.ExitStack | None = None
        self._pipe = None
        self._input_info = None
        self._output_name: str | None = None
        self._configured = False

    @staticmethod
    def _import_hailo():
        """Importa ``hailo_platform`` de forma perezosa (solo cuando se necesita HW)."""
        import hailo_platform

        return hailo_platform

    def open(self) -> Detector:
        """Configura HEF / VDevice / InferVStreams. Idempotente. Requiere Hailo real."""
        if self._configured:
            return self
        hp = self._import_hailo()
        self._stack = contextlib.ExitStack()
        hef = hp.HEF(self.hef_path)
        if self._vdevice is None:
            # VDevice propio: lo gestiona el ExitStack y se libera en close().
            self._vdevice = self._stack.enter_context(hp.VDevice())
            self._owns_vdevice = True
        cfg = hp.ConfigureParams.create_from_hef(
            hef, interface=hp.HailoStreamInterface.PCIe
        )
        network_group = self._vdevice.configure(hef, cfg)[0]
        ng_params = network_group.create_params()
        self._input_info = hef.get_input_vstream_infos()[0]
        self._output_name = hef.get_output_vstream_infos()[0].name
        inp = hp.InputVStreamParams.make(network_group, format_type=hp.FormatType.UINT8)
        outp = hp.OutputVStreamParams.make(
            network_group, format_type=hp.FormatType.FLOAT32
        )
        self._pipe = self._stack.enter_context(
            hp.InferVStreams(network_group, inp, outp)
        )
        self._stack.enter_context(network_group.activate(ng_params))
        self._configured = True
        return self

    def detect(self, frame_bgr) -> list[Detection]:
        """Infiere sobre un frame BGR y devuelve las personas como ``Detection``.

        OpenCV y numpy se importan de forma perezosa aquí: no hacen falta para importar
        el módulo ni para los tests puros (que ejercitan ``parse_nms_class``).
        """
        import cv2
        import numpy as np

        if not self._configured:
            self.open()
        height, width, _ = self._input_info.shape
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (width, height))
        res = self._pipe.infer({self._input_info.name: np.expand_dims(resized, 0)})
        out = res[self._output_name]
        arr = out[0] if isinstance(out, (list, np.ndarray)) else out
        class_dets = arr[self.person_class_id]
        return parse_nms_class(class_dets, conf=self.conf, class_id=self.person_class_id)

    def close(self) -> None:
        """Libera el pipeline y, si lo creó, el VDevice propio. Idempotente."""
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self._pipe = None
        self._configured = False
        if self._owns_vdevice:
            self._vdevice = None

    def __enter__(self) -> Detector:
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
