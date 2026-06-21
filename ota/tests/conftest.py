"""Fakes deterministas + helpers compartidos por la suite OTA (x86, sin Pi/Hailo/cámara/red)."""
import gzip
import io
import json
import os
import sys
import tarfile

import pytest

_OTA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_OTA_DIR)
for _p in (_OTA_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent import minisign  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.interfaces import HealthUnavailable  # noqa: E402


# ─────────────────────────────── fakes de interfaces ───────────────────────────────
class FakeStore:
    """ObjectStore en memoria (NUNCA presigned: sólo expone get_bytes, igual que el real)."""

    def __init__(self):
        self.objects = {}
        self.gets = []

    def put(self, key, data):
        self.objects[key] = data if isinstance(data, bytes) else data.encode("utf-8")

    def get_bytes(self, key):
        self.gets.append(key)
        if key not in self.objects:
            raise FileNotFoundError(f"no such key: {key}")
        return self.objects[key]


class FakeService:
    def __init__(self, active=True, nrestarts=0, crashloop=False):
        self.active = active
        self._nr = nrestarts
        self.crashloop = crashloop
        self.restart_calls = []

    def restart(self, name):
        self.restart_calls.append(name)

    def is_active(self, name):
        return self.active

    def n_restarts(self, name):
        v = self._nr
        if self.crashloop:
            self._nr += 1  # simula que el servicio se reinicia solo (crash-loop)
        return v


class IncreasingFramesProbe:
    """/api/health que devuelve frames_processed CRECIENTE por cámara (salud real)."""

    def __init__(self, version, cams=("rpi-001-cam0",), start=0, step=10,
                 status="ok", db_schema_version=3, frames_flowing=True):
        self.version = version
        self.cams = cams
        self.start = start
        self.step = step
        self.status = status
        self.db = db_schema_version
        self.frames_flowing = frames_flowing
        self.i = 0

    def get(self):
        frames = self.start + self.step * self.i
        self.i += 1
        return {
            "status": self.status,
            "app_version": self.version,
            "db_schema_version": self.db,
            "fake_source": True,
            "frames_flowing": self.frames_flowing,
            "cameras": [
                {"camera_id": c, "frames_processed": frames,
                 "last_inference_ts": 1000 + frames, "hailo_inference_ok": True,
                 "config_version": 1}
                for c in self.cams
            ],
        }


class StaticProbe:
    """Devuelve siempre el mismo dict (o lanza non-200)."""

    def __init__(self, health=None, raise_unavailable=False):
        self.health = health
        self.raise_unavailable = raise_unavailable

    def get(self):
        if self.raise_unavailable:
            raise HealthUnavailable("fake non-200")
        return self.health


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def now_iso(self):
        return "2026-01-01T00:00:00Z"


class FakeRegistry:
    def __init__(self):
        self.beats = []

    def heartbeat(self, **fields):
        self.beats.append(fields)

    def last(self):
        return self.beats[-1] if self.beats else None


# ─────────────────────────────── helpers de artefacto ───────────────────────────────
def make_tarball(version, extra_files=None):
    """Crea un tar.gz determinista con `cam-counter-edge-<version>/bundle-manifest.json`."""
    files = {
        "bundle-manifest.json": json.dumps({
            "schema_version": 1, "version": version, "git_sha": "0000000",
            "built_at": "2026-01-01T00:00:00Z", "min_agent_version": "0.1.0",
            "entrypoint": "edge/run_edge.sh",
        }).encode(),
        "edge/run_edge.sh": b"#!/usr/bin/env bash\nexit 0\n",
    }
    if extra_files:
        files.update(extra_files)
    buf = io.BytesIO()
    raw = io.BytesIO()
    top = f"cam-counter-edge-{version}"
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    buf.write(gzip.compress(raw.getvalue(), mtime=0))
    return buf.getvalue()


def make_config(tmp_path, **overrides):
    cfg = AgentConfig(
        channel="canary",
        bucket="cam-counter-fleet-releases-950639281773",
        install_root=str(tmp_path / "opt" / "cam-counter"),
        soak_seconds=0.5,
        poll_interval=0.1,
        expected_db_schema_version=3,
        agent_version="0.1.0",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg.validate()


def seed_channel(store, cfg, version, tarball, secret=None, sha256=None,
                 sig_text=None, min_agent_version="0.1.0", native_blob=None):
    """Coloca en el store el manifiesto del canal + artefacto + sha + minisig."""
    import hashlib

    art_key = f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz"
    sig_key = f"{art_key}.minisig"
    digest = sha256 or hashlib.sha256(tarball).hexdigest()
    if sig_text is None and secret is not None:
        sig_text = minisign.sign(tarball, secret)
    manifest = {
        "schema_version": 1, "channel": cfg.channel, "version": version, "sequence": 1,
        "artifact": {"key": art_key, "sha256": digest, "size_bytes": len(tarball),
                     "sig_key": sig_key},
        "min_agent_version": min_agent_version,
        "released_at": "2026-01-01T00:00:00Z", "released_by": "test",
        "git_sha": "0000000", "previous_version": None,
    }
    if native_blob:
        manifest["native_blob"] = native_blob
    store.put(cfg.channel_manifest_key(), json.dumps(manifest).encode())
    store.put(art_key, tarball)
    if sig_text is not None:
        store.put(sig_key, sig_text.encode())
    return manifest


@pytest.fixture
def repo_root():
    return _REPO_ROOT


@pytest.fixture
def test_keypair(tmp_path):
    """Keypair efímero + ruta de pubkey escrita en disco (para cfg.pubkey_path)."""
    pub_text, secret = minisign.generate_keypair(comment="test")
    pub_path = tmp_path / "test.pub"
    pub_path.write_text(pub_text)
    return str(pub_path), pub_text, secret
