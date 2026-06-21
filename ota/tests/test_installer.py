"""Instalación atómica, swap de symlink, retención de last_good, .part y failed-markers."""
import os

from agent.installer import Installer
from conftest import make_config, make_tarball


def _install_and_activate(inst, version):
    inst.install_atomic(version, make_tarball(version))
    inst.activate(version)


def test_atomic_swap_points_current(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    _install_and_activate(inst, "0.1.0")
    assert inst.current_version() == "0.1.0"
    assert os.path.islink(cfg.current_link)
    # El symlink es RELATIVO (releases/<version>) -> portabilidad del install_root.
    assert os.readlink(cfg.current_link) == os.path.join("releases", "0.1.0")


def test_activate_swaps_atomically_between_versions(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    _install_and_activate(inst, "0.1.0")
    _install_and_activate(inst, "0.2.0")
    assert inst.current_version() == "0.2.0"
    inst.rollback("0.1.0")
    assert inst.current_version() == "0.1.0"


def test_last_good_never_deleted_and_min_two_retained(tmp_path):
    cfg = make_config(tmp_path, min_releases_retained=2)
    inst = Installer(cfg)
    inst.ensure_dirs()
    for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0"):
        _install_and_activate(inst, v)
    inst.set_last_good("0.1.0")  # last_good antiguo: NUNCA debe borrarse
    inst.activate("0.4.0")
    inst.prune_old_releases()
    remaining = sorted(os.listdir(cfg.releases_dir))
    assert "0.1.0" in remaining, "last_good jamás se borra"
    assert "0.4.0" in remaining, "current jamás se borra"
    assert len(remaining) >= 2


def test_discard_part_files(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    # Restos .part de una descarga/extracción a medias.
    part_dir = os.path.join(cfg.releases_dir, ".0.9.0-abc.part")
    os.makedirs(part_dir)
    part_file = os.path.join(cfg.releases_dir, "junk.part")
    with open(part_file, "w") as fh:
        fh.write("x")
    inst.discard_part_files()
    assert not os.path.exists(part_dir)
    assert not os.path.exists(part_file)


def test_failed_marker_roundtrip(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    assert inst.is_failed("0.9.0") is False
    inst.mark_failed("0.9.0", "soak fail")
    assert inst.is_failed("0.9.0") is True
    inst.clear_failed("0.9.0")
    assert inst.is_failed("0.9.0") is False


def test_install_is_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    tb = make_tarball("0.1.0")
    p1 = inst.install_atomic("0.1.0", tb)
    p2 = inst.install_atomic("0.1.0", tb)  # reintento: no re-extrae
    assert p1 == p2
    assert os.path.exists(os.path.join(p1, "bundle-manifest.json"))


def test_last_good_survives_in_shared(tmp_path):
    cfg = make_config(tmp_path)
    inst = Installer(cfg)
    inst.set_last_good("0.1.0")
    # last_good vive en shared/ota (sobrevive a upgrades): otra instancia lo lee.
    inst2 = Installer(cfg)
    assert inst2.last_good() == "0.1.0"
    assert cfg.shared_dir in inst._last_good_file()
