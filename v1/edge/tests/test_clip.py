"""Tests del buffer/grabador de clips (``clip.ClipRecorder``) y la clave S3.

Cubren en x86 SIN red/Hailo/cámara (frames sintéticos, ``tmp_path``):
- un "cruce" produce un clip de la DURACIÓN esperada (nº de frames ≈
  (pre+post)·FPS) escrito ATÓMICAMENTE (queda el fichero final, sin ``.part``),
- tras el clip hay EXACTAMENTE UNA fila ``pending`` en ``clip_uploads`` con
  ``s3_key_planned`` que coincide con la plantilla de media construida desde
  slugs VALIDADOS,
- la clave se construye SÓLO desde slugs válidos: ``'#'`` / ``'/'`` / mayúsculas /
  vacío / >63 chars son rechazados ANTES de construir la clave.

El grabador es ASÍNCRONO: ``request_clip`` no escribe nada (lo prueba el assert de
que no hay resultado hasta completar el post-roll y hacer ``flush``).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from cam_counter_edge.clip import ClipRecorder, encode_jpeg
from cam_counter_edge.identifiers import InvalidSlugError, media_clip_key
from cam_counter_edge.line_counter import compute_event_id
from cam_counter_edge.store import Store
from cam_counter_edge.types import CrossingEvent

SITE = "site-a"
DEVICE = "pi-001"
CAMERA = "pi-001-cam0"
TS = 1_700_000_000_000  # 2023-11-14 UTC -> media/.../2023/11/14/...
TRACK_ID = "7"


def _event(crossing_seq: int = 1) -> CrossingEvent:
    """``CrossingEvent`` válido con ``event_id`` determinista (sha1)."""
    event_id = compute_event_id(SITE, DEVICE, CAMERA, TRACK_ID, crossing_seq)
    return CrossingEvent(
        event_id=event_id,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=CAMERA,
        track_id=TRACK_ID,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=TS,
        ts_event_iso="2023-11-14T22:13:20.000Z",
        line_version=1,
        confidence=0.9,
        clip_status="pending",
        schema_version=1,
    )


def _frame(value: int) -> np.ndarray:
    """Frame RGB uint8 sintético (dimensiones pares para encoders MP4)."""
    return np.full((48, 64, 3), value % 256, dtype=np.uint8)


def _jpeg(value: int) -> bytes:
    return encode_jpeg(_frame(value))


# -- duración del clip + escritura atómica --------------------------------


def test_clip_duration_and_atomic_write(tmp_path) -> None:
    """Un cruce ensambla pre+post y escribe el clip atómicamente."""
    store = Store(str(tmp_path / "events.db"))
    out_dir = tmp_path / "clips"
    fps, pre_s, post_s = 10.0, 1.0, 1.0  # pre=10, post=10 -> ~20 frames
    recorder = ClipRecorder(
        store, out_dir=out_dir, fps=fps, pre_seconds=pre_s, post_seconds=post_s
    )
    event = _event()

    # Pre-roll: 12 frames (el buffer circular conserva sólo los últimos 10).
    for k in range(12):
        recorder.add_frame(CAMERA, _jpeg(k), ts_ms=TS - (12 - k) * 100)

    recorder.request_clip(event)
    # ASÍNCRONO: aún sin post-roll, no hay clip producido todavía.
    assert recorder.result_for(event.event_id) is None

    # Post-roll: 10 frames -> completa la ventana y dispara el encode.
    for k in range(10):
        recorder.add_frame(CAMERA, _jpeg(100 + k), ts_ms=TS + (k + 1) * 100)

    recorder.flush(timeout=15.0)
    result = recorder.result_for(event.event_id)
    assert result is not None

    # Duración: pre(10) + post(10) = 20 frames ≈ (pre+post)*fps = 20 (con tolerancia).
    expected = round((pre_s + post_s) * fps)
    assert result.frame_count == 20
    assert abs(result.frame_count - expected) <= 2

    # Escritura ATÓMICA: queda exactamente el fichero final, sin ningún `.part`.
    assert result.ext in ("mp4", "gif")
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == [f"{event.event_id}.{result.ext}"]
    assert not any(".part" in name for name in files)
    assert result.local_path == str(out_dir / f"{event.event_id}.{result.ext}")

    # Cross-check de duración cuando el formato es GIF (decodificable con Pillow).
    if result.ext == "gif":
        with Image.open(result.local_path) as img:
            assert getattr(img, "n_frames", 1) == 20

    recorder.close()
    store.close()


# -- encolado pending + s3_key_planned ------------------------------------


def test_clip_enqueues_pending_with_s3_key(tmp_path) -> None:
    """Tras el clip hay EXACTAMENTE una fila ``pending`` con la clave correcta."""
    store = Store(str(tmp_path / "events.db"))
    out_dir = tmp_path / "clips"
    recorder = ClipRecorder(
        store, out_dir=out_dir, fps=10.0, pre_seconds=1.0, post_seconds=1.0
    )
    event = _event()

    for k in range(10):
        recorder.add_frame(CAMERA, _jpeg(k), ts_ms=TS - (10 - k) * 100)
    recorder.request_clip(event)
    for k in range(10):
        recorder.add_frame(CAMERA, _jpeg(100 + k), ts_ms=TS + (k + 1) * 100)
    recorder.flush(timeout=15.0)

    result = recorder.result_for(event.event_id)
    assert result is not None

    pending = store.get_clip_uploads(status="pending")
    assert len(pending) == 1  # EXACTAMENTE una fila pending
    row = pending[0]
    assert row["event_id"] == event.event_id
    assert row["camera_id"] == CAMERA
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["local_path"] == result.local_path

    # La clave coincide con la plantilla de media construida desde slugs validados.
    expected_key = media_clip_key(SITE, DEVICE, CAMERA, event.event_id, result.ext, TS)
    assert row["s3_key_planned"] == expected_key
    assert result.s3_key_planned == expected_key
    assert expected_key == (
        f"media/{SITE}/{DEVICE}/{CAMERA}/2023/11/14/{event.event_id}.{result.ext}"
    )

    recorder.close()
    store.close()


# -- la clave S3 SÓLO se construye desde slugs válidos --------------------


def test_s3_key_built_from_template() -> None:
    """La clave válida sigue la plantilla ``media/.../{yyyy}/{mm}/{dd}/{id}.{ext}``."""
    event_id = compute_event_id(SITE, DEVICE, CAMERA, TRACK_ID, 1)
    key = media_clip_key(SITE, DEVICE, CAMERA, event_id, "mp4", TS)
    assert key == f"media/{SITE}/{DEVICE}/{CAMERA}/2023/11/14/{event_id}.mp4"
    # event_id es sha1 hex de 40 chars, no criptográfico (sólo dedupe).
    assert len(event_id) == 40 and event_id == event_id.lower()


@pytest.mark.parametrize("bad", ["UPPER", "has/slash", "has#hash", "", "x" * 64])
def test_s3_key_rejects_invalid_slugs(bad: str) -> None:
    """Un slug inválido es RECHAZADO ANTES de construir la clave (cada posición)."""
    event_id = "a" * 40
    with pytest.raises(InvalidSlugError):
        media_clip_key(bad, DEVICE, CAMERA, event_id, "mp4", TS)
    with pytest.raises(InvalidSlugError):
        media_clip_key(SITE, bad, CAMERA, event_id, "mp4", TS)
    with pytest.raises(InvalidSlugError):
        media_clip_key(SITE, DEVICE, bad, event_id, "mp4", TS)


def test_s3_key_rejects_invalid_event_id_and_ext() -> None:
    """``event_id`` no-sha1 o extensión no soportada son rechazados."""
    valid_event = "a" * 40
    with pytest.raises(InvalidSlugError):
        media_clip_key(SITE, DEVICE, CAMERA, "NOT-A-SHA1", "mp4", TS)
    with pytest.raises(InvalidSlugError):
        media_clip_key(SITE, DEVICE, CAMERA, "ABC" + "a" * 37, "mp4", TS)  # mayúsculas
    with pytest.raises(InvalidSlugError):
        media_clip_key(SITE, DEVICE, CAMERA, valid_event, "exe", TS)


# -- el camino de conteo no bloquea: encolar es no bloqueante --------------


def test_request_clip_is_nonblocking(tmp_path) -> None:
    """``request_clip``/``add_frame`` sólo tocan memoria: ningún clip hasta flush."""
    store = Store(str(tmp_path / "events.db"))
    recorder = ClipRecorder(
        store, out_dir=tmp_path / "clips", fps=10.0, pre_seconds=1.0, post_seconds=1.0
    )
    event = _event()
    for k in range(10):
        recorder.add_frame(CAMERA, _jpeg(k), ts_ms=TS - (10 - k) * 100)
    recorder.request_clip(event)  # no bloquea, no escribe
    assert recorder.result_for(event.event_id) is None
    assert store.get_clip_uploads(status="pending") == []
    recorder.close()  # finaliza el post-roll pendiente (best-effort) y drena
    store.close()
