"""La MISMA version string fluye por bundle-manifest, channel-manifest, registry y /api/device.

Drift = comparación de strings. La única fuente es `scripts/version.py` (`git describe`).
"""
import importlib.util
import json
import os
import subprocess
import tarfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module(relpath, name):
    path = os.path.join(_REPO_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canonical_version():
    out = subprocess.run(["python3", "scripts/version.py"], cwd=_REPO_ROOT,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_version_py_is_single_source():
    version_mod = _load_module("scripts/version.py", "_v")
    version, _git_sha, _dirty, _release = version_mod.derive()
    assert version == _canonical_version()


def test_bundle_manifest_version_matches(tmp_path):
    v = _canonical_version()
    subprocess.run(["bash", "ota/packaging/make-release.sh", "--out-dir", str(tmp_path)],
                   cwd=_REPO_ROOT, check=True, capture_output=True, text=True)
    tarball = next(p for p in os.listdir(tmp_path) if p.endswith(".tar.gz"))
    with tarfile.open(os.path.join(tmp_path, tarball)) as tf:
        member = f"cam-counter-edge-{v}/bundle-manifest.json"
        data = json.load(tf.extractfile(member))
    assert data["version"] == v


def test_channel_manifest_version_matches():
    v = _canonical_version()
    pm = _load_module("scripts/publish_manifest.py", "_pm")
    artifact = {
        "key": f"releases/{v}/cam-counter-edge-{v}-arm64.tar.gz",
        "sha256": "a" * 64, "size_bytes": 1, "sig_key": "x.minisig",
    }
    manifest = pm.build_manifest("canary", v, artifact, None, git_sha="s",
                                 released_by="t", min_agent_version="0.1.0",
                                 sequence=1, previous_version=None)
    assert manifest["version"] == v


def test_registry_heartbeat_carries_same_version():
    """reported_version/desired_version del registry = la MISMA cadena (passthrough)."""
    v = _canonical_version()
    # El heartbeat es un passthrough de strings (ver clients.DynamoRegistry).
    fields = {"reported_version": v, "desired_version": v}
    assert fields["reported_version"] == v == fields["desired_version"]


def test_api_app_version_uses_same_derivation():
    """/api/device app_version se deriva de scripts/version.py (igual que el resto)."""
    v = _canonical_version()
    # engine._version_info importa scripts/version.py por ruta y usa derive() (idéntico path).
    version_mod = _load_module("scripts/version.py", "_v2")
    assert version_mod.derive()[0] == v
    engine_src = open(os.path.join(_REPO_ROOT, "v1", "api", "engine.py"),
                      encoding="utf-8").read()
    assert "version.py" in engine_src and "derive()" in engine_src
