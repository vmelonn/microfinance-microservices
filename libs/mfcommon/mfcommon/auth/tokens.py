"""
A minimal JWT implementation, HS256 only, no external dependency.

A JWT is three base64url-encoded, dot-separated parts:
    header.payload.signature

The header and payload are just JSON; the signature is an HMAC-SHA256 over
the literal string "header.payload", using a secret only the server knows.
Anyone can read a JWT's contents (it's not encrypted, just encoded),
what the signature protects against is *tampering*: change one byte of
the payload and the signature no longer matches, so decode_token() below
will reject it.

This is deliberately a small, from-scratch implementation, consistent
with the rest of this project's approach to primitives (BCD packing, PIN
blocks, the mock HSM), understanding exactly what's inside the token
matters more here than the convenience of a library.
"""

import base64
import hashlib
import hmac
import json
import time


class TokenError(Exception):
    """Raised for any invalid, tampered, or expired token."""
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(message: str, secret: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(signature)


def create_token(claims: dict, secret: str, expires_in_seconds: int = 3600) -> str:
    """
    claims: whatever the caller wants embedded (e.g. {"sub": user_id}).
    "exp" (expiry, as a Unix timestamp) is added automatically.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    payload = dict(claims)
    payload["exp"] = int(time.time()) + expires_in_seconds
    payload["iat"] = int(time.time())

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}"
    signature_b64 = _sign(signing_input, secret)

    return f"{signing_input}.{signature_b64}"


def decode_token(token: str, secret: str) -> dict:
    """Verifies the signature and expiry, then returns the claims. Raises TokenError otherwise."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        raise TokenError("Malformed token, expected header.payload.signature")

    signing_input = f"{header_b64}.{payload_b64}"
    expected_signature = _sign(signing_input, secret)

    # constant-time comparison, same reasoning as password verification
    if not hmac.compare_digest(expected_signature, signature_b64):
        raise TokenError("Signature does not match, token is invalid or was tampered with")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        raise TokenError("Malformed token payload")

    if payload.get("exp", 0) < time.time():
        raise TokenError("Token has expired")

    return payload
