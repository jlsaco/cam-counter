"""Verificación (y firma de prueba) de firmas **minisign** en Python puro.

El agente verifica el artefacto OTA contra una **pubkey minisign fijada** en el repo/imagen
(`ota/agent/keys/cam-counter-release.pub`). La clave privada vive SÓLO en AWS Secrets Manager
y firma en el workflow de release con el `minisign` real; NUNCA se committea.

Formato minisign (interoperable con el binario oficial):

- Fichero de clave pública (2 líneas):
    `untrusted comment: ...`
    base64( sig_alg[2] || key_id[8] || public_key[32] )      # sig_alg = b"Ed"

- Fichero de firma `.minisig` (4 líneas):
    `untrusted comment: ...`
    base64( sig_alg[2] || key_id[8] || signature[64] )       # b"ED" prehash | b"Ed" legacy
    `trusted comment: <texto>`
    base64( global_signature[64] )

Verificación:
  1) key_id de la firma == key_id de la pubkey.
  2) si sig_alg == b"ED": firma Ed25519 sobre BLAKE2b-512(fichero); si b"Ed": sobre el
     fichero crudo.
  3) global_signature: Ed25519 sobre (signature[64] || bytes-del-trusted-comment).

Firmamos con el modo prehashed `ED` (por defecto en minisign moderno). El verificador
acepta ambos modos.
"""
import base64
import hashlib
import os

from . import ed25519

_ALG_LEGACY = b"Ed"  # firma sobre el mensaje crudo
_ALG_PREHASH = b"ED"  # firma sobre BLAKE2b-512(mensaje)
_UNTRUSTED_PREFIX = "untrusted comment: "
_TRUSTED_PREFIX = "trusted comment: "


class MinisignError(Exception):
    """Error de parseo o de verificación de firma minisign."""


def _b64(data):
    return base64.standard_b64encode(data).decode("ascii")


def _unb64(text):
    try:
        return base64.standard_b64decode(text.strip().encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - normalizamos a MinisignError
        raise MinisignError(f"base64 inválido: {exc}") from exc


def parse_public_key(text):
    """Devuelve (key_id: bytes[8], public_key: bytes[32]) de un fichero .pub minisign."""
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        raise MinisignError("fichero de pubkey vacío")
    # La última línea no-comentario es la clave (tolera/ignora el comentario).
    key_line = lines[-1]
    raw = _unb64(key_line)
    if len(raw) != 2 + 8 + 32:
        raise MinisignError("longitud de pubkey minisign inválida")
    alg, key_id, pubkey = raw[:2], raw[2:10], raw[10:42]
    if alg != _ALG_LEGACY:
        raise MinisignError(f"algoritmo de pubkey no soportado: {alg!r}")
    return key_id, pubkey


def parse_signature(text):
    """Devuelve (alg, key_id, signature, trusted_comment, global_sig) de un .minisig."""
    lines = text.splitlines()
    if len(lines) < 4:
        raise MinisignError("fichero .minisig incompleto (se esperan 4 líneas)")
    sig_raw = _unb64(lines[1])
    if len(sig_raw) != 2 + 8 + 64:
        raise MinisignError("longitud de firma minisign inválida")
    alg, key_id, sig = sig_raw[:2], sig_raw[2:10], sig_raw[10:74]
    if alg not in (_ALG_LEGACY, _ALG_PREHASH):
        raise MinisignError(f"algoritmo de firma no soportado: {alg!r}")
    tline = lines[2]
    if not tline.startswith(_TRUSTED_PREFIX):
        raise MinisignError("falta el 'trusted comment' en el .minisig")
    trusted_comment = tline[len(_TRUSTED_PREFIX):]
    global_sig = _unb64(lines[3])
    if len(global_sig) != 64:
        raise MinisignError("longitud de la firma global inválida")
    return alg, key_id, sig, trusted_comment, global_sig


def _digest_for_alg(alg, message):
    if alg == _ALG_PREHASH:
        return hashlib.blake2b(message, digest_size=64).digest()
    return message  # legacy: firma sobre el mensaje crudo


def verify(message, signature_text, public_key_text):
    """True sii `signature_text` (.minisig) firma `message` bajo la pubkey dada.

    Verifica AMBAS firmas (la del contenido y la global del trusted-comment) y que el
    key_id coincida. Devuelve False ante cualquier discrepancia; NUNCA lanza por contenido
    malformado (lo normaliza a False salvo errores de programación).
    """
    try:
        key_id_pub, pubkey = parse_public_key(public_key_text)
        alg, key_id_sig, sig, trusted_comment, global_sig = parse_signature(
            signature_text
        )
    except MinisignError:
        return False

    if key_id_sig != key_id_pub:
        return False

    signed = _digest_for_alg(alg, message)
    if not ed25519.checkvalid(sig, signed, pubkey):
        return False

    # La firma global cubre signature || trusted_comment (evita manipular el comentario).
    global_signed = sig + trusted_comment.encode("utf-8")
    return ed25519.checkvalid(global_sig, global_signed, pubkey)


# ─────────────────────────── Firma de PRUEBA (tests / selftest) ───────────────────────────
# NO se usa en producción: en producción firma el `minisign` real con la clave de Secrets
# Manager. Esto sólo produce keypairs/firmas EFÍMERAS para los tests y el self-test del
# bucket, sin depender del binario minisign ni de `cryptography`.


def generate_keypair(comment="cam-counter test key"):
    """Genera un keypair efímero. Devuelve (pub_text, secret_dict). NO para producción."""
    seed = os.urandom(32)
    key_id = os.urandom(8)
    pub = ed25519.publickey(seed)
    pub_blob = _ALG_LEGACY + key_id + pub
    pub_text = f"{_UNTRUSTED_PREFIX}{comment}\n{_b64(pub_blob)}\n"
    secret = {"seed": seed, "key_id": key_id, "pubkey": pub}
    return pub_text, secret


def sign(message, secret, trusted_comment="cam-counter test artifact", comment="signature"):
    """Produce el texto de un `.minisig` (modo prehashed `ED`) para los tests/selftest."""
    seed, key_id, pubkey = secret["seed"], secret["key_id"], secret["pubkey"]
    digest = _digest_for_alg(_ALG_PREHASH, message)
    sig = ed25519.signature(digest, seed, pubkey)
    sig_blob = _ALG_PREHASH + key_id + sig
    global_sig = ed25519.signature(sig + trusted_comment.encode("utf-8"), seed, pubkey)
    return (
        f"{_UNTRUSTED_PREFIX}{comment}\n"
        f"{_b64(sig_blob)}\n"
        f"{_TRUSTED_PREFIX}{trusted_comment}\n"
        f"{_b64(global_sig)}\n"
    )
