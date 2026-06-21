"""Ed25519 (RFC 8032) en Python PURO — sólo stdlib (`hashlib`).

Por qué pura-Python y no `minisign`/`cryptography`/`pynacl`:
- El update-agent corre en la Pi y debe verificar firmas con CERO dependencias externas
  (ni el binario `minisign`, ni `cryptography`, ni `pynacl`): así la verificación de firma
  NUNCA falla por una dependencia ausente en una imagen mínima.
- Es la implementación de referencia de RFC 8032 (interoperable con el `minisign` real que
  firma en CI con la clave de Secrets Manager): mismo Ed25519 estándar.

No optimizada (scalarmult recursivo): se ejecuta un puñado de veces por actualización
(verificar un artefacto), no en caliente. La suite de tests la contrasta contra la librería
`cryptography` cuando está disponible (oráculo RFC-8032), además de un roundtrip propio.
"""
import hashlib

_b = 256
_q = 2 ** 255 - 19
# Orden del subgrupo (L).
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _sha512(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    # Inverso modular vía pequeño teorema de Fermat (q es primo).
    return pow(x, _q - 2, _q)


_d = (-121665 * _inv(121666)) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = (4 * _inv(5)) % _q
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _encodeint(y):
    bits = [(y >> i) & 1 for i in range(_b)]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8)
    )


def _encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(_b - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8)
    )


def _Hint(m):
    h = _sha512(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * _b))


def _secret_scalar(h):
    return 2 ** (_b - 2) + sum(2 ** i * _bit(h, i) for i in range(3, _b - 2))


def publickey(sk):
    """Deriva la clave pública (32 bytes) de una semilla secreta de 32 bytes."""
    if len(sk) != 32:
        raise ValueError("la semilla Ed25519 debe medir 32 bytes")
    h = _sha512(sk)
    a = _secret_scalar(h)
    A = _scalarmult(_B, a)
    return _encodepoint(A)


def signature(m, sk, pk):
    """Firma `m` (bytes) con la semilla `sk` (32B) y su pública `pk` (32B). 64 bytes."""
    h = _sha512(sk)
    a = _secret_scalar(h)
    r = _Hint(h[_b // 8:_b // 4] + m)
    R = _scalarmult(_B, r)
    S = (r + _Hint(_encodepoint(R) + pk + m) * a) % _L
    return _encodepoint(R) + _encodeint(S)


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodeint(s):
    return sum(2 ** i * _bit(s, i) for i in range(0, _b))


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, _b - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, _b - 1):
        x = _q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("punto decodificado fuera de la curva")
    return P


def checkvalid(sig, m, pk):
    """True sii `sig` (64B) es una firma Ed25519 válida de `m` bajo `pk` (32B)."""
    if len(sig) != 64 or len(pk) != 32:
        return False
    try:
        R = _decodepoint(sig[:_b // 8])
        A = _decodepoint(pk)
        S = _decodeint(sig[_b // 8:_b // 4])
    except (ValueError, IndexError):
        return False
    h = _Hint(_encodepoint(R) + pk + m)
    return _scalarmult(_B, S) == _edwards(R, _scalarmult(A, h))
