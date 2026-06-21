"""El artefacto OTA NO contiene el secreto de cámara quemado (RWCHBY) ni rutas /home/pi."""
import os
import subprocess
import tarfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(args):
    return subprocess.run(args, cwd=_REPO_ROOT, capture_output=True, text=True, check=True)


def test_bundle_file_list_has_no_secret_or_abs_path():
    out = _run(["bash", "ota/packaging/make-release.sh", "--list-bundle-files"])
    files = [f for f in out.stdout.splitlines() if f.strip()]
    assert files, "la lista de bundle no debe estar vacía"
    leaked = []
    for f in files:
        path = os.path.join(_REPO_ROOT, f)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        if b"RWCHBY" in data or b"/home/pi" in data:
            leaked.append(f)
    assert leaked == [], f"secreto/ruta absoluta filtrada en: {leaked}"


def test_built_tarball_has_no_secret(tmp_path):
    _run(["bash", "ota/packaging/make-release.sh", "--out-dir", str(tmp_path)])
    tarball = next(p for p in os.listdir(tmp_path) if p.endswith(".tar.gz"))
    with tarfile.open(os.path.join(tmp_path, tarball)) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            data = tf.extractfile(member).read()
            assert b"RWCHBY" not in data, f"secreto en {member.name}"
            assert b"/home/pi" not in data, f"ruta /home/pi en {member.name}"
