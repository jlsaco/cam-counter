"""Flujo completo del update-agent: update ok, rollbacks, verify-fail, failed-marker, gates."""
import os

from agent.agent import Outcome, UpdateAgent
from agent.installer import Installer
from conftest import (
    FakeClock,
    FakeRegistry,
    FakeService,
    FakeStore,
    IncreasingFramesProbe,
    StaticProbe,
    make_config,
    make_tarball,
    seed_channel,
)


def _frames0(version):
    return {
        "status": "ok", "app_version": version, "db_schema_version": 3,
        "fake_source": True, "frames_flowing": False,
        "cameras": [{"camera_id": "rpi-001-cam0", "frames_processed": 0,
                     "last_inference_ts": None, "hailo_inference_ok": False,
                     "config_version": 1}],
    }


def _agent(cfg, store, probe, service=None, registry=None, verify_fn=None):
    return UpdateAgent(
        cfg, store, service or FakeService(), probe, FakeClock(),
        registry=registry, verify_fn=verify_fn or (lambda b, s, p: True),
    )


def test_noop_when_already_desired(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    inst.install_atomic("0.1.0", make_tarball("0.1.0"))
    inst.activate("0.1.0")
    store = FakeStore()
    seed_channel(store, cfg, "0.1.0", make_tarball("0.1.0"), secret=secret)
    reg = FakeRegistry()
    res = _agent(cfg, store, IncreasingFramesProbe("0.1.0"), registry=reg).run_once()
    assert res.outcome == Outcome.NOOP
    # Heartbeat de espejo, pero NUNCA leyó desired del registry (sólo del manifiesto S3).
    assert any(b.get("reported_version") == "0.1.0" for b in reg.beats)


def test_update_success_sets_last_good(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    tb = make_tarball("0.2.0")
    seed_channel(store, cfg, "0.2.0", tb, secret=secret)
    reg = FakeRegistry()
    agent = UpdateAgent(cfg, store, FakeService(), IncreasingFramesProbe("0.2.0"),
                        FakeClock(), registry=reg, verify_fn=lambda b, s, p: True)
    res = agent.run_once()
    assert res.outcome == Outcome.UPDATED
    assert agent.installer.current_version() == "0.2.0"
    assert agent.installer.last_good() == "0.2.0"
    assert reg.last()["last_update_status"] == "healthy"


def test_health200_frames0_rolls_back(tmp_path, test_keypair):
    """Una release "200 pero frames=0" se hace ROLLBACK a last_good (no se queda activa)."""
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    inst.install_atomic("0.1.0", make_tarball("0.1.0"))
    inst.activate("0.1.0")
    inst.set_last_good("0.1.0")
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret)
    reg = FakeRegistry()
    agent = UpdateAgent(cfg, store, FakeService(), StaticProbe(health=_frames0("0.2.0")),
                        FakeClock(), registry=reg, verify_fn=lambda b, s, p: True)
    res = agent.run_once()
    assert res.outcome == Outcome.ROLLED_BACK
    assert agent.installer.current_version() == "0.1.0", "revierte a last_good"
    assert agent.installer.last_good() == "0.1.0", "last_good NUNCA se borra"
    assert agent.installer.is_failed("0.2.0") is True, "failed-marker por versión"
    assert reg.last()["last_update_status"] == "rolled_back"


def test_crash_loop_rolls_back(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    inst.install_atomic("0.1.0", make_tarball("0.1.0"))
    inst.activate("0.1.0")
    inst.set_last_good("0.1.0")
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret)
    agent = UpdateAgent(cfg, store, FakeService(crashloop=True),
                        IncreasingFramesProbe("0.2.0"), FakeClock(),
                        verify_fn=lambda b, s, p: True)
    res = agent.run_once()
    assert res.outcome == Outcome.ROLLED_BACK
    assert agent.installer.current_version() == "0.1.0"


def test_failed_version_not_retried(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    inst.mark_failed("0.2.0", "previo")
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret)
    agent = _agent(cfg, store, IncreasingFramesProbe("0.2.0"))
    res = agent.run_once()
    assert res.outcome == Outcome.SKIPPED_FAILED
    # No descargó el artefacto (sólo leyó el manifiesto).
    assert all("releases/" not in k for k in store.gets)


def test_sha256_mismatch_does_not_install(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    tb = make_tarball("0.2.0")
    seed_channel(store, cfg, "0.2.0", tb, secret=secret, sha256="0" * 64)  # sha falso
    agent = _agent(cfg, store, IncreasingFramesProbe("0.2.0"))
    res = agent.run_once()
    assert res.outcome == Outcome.VERIFY_FAILED
    assert agent.installer.current_version() is None, "no instala con sha256 inválido"


def test_bad_signature_does_not_install(tmp_path, test_keypair):
    pub_path, _, _ = test_keypair
    from agent import minisign
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    tb = make_tarball("0.2.0")
    # Firma con OTRA clave (no la fijada en cfg.pubkey_path) -> verify real falla.
    _, other_secret = minisign.generate_keypair()
    seed_channel(store, cfg, "0.2.0", tb, secret=other_secret)
    # verify_fn REAL (no el stub): usa la pubkey fijada de cfg.
    agent = UpdateAgent(cfg, store, FakeService(), IncreasingFramesProbe("0.2.0"),
                        FakeClock())
    res = agent.run_once()
    assert res.outcome == Outcome.VERIFY_FAILED
    assert agent.installer.current_version() is None
    assert agent.installer.is_failed("0.2.0") is True


def test_min_agent_version_gate(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path, agent_version="0.0.1")
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret,
                 min_agent_version="0.1.0")
    res = _agent(cfg, store, IncreasingFramesProbe("0.2.0")).run_once()
    assert res.outcome == Outcome.SKIPPED_MIN_AGENT


def test_agent_never_reads_registry_to_decide(tmp_path, test_keypair):
    """El agente decide SÓLO con el manifiesto S3; el registry es un sink de heartbeat."""
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    Installer(cfg).ensure_dirs()
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret)

    class ReadDenyingRegistry(FakeRegistry):
        # No expone NINGÚN método de lectura; sólo heartbeat. Si el agente intentara leer
        # desired del registry, fallaría por AttributeError (no existe tal método).
        pass

    reg = ReadDenyingRegistry()
    res = UpdateAgent(cfg, store, FakeService(), IncreasingFramesProbe("0.2.0"),
                      FakeClock(), registry=reg,
                      verify_fn=lambda b, s, p: True).run_once()
    assert res.outcome == Outcome.UPDATED
    # La versión deseada salió del manifiesto del canal en S3.
    assert cfg.channel_manifest_key() in store.gets


def test_part_files_discarded_at_startup(tmp_path, test_keypair):
    pub_path, _, secret = test_keypair
    cfg = make_config(tmp_path, pubkey_path=pub_path)
    inst = Installer(cfg)
    inst.ensure_dirs()
    stale = os.path.join(cfg.releases_dir, "stale.part")
    with open(stale, "w") as fh:
        fh.write("x")
    store = FakeStore()
    seed_channel(store, cfg, "0.2.0", make_tarball("0.2.0"), secret=secret)
    _agent(cfg, store, IncreasingFramesProbe("0.2.0")).run_once()
    assert not os.path.exists(stale), ".part descartado al arranque del agente"
