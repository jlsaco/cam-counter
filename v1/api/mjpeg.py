"""Render de frames JPEG sintéticos para el stream MJPEG (sin cámara/Hailo).

``present`` del pipeline emite MJPEG como primitivo de vídeo en vivo. En el Pi el
frame proviene de la cámara; aquí (fuente falsa o modo sin hardware) lo generamos
con Pillow para que la UI y los E2E tengan un stream real y determinista.

Toda la geometría que se dibuja (línea, cajas) viene en floats normalizados 0..1
y se mapea al tamaño en píxeles del frame SÓLO para pintar (los píxeles nunca
salen en un contrato).
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image, ImageDraw

__all__ = ["MULTIPART_BOUNDARY", "multipart_chunk", "render_frame", "placeholder_frame"]

# Resolución del frame sintético (NO es un contrato; sólo para pintar píxeles).
FRAME_W = 640
FRAME_H = 360

MULTIPART_BOUNDARY = "camcounterframe"

# Caja XYXY normalizada 0..1.
NormBox = Sequence[float]
# Endpoint normalizado (x, y).
NormPoint = tuple[float, float]


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def render_frame(
    *,
    camera_id: str,
    frame_index: int,
    boxes: Sequence[NormBox],
    line: tuple[NormPoint, NormPoint] | None,
    in_count: int,
    out_count: int,
) -> bytes:
    """Dibuja un frame sintético (fondo + línea + cajas + HUD) y lo encodea a JPEG.

    Determinista respecto a las entradas: mismos argumentos -> mismos bytes.
    """
    img = Image.new("RGB", (FRAME_W, FRAME_H), (18, 22, 28))
    draw = ImageDraw.Draw(img)

    # Rejilla tenue para dar sensación de vídeo en vivo.
    for gx in range(0, FRAME_W, 64):
        draw.line([(gx, 0), (gx, FRAME_H)], fill=(28, 34, 42), width=1)
    for gy in range(0, FRAME_H, 64):
        draw.line([(0, gy), (FRAME_W, gy)], fill=(28, 34, 42), width=1)

    # Línea-umbral de conteo (normalizada -> píxeles).
    if line is not None:
        (ax, ay), (bx, by) = line
        draw.line(
            [(ax * FRAME_W, ay * FRAME_H), (bx * FRAME_W, by * FRAME_H)],
            fill=(255, 196, 0),
            width=3,
        )

    # Cajas de personas (normalizadas -> píxeles).
    for box in boxes:
        xmin, ymin, xmax, ymax = box
        draw.rectangle(
            [xmin * FRAME_W, ymin * FRAME_H, xmax * FRAME_W, ymax * FRAME_H],
            outline=(64, 220, 120),
            width=2,
        )

    # HUD: identificador, frame y contadores (texto bitmap por defecto de PIL).
    draw.text((8, 6), f"{camera_id}  f={frame_index}", fill=(220, 226, 232))
    draw.text((8, 22), f"in={in_count}  out={out_count}", fill=(255, 196, 0))
    return _jpeg_bytes(img)


def placeholder_frame(camera_id: str, message: str = "sin senal") -> bytes:
    """Frame estático 'sin señal' para el modo sin hardware (frames_processed=0)."""
    img = Image.new("RGB", (FRAME_W, FRAME_H), (12, 14, 18))
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), camera_id, fill=(150, 156, 162))
    draw.text((FRAME_W // 2 - 30, FRAME_H // 2), message, fill=(120, 126, 132))
    return _jpeg_bytes(img)


def multipart_chunk(jpeg: bytes) -> bytes:
    """Envuelve un JPEG como una parte ``multipart/x-mixed-replace``."""
    head = (
        f"--{MULTIPART_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n\r\n"
    ).encode("ascii")
    return head + jpeg + b"\r\n"
