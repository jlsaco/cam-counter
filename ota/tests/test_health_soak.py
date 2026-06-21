"""Health-check de PRODUCTO con soak: frames crecientes ok; frames=0/non-200/crash-loop fail."""
from agent.health import run_soak
from conftest import (
    FakeClock,
    FakeService,
    IncreasingFramesProbe,
    StaticProbe,
    make_config,
)


def _frames0_health(version):
    return {
        "status": "ok", "app_version": version, "db_schema_version": 3,
        "fake_source": True, "frames_flowing": False,
        "cameras": [{"camera_id": "rpi-001-cam0", "frames_processed": 0,
                     "last_inference_ts": None, "hailo_inference_ok": False,
                     "config_version": 1}],
    }


def test_soak_ok_when_frames_increasing(tmp_path):
    cfg = make_config(tmp_path)
    probe = IncreasingFramesProbe("0.1.0")
    res = run_soak(probe, FakeService(), FakeClock(), cfg, "0.1.0")
    assert res.ok is True
    assert res.samples >= 2


def test_soak_fail_frames_zero(tmp_path):
    """200 estático pero frames=0 (conteo roto) -> FALLA (debe disparar rollback)."""
    cfg = make_config(tmp_path)
    probe = StaticProbe(health=_frames0_health("0.1.0"))
    res = run_soak(probe, FakeService(), FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "frames" in res.reason.lower()


def test_soak_fail_non_200(tmp_path):
    cfg = make_config(tmp_path)
    probe = StaticProbe(raise_unavailable=True)
    res = run_soak(probe, FakeService(), FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "non-200" in res.reason


def test_soak_fail_crash_loop(tmp_path):
    cfg = make_config(tmp_path)
    probe = IncreasingFramesProbe("0.1.0")
    svc = FakeService(crashloop=True)  # NRestarts crece durante el soak
    res = run_soak(probe, svc, FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "crash-loop" in res.reason


def test_soak_fail_service_inactive(tmp_path):
    cfg = make_config(tmp_path)
    probe = IncreasingFramesProbe("0.1.0")
    res = run_soak(probe, FakeService(active=False), FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "active" in res.reason


def test_soak_fail_version_mismatch(tmp_path):
    """app_version reportado != versión instalada -> FALLA (drift = comparación de strings)."""
    cfg = make_config(tmp_path)
    probe = IncreasingFramesProbe("0.0.9")  # reporta una versión distinta
    res = run_soak(probe, FakeService(), FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "app_version" in res.reason


def test_soak_fail_db_schema_mismatch(tmp_path):
    cfg = make_config(tmp_path, expected_db_schema_version=99)
    probe = IncreasingFramesProbe("0.1.0", db_schema_version=3)
    res = run_soak(probe, FakeService(), FakeClock(), cfg, "0.1.0")
    assert res.ok is False
    assert "db_schema_version" in res.reason
