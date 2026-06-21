"""Verificación de firma minisign: pass/fail, formato, y cross-check RFC 8032."""
import os

import pytest

from agent import ed25519, minisign


def test_minisign_verify_pass():
    msg = b"cam-counter artifact \x00\x01\x02 bytes"
    pub, secret = minisign.generate_keypair()
    sig = minisign.sign(msg, secret)
    assert minisign.verify(msg, sig, pub) is True


def test_minisign_verify_fail_tampered_message():
    msg = b"original artifact"
    pub, secret = minisign.generate_keypair()
    sig = minisign.sign(msg, secret)
    assert minisign.verify(msg + b"!", sig, pub) is False


def test_minisign_verify_fail_wrong_key():
    msg = b"artifact"
    pub, secret = minisign.generate_keypair()
    sig = minisign.sign(msg, secret)
    other_pub, _ = minisign.generate_keypair()
    # key_id distinto -> rechazo.
    assert minisign.verify(msg, sig, other_pub) is False


def test_minisign_verify_fail_tampered_signature():
    msg = b"artifact"
    pub, secret = minisign.generate_keypair()
    sig = minisign.sign(msg, secret)
    lines = sig.splitlines()
    l2 = list(lines[1])
    l2[20] = "A" if l2[20] != "A" else "B"
    lines[1] = "".join(l2)
    assert minisign.verify(msg, "\n".join(lines) + "\n", pub) is False


def test_minisign_verify_fail_tampered_trusted_comment():
    msg = b"artifact"
    pub, secret = minisign.generate_keypair()
    sig = minisign.sign(msg, secret, trusted_comment="genuine")
    # Cambiar el trusted comment invalida la firma global.
    tampered = sig.replace("trusted comment: genuine", "trusted comment: forged")
    assert minisign.verify(msg, tampered, pub) is False


def test_pinned_pubkey_parses():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pub_path = os.path.join(here, "agent", "keys", "cam-counter-release.pub")
    with open(pub_path, encoding="utf-8") as fh:
        text = fh.read()
    key_id, pubkey = minisign.parse_public_key(text)
    assert len(key_id) == 8
    assert len(pubkey) == 32


def test_ed25519_roundtrip():
    seed = os.urandom(32)
    pub = ed25519.publickey(seed)
    msg = b"rfc 8032 message"
    sig = ed25519.signature(msg, seed, pub)
    assert ed25519.checkvalid(sig, msg, pub) is True
    assert ed25519.checkvalid(sig, msg + b"x", pub) is False


def test_ed25519_matches_cryptography():
    """Oráculo RFC 8032: nuestro Ed25519 puro debe coincidir bit a bit con `cryptography`."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = os.urandom(32)
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    pub_ref = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert ed25519.publickey(seed) == pub_ref
    msg = b"interop cross-check"
    assert ed25519.signature(msg, seed, pub_ref) == sk.sign(msg)
