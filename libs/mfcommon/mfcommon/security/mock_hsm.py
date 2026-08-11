"""
Mock HSM: exposes the same interface a real Hardware Security Module client
would (encrypt a PIN block, verify a MAC), backed by ordinary software keys
instead of tamper-resistant hardware.

This is a LEARNING STAND-IN ONLY. The encryption here (XOR with a derived
key) is not cryptographically secure, a real HSM uses 3DES/AES and never
lets the key leave dedicated hardware. Never use real PINs, real cards, or
this code anywhere near production.

Includes a simplified DUKPT-style key derivation: instead of one fixed key
used forever, each transaction gets its own key, derived from a shared base
key plus an incrementing counter (a stand-in for a Key Serial Number). Both
sides can independently re-derive the same per-transaction key from the
base key and the counter, so a single leaked transaction key reveals
nothing about any other transaction.
"""

import hashlib
import hmac
import os

from mfcommon.security.pin_block import build_iso0_pin_block


class MockHSM:
    def __init__(self, base_key: bytes = None, kms=None, key_id: str = "hsm-base-key",
                 persisted_key_path: str = None):
        """
        Three ways to get a base_key, in priority order:

        1. base_key passed explicitly, used as-is. This is what every
           existing caller and test already does; behavior is unchanged.

        2. kms + persisted_key_path given, the base_key is now genuinely
           persistent across restarts, which os.urandom() alone never was.
           On first run, a fresh key is generated via the KMS and its
           ENCRYPTED form is written to persisted_key_path (safe to store,
           since only the KMS's master key can ever decrypt it back). On
           every subsequent run, the encrypted key is read back and
           decrypted through the KMS, the actual key material never
           touches disk in plaintext.

        3. Neither given, falls back to the original behavior:
           os.urandom(16), regenerated fresh every process start. Fine for
           a single-process demo; anything encrypted under this key becomes
           permanently undecryptable the moment the process restarts,
           which is exactly the real problem option 2 exists to fix.
        """
        if base_key is not None:
            self.base_key = base_key
        elif kms is not None and persisted_key_path is not None:
            self.base_key = self._load_or_create_persistent_key(kms, key_id, persisted_key_path)
        else:
            self.base_key = os.urandom(16)

        self._ksn_counter = 0

    @staticmethod
    def _load_or_create_persistent_key(kms, key_id: str, persisted_key_path: str) -> bytes:
        if os.path.exists(persisted_key_path):
            with open(persisted_key_path, "rb") as f:
                encrypted_key = f.read()
            return kms.decrypt_data_key(encrypted_key, key_id)

        plaintext_key, encrypted_key = kms.generate_data_key(key_id)
        with open(persisted_key_path, "wb") as f:
            f.write(encrypted_key)
        return plaintext_key

    def _next_ksn(self) -> str:
        """KSN = Key Serial Number, a stand-in for DUKPT's real counter scheme."""
        self._ksn_counter += 1
        return f"{self._ksn_counter:010d}"

    def _derive_transaction_key(self, ksn: str) -> bytes:
        """
        Simplified stand-in for real DUKPT key derivation (not the actual
        ANSI X9.24 algorithm), but the same core idea: a unique key per
        transaction, derived deterministically from a shared secret and a
        counter, so either side can independently compute it.
        """
        return hmac.new(self.base_key, ksn.encode(), hashlib.sha256).digest()[:16]

    def encrypt_pin_block(self, pin: str, pan: str):
        """
        Returns (ksn, encrypted_block). The KSN travels alongside the
        encrypted block in a real message so the receiving side knows which
        per-transaction key to re-derive and decrypt with.
        """
        ksn = self._next_ksn()
        key = self._derive_transaction_key(ksn)
        plaintext_block = build_iso0_pin_block(pin, pan)
        encrypted = self._xor_stream(plaintext_block, key)
        return ksn, encrypted

    def decrypt_pin_block(self, ksn: str, encrypted_block: bytes) -> bytes:
        key = self._derive_transaction_key(ksn)
        return self._xor_stream(encrypted_block, key)  # XOR is its own inverse

    @staticmethod
    def _xor_stream(data: bytes, key: bytes) -> bytes:
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

    def generate_mac(self, message: bytes) -> bytes:
        """Message Authentication Code, proves a message wasn't tampered with in transit."""
        return hmac.new(self.base_key, message, hashlib.sha256).digest()[:8]

    def verify_mac(self, message: bytes, mac: bytes) -> bool:
        expected = self.generate_mac(message)
        return hmac.compare_digest(expected, mac)
