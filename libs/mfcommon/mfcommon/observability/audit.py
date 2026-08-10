"""
Audit masking -- ported from the monolith's ops/audit_log.py, with one
change that the decomposition forces.

In the monolith, exactly one place logged ISO 8583 messages: switch/client.py.
Masking there covered everything, because nothing else ever saw a raw
message. Across seven services that assumption is gone -- iso8583-adapter,
ace-stub, and (once entitled) ACE itself all handle messages containing
DE 52, and any of the three logging carelessly writes a PIN block to a log
aggregator that probably retains it for a year.

So the masking rules live here, in shared code, and every service that
touches a message imports them. The rule is not "remember to mask" -- it is
"there is one function, and it is the only way messages get logged."

NEVER_LOG vs MASK_LAST4 is a real distinction, not defensive
over-engineering: a PAN's last four digits are printed on receipts and are
genuinely needed to answer support questions, so truncation preserves
utility. A PIN block has no safe fragment -- eight bytes is the entire
secret, and any part of it is a head start on the rest.
"""

from __future__ import annotations

# PIN block, security control info (carries the KSN needed to derive the
# PIN key), EMV cryptogram data, and the three MAC fields. No fragment of
# any of these is safe to retain.
NEVER_LOG = {52, 53, 55, 64, 96, 128}

# Truncated to the last four digits -- still useful for support, useless to
# an attacker.
MASK_LAST4 = {2, 34, 35, 45}  # PAN, extended PAN, track 2, track 1

_REDACTED = "<redacted>"


def mask_fields(fields: dict) -> dict:
    """Returns a copy of an ISO 8583 field dict that is safe to persist."""
    masked = {}
    for de, value in fields.items():
        if de in NEVER_LOG:
            # Recorded as present-but-redacted rather than dropped. Knowing a
            # PIN block WAS present matters when reconstructing a failed
            # transaction; silently omitting the key makes the log lie about
            # what was on the wire.
            masked[de] = _REDACTED
            continue
        if de in MASK_LAST4 and isinstance(value, str) and len(value) > 4:
            masked[de] = f"...{value[-4:]}"
        else:
            masked[de] = value
    return masked


def mask_pan(pan: str | None) -> str:
    """For log lines and JSON payloads that carry a bare card number."""
    if not pan:
        return "-"
    return f"...{pan[-4:]}" if len(pan) > 4 else "..."


# Keys that must never appear in a logged REST/JSON payload. The monolith
# only had to worry about ISO 8583 fields; a microservice platform passes
# the same secrets around as JSON between services, where they are just as
# loggable and rather easier to leak by accident.
SENSITIVE_JSON_KEYS = {
    "pin", "sender_pin", "password", "new_password", "pin_block", "pin_block_hex",
    "pinBlockHex", "access_token", "authorization", "password_hash", "ksn",
}


def mask_payload(payload: dict) -> dict:
    """Recursively redacts sensitive keys from a JSON-shaped dict."""
    safe = {}
    for key, value in payload.items():
        if key in SENSITIVE_JSON_KEYS:
            safe[key] = _REDACTED
        elif key in ("card_number", "sender_card_number", "pan"):
            safe[key] = mask_pan(value) if isinstance(value, str) else value
        elif isinstance(value, dict):
            safe[key] = mask_payload(value)
        elif isinstance(value, list):
            safe[key] = [mask_payload(v) if isinstance(v, dict) else v for v in value]
        else:
            safe[key] = value
    return safe
