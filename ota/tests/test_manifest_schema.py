"""El channel-manifest valida contra el schema; formas inválidas se rechazan."""
import jsonschema
import pytest

from tools import validate_manifest


def _schema():
    return validate_manifest.load_schema(validate_manifest._DEFAULT_SCHEMA)


def test_canonical_example_validates():
    validate_manifest.validate(validate_manifest.example_manifest(), _schema())


def test_stable_channel_validates():
    validate_manifest.validate(
        validate_manifest.example_manifest(channel="stable", version="1.2.3", sequence=5),
        _schema(),
    )


def test_invalid_channel_rejected():
    m = validate_manifest.example_manifest()
    m["channel"] = "_selftest"  # no está en el enum [canary, stable]
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest.validate(m, _schema())


def test_missing_required_rejected():
    m = validate_manifest.example_manifest()
    del m["artifact"]
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest.validate(m, _schema())


def test_bad_sha256_pattern_rejected():
    m = validate_manifest.example_manifest()
    m["artifact"]["sha256"] = "NOTHEX"
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest.validate(m, _schema())


def test_additional_property_rejected():
    m = validate_manifest.example_manifest()
    m["surprise"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest.validate(m, _schema())
