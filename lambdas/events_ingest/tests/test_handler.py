"""Tests de la Lambda de ingesta: contrato VERBATIM, anti-spoof, idempotencia."""

from __future__ import annotations

import copy
import hashlib
from decimal import Decimal

import jsonschema
import pytest

import handler
from ddb import CONDITION_NEW_ITEM
from fakeddb import FakeTable
from keys import build_sk, looks_like_clip_key


def _event_id(site, device, camera, track, seq):
    raw = f"{site}|{device}|{camera}|{track}|{seq}"
    return hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324 (dedup, no cripto)


SITE, DEVICE, CAMERA, TRACK, SEQ = "sitio-demo", "rpi-001", "rpi-001-cam1", "t-7", 3
EID = _event_id(SITE, DEVICE, CAMERA, TRACK, SEQ)
TS_MS = 1_718_900_000_000


def make_event(**overrides):
    ev = {
        "event_id": EID,
        "site_id": SITE,
        "device_id": DEVICE,
        "camera_id": CAMERA,
        "track_id": TRACK,
        "crossing_seq": SEQ,
        "direction": "in",
        "ts_event_ms": TS_MS,
        "ts_event_iso": "2024-06-20T16:13:20Z",
        "confidence": 0.91,
        "schema_version": 1,
        # meta inyectada por la IoT Rule (debe separarse antes de validar):
        "_device_id_topic": DEVICE,
        "_client_id": f"cam-counter-{SITE}-{DEVICE}",
        "_ingest_ts_ms": TS_MS + 50,
    }
    ev.update(overrides)
    return ev


# ───────────────────────── validación VERBATIM ─────────────────────────


def test_condition_unificada_con_el_borde():
    assert CONDITION_NEW_ITEM == "attribute_not_exists(PK) AND attribute_not_exists(SK)"


def test_meta_se_separa_y_evento_valida():
    payload, meta = handler.split_meta(make_event())
    assert set(meta) == {"_device_id_topic", "_client_id", "_ingest_ts_ms"}
    assert "_device_id_topic" not in payload
    # No lanza: el payload limpio cumple el contrato (additionalProperties:false).
    handler.validate_crossing_event(payload)


def test_evento_valido_put_nuevo():
    table = FakeTable()
    out = handler.process(make_event(), table)
    assert out == {"ok": True, "dup": False}
    (item,) = table.items.values()
    assert item["PK"] == f"CAM#{SITE}#{DEVICE}#{CAMERA}"
    assert item["SK"] == build_sk(TS_MS, EID)
    assert item["GSI1PK"] == f"SITE#{SITE}"
    assert item["GSI1SK"] == item["SK"]
    # marca de ingesta SÓLO-Lambda (paridad de ingesta).
    assert item["_ingest_ts_ms"] == TS_MS + 50
    # `synced` es SÓLO-local: NUNCA se persiste en la nube.
    assert "synced" not in item
    # confidence float -> Decimal (boto3 resource).
    assert isinstance(item["confidence"], Decimal)


def test_campo_extra_rechazado_verbatim():
    # `count_delta` no existe en el contrato -> additionalProperties:false lo rechaza.
    with pytest.raises(jsonschema.ValidationError):
        handler.process(make_event(count_delta=1), FakeTable())


def test_slug_invalido_rechazado():
    with pytest.raises(jsonschema.ValidationError):
        handler.process(make_event(site_id="Sitio_Malo"), FakeTable())


def test_schema_version_distinto_rechazado():
    with pytest.raises(jsonschema.ValidationError):
        handler.process(make_event(schema_version=2), FakeTable())


# ───────────────────────── anti-spoof ─────────────────────────


def test_topic_device_mismatch():
    with pytest.raises(ValueError, match="device_id mismatch"):
        handler.process(make_event(_device_id_topic="otro-pi"), FakeTable())


def test_clientid_spoof():
    with pytest.raises(ValueError, match="clientid spoof"):
        handler.process(make_event(_client_id="cam-counter-otro-sitio-rpi-001"), FakeTable())


def test_sin_meta_clientid_no_falla():
    ev = make_event()
    del ev["_client_id"]
    del ev["_device_id_topic"]
    out = handler.process(ev, FakeTable())
    assert out["dup"] is False


# ───────────────────────── clip_key acotado ─────────────────────────


def test_clip_key_valido_aceptado():
    ck = f"media/{SITE}/{DEVICE}/{CAMERA}/2024/06/20/{EID}.mp4"
    assert looks_like_clip_key(ck, make_event())
    out = handler.process(make_event(clip_key=ck, clip_status="uploaded"), FakeTable())
    assert out["dup"] is False


def test_clip_key_fuera_de_prefijo_rechazado():
    ck = f"media/otro-sitio/{DEVICE}/{CAMERA}/2024/06/20/{EID}.mp4"
    with pytest.raises(ValueError, match="clip_key fuera"):
        handler.process(make_event(clip_key=ck), FakeTable())


def test_clip_key_path_traversal_rechazado():
    ck = f"media/{SITE}/{DEVICE}/{CAMERA}/../../{EID}.mp4"
    assert not looks_like_clip_key(ck, make_event())


# ───────────────────────── idempotencia ─────────────────────────


def test_duplicado_es_exito():
    table = FakeTable()
    handler.process(make_event(), table)
    out = handler.process(make_event(), table)  # mismo event_id -> misma SK
    assert out == {"ok": True, "dup": True}
    assert len(table.items) == 1  # NO duplica


def test_clip_status_avanza_pending_a_uploaded_en_dup():
    table = FakeTable()
    handler.process(make_event(clip_status="pending"), table)
    (item,) = table.items.values()
    assert item["clip_status"] == "pending"
    # reintento con el clip ya subido: avanza monótonamente.
    ck = f"media/{SITE}/{DEVICE}/{CAMERA}/2024/06/20/{EID}.mp4"
    out = handler.process(make_event(clip_key=ck, clip_status="uploaded"), table)
    assert out["dup"] is True
    (item,) = table.items.values()
    assert item["clip_status"] == "uploaded"


def test_ts_event_ms_inmutable_misma_sk():
    # La SK no debe divergir para el mismo event_id: deriva de ts_event_ms (inmutable).
    a = handler.build_item(*handler.split_meta(make_event()))
    b = handler.build_item(*handler.split_meta(make_event()))
    assert a["SK"] == b["SK"] == build_sk(TS_MS, EID)


def test_no_muta_el_evento_entrante():
    ev = make_event()
    snapshot = copy.deepcopy(ev)
    handler.process(ev, FakeTable())
    assert ev == snapshot
