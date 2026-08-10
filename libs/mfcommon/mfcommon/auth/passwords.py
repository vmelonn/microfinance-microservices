"""
Password hashing for app login -- deliberately separate from anything
card/PIN related. A card PIN is verified by the switch, over ISO 8583, at
transaction time. A login password is verified by us, locally, before a
request is even built. Conflating the two would mean one compromised
secret exposes the other.

Uses PBKDF2-HMAC-SHA256 (in Python's standard library, no extra
dependency) with a random salt per password and a high iteration count,
so even if the stored hashes ever leaked, brute-forcing them back into
plaintext passwords is deliberately slow.
"""

import hashlib
import hmac
import os

_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Returns 'salt_hex$hash_hex' -- both parts needed later to verify."""
    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Recomputes the hash using the SAME salt that was stored, and compares
    in constant time -- comparing hash strings with a plain == would leak
    timing information about how many leading bytes matched, which is
    exactly the kind of side channel a real auth system has to avoid.
    """
    if not stored_hash or "$" not in stored_hash:
        return False  # covers the migrated '' placeholder -- never a valid password
    salt_hex, expected_hex = stored_hash.split("$", 1)
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(derived.hex(), expected_hex)
