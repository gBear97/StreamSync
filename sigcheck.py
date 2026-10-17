"""Ed25519 signature verification, standard library only.

Every release publishes SHA256SUMS and, beside it, SHA256SUMS.sig: an
Ed25519 signature over the exact bytes of SHA256SUMS, made in CI with
the project's release key. The updater checks it against the public key
built into the app before it trusts a single checksum, so an update has
to come from whoever holds that key - not merely arrive intact. On
Windows, where releases carry no Authenticode signature, it is the only
such proof.

Verification only, never signing: the private key lives in a repository
secret and nowhere in this code. This is the reference algorithm of
RFC 8032 (section 6) in plain integers - slow by native standards, a few
milliseconds for one signature, which is all an update needs - kept to
the standard library so the updater stays importable anywhere.
"""

import hashlib

# Curve25519 in twisted Edwards form, as RFC 8032 section 5.1 defines it.
P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493   # group order
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _inv(x):
    return pow(x, P - 2, P)


def _add(a, b):
    """Point addition in extended coordinates (X, Y, Z, T)."""
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % P
    pb = (y1 + x1) * (y2 + x2) % P
    pc = 2 * t1 * t2 * D % P
    pd = 2 * z1 * z2 % P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, point):
    q = (0, 1, 1, 0)                   # the neutral element
    while s > 0:
        if s & 1:
            q = _add(q, point)
        point = _add(point, point)
        s >>= 1
    return q


def _equal(a, b):
    # x1/z1 == x2/z2 and y1/z1 == y2/z2, without dividing
    return ((a[0] * b[2] - b[0] * a[2]) % P == 0
            and (a[1] * b[2] - b[1] * a[2]) % P == 0)


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def _decompress(s):
    """The point a 32-byte encoding names, or None if it names none."""
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)       # the base point


def verify(public_key, message, signature):
    """True only if `signature` is a valid Ed25519 signature of `message`
    by the holder of `public_key` (32 bytes). Anything malformed is
    simply not valid: this never raises on bad input."""
    if (not isinstance(public_key, (bytes, bytearray))
            or not isinstance(signature, (bytes, bytearray))
            or len(public_key) != 32 or len(signature) != 64):
        return False
    a = _decompress(public_key)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(
        bytes(signature[:32]) + bytes(public_key) + bytes(message)).digest(),
        "little") % L
    return _equal(_mul(s, G), _add(r, _mul(h, a)))
