"""
Key management service, following the same swappable-interface pattern as
cache/idempotency_store.py and cache/velocity_tracker.py: one interface,
one fully real and tested implementation, one cloud implementation this
sandbox genuinely cannot reach (no network access to AWS/GCP's control
plane here -- the code is correct and matches boto3's real API shape, but
untestable without real credentials).

The core operation both real cloud KMS services (AWS KMS, Google Cloud
KMS) and this local version expose is envelope encryption:

  generate_data_key(key_id) -> (plaintext_dek, encrypted_dek)
      Generates a fresh AES data key. You get back the RAW key (use it
      immediately, then discard it from memory) and an ENCRYPTED copy,
      safe to store anywhere, since only the KMS holds the master key
      needed to ever decrypt it again.

  decrypt_data_key(encrypted_dek, key_id) -> plaintext_dek
      Asks the KMS to unwrap a previously-encrypted key back to plaintext.

This is the standard pattern real systems use to protect a long-lived
root/master key -- exactly the problem MockHSM's base_key had: it was
just os.urandom(), regenerated fresh every process start, meaning
anything encrypted before a restart became permanently undecryptable.
"""

import os
from abc import ABC, abstractmethod

# NOTE: `cryptography` is imported lazily inside LocalKeyManagementService,
# not at module scope, for the same reason boto3 is deferred inside
# AWSKeyManagementService below -- mfcommon is a shared library imported by
# all seven services, and only iso8583-adapter ever touches key material.
# A module-level import here would make every service in the platform
# install and load a crypto library it never calls.


class KeyManagementService(ABC):
    @abstractmethod
    def generate_data_key(self, key_id: str) -> tuple:
        """Returns (plaintext_dek: bytes, encrypted_dek: bytes)."""
        ...

    @abstractmethod
    def decrypt_data_key(self, encrypted_dek: bytes, key_id: str) -> bytes:
        """Returns the plaintext_dek that was wrapped into encrypted_dek."""
        ...


class LocalKeyManagementService(KeyManagementService):
    """
    A real KMS, running locally -- genuine AES-256-GCM authenticated
    encryption, not the mock XOR the rest of this project's HSM uses
    elsewhere. The master key here is the one thing a real cloud KMS would
    keep in actual tamper-resistant hardware; here it's just a key loaded
    from an environment variable (or generated, for pure local dev), same
    honesty as MockHSM's own doc comments about what it does and doesn't
    achieve.
    """

    def __init__(self, master_key: bytes = None):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._aesgcm_cls = AESGCM
        self._master_key = master_key or os.urandom(32)  # AES-256 needs a 32-byte key

    def generate_data_key(self, key_id: str) -> tuple:
        plaintext_dek = os.urandom(32)
        encrypted_dek = self._wrap(plaintext_dek, key_id)
        return plaintext_dek, encrypted_dek

    def decrypt_data_key(self, encrypted_dek: bytes, key_id: str) -> bytes:
        return self._unwrap(encrypted_dek, key_id)

    def _wrap(self, plaintext_dek: bytes, key_id: str) -> bytes:
        aesgcm = self._aesgcm_cls(self._master_key)
        nonce = os.urandom(12)  # AES-GCM's standard nonce size
        # key_id is bound in as "associated data" -- authenticated but not
        # encrypted, meaning a wrapped key can't be silently swapped to
        # answer for a DIFFERENT key_id than the one it was created under.
        ciphertext = aesgcm.encrypt(nonce, plaintext_dek, key_id.encode("utf-8"))
        return nonce + ciphertext  # nonce prepended, needed again to decrypt

    def _unwrap(self, encrypted_dek: bytes, key_id: str) -> bytes:
        aesgcm = self._aesgcm_cls(self._master_key)
        nonce, ciphertext = encrypted_dek[:12], encrypted_dek[12:]
        return aesgcm.decrypt(nonce, ciphertext, key_id.encode("utf-8"))


class AWSKeyManagementService(KeyManagementService):
    """
    Real AWS KMS integration -- correct API shape, genuinely untestable in
    this environment (no network route to AWS's control plane here, and
    no credentials configured). Activate this by setting AWS credentials
    normally (environment variables, an IAM role, or ~/.aws/credentials)
    and swapping LocalKeyManagementService for this in api/main.py.
    """

    def __init__(self, region_name: str = "us-east-1"):
        import boto3  # imported here, not at module level, so this class
                       # can exist (and be read/reviewed) without boto3
                       # being a hard dependency for everyone who never
                       # touches real AWS
        self._client = boto3.client("kms", region_name=region_name)

    def generate_data_key(self, key_id: str) -> tuple:
        response = self._client.generate_data_key(KeyId=key_id, KeySpec="AES_256")
        return response["Plaintext"], response["CiphertextBlob"]

    def decrypt_data_key(self, encrypted_dek: bytes, key_id: str) -> bytes:
        response = self._client.decrypt(CiphertextBlob=encrypted_dek, KeyId=key_id)
        return response["Plaintext"]
