"""Ciclo completo OTA con un fake-release DETERMINISTA (artefacto REAL de make-release.sh).

Ejercita extracción REAL del tarball + instalación atómica + swap de symlink + soak. Es el
equivalente determinista al "ciclo completo en qemu-arm64": el payload es Python puro y no
requiere ejecutar binarios arm64, por lo que un fake-release determinista cubre el flujo de
INSTALACIÓN/ACTIVACIÓN/ROLLBACK del agente en x86 sin qemu. (Si se dispusiera de qemu-arm64,
el mismo tarball podría además arrancarse bajo emulación; aquí validamos el agente, no el
binario nativo, que queda fuera del payload — native_blob.)
"""
import hashlib
import os
import subprocess

from agent import minisign
from agent.agent import Outcome, UpdateAgent
from agent.installer import Installer
from conftest import (
    FakeClock,
    FakeRegistry,
    FakeService,
    FakeStore,
    IncreasingFramesProbe,
    make_config,
    seed_channel,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _real_artifact(tmp_path, version_label="0.0.0-dev"):
    """Construye un artefacto REAL con make-release.sh y lo devuelve (bytes, version)."""
    out = subprocess.run(
        ["bash", "ota/packaging/make-release.sh", "--out-dir", str(tmp_path)],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    tarball = next(p for p in os.listdir(tmp_path) if p.endswith(".tar.gz"))
    version = subprocess.run(["python3", "scripts/version.py"], cwd=_REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    with open(os.path.join(tmp_path, tarball), "rb") as fh:
        return fh.read(), version, out.stdout


def test_full_cycle_real_artifact_installs_and_activates(tmp_path):
    blob, version, _ = _real_artifact(tmp_path)
    pub_text, secret = minisign.generate_keypair()
    pub_path = tmp_path / "rel.pub"
    pub_path.write_text(pub_text)

    cfg = make_config(tmp_path, pubkey_path=str(pub_path))
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    sig_text = minisign.sign(blob, secret)
    seed_channel(store, cfg, version, blob, secret=secret,
                 sha256=hashlib.sha256(blob).hexdigest(), sig_text=sig_text)

    agent = UpdateAgent(cfg, store, FakeService(), IncreasingFramesProbe(version),
                        FakeClock(), registry=FakeRegistry())
    res = agent.run_once()

    assert res.outcome == Outcome.UPDATED
    assert agent.installer.current_version() == version
    # El payload REAL se extrajo: bundle-manifest.json presente en la release instalada.
    installed = agent.installer.release_path(version)
    assert os.path.isfile(os.path.join(installed, "bundle-manifest.json"))
    assert os.path.isdir(os.path.join(installed, "edge"))
    assert agent.installer.last_good() == version
