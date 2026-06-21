"""Integración REAL contra el bucket de releases (gated por credenciales).

SIN credenciales (o sin boto3) -> SKIP (nunca rojo en CI sin OIDC). CON credenciales -> se
exige un PASS real (publicar -> validar -> leer -> limpiar contra AWS). Esta es la prueba que
demuestra que el bloque "publicar release -> leer manifiesto del canal" funciona de verdad.
"""
import pytest

from tools import selftest_release_bucket as st

_BUCKET = "cam-counter-fleet-releases-950639281773"


def test_selftest_release_bucket_real_aws():
    have, info = st._have_credentials()
    if not have:
        pytest.skip(f"sin credenciales AWS ({info}); el runner lo ejecuta en modo strict")
    # CON credenciales: PASS real obligatorio (publica/valida/lee/limpia y verifica limpieza).
    ok = st.run_selftest(_BUCKET, region="us-east-1", cleanup=True)
    assert ok is True
