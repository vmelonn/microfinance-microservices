"""
Masking and circuit-breaker tests.

The masking tests exist because the failure they guard against is silent:
nothing breaks when a PIN block gets logged. It just sits in a log
aggregator until someone finds it.
"""

import time

from mfcommon.http.client import CircuitBreaker
from mfcommon.observability.audit import (
    NEVER_LOG,
    mask_fields,
    mask_pan,
    mask_payload,
)


def test_pin_block_and_mac_never_survive_masking():
    fields = {
        2: "4532015112830366",
        4: "000000005000",
        39: "00",
        52: "\x8f\x21\x00\xff\x9a\x0d\x1e\x7c",  # binary PIN block
        53: "0000000000000001",
        64: "\x11\x22\x33\x44\x55\x66\x77\x88",  # MAC
    }

    masked = mask_fields(fields)

    for de in NEVER_LOG:
        if de in fields:
            assert masked[de] == "<redacted>", f"DE {de} leaked through masking"
    # The raw secret must not appear anywhere in the serialised output.
    assert "\x8f\x21" not in str(masked)


def test_redacted_fields_are_marked_present_not_dropped():
    """Dropping the key makes the log claim no PIN block was sent, which is
    a lie that matters when reconstructing a disputed transaction."""
    masked = mask_fields({52: "secret-block", 39: "00"})
    assert 52 in masked
    assert masked[52] == "<redacted>"


def test_pan_keeps_its_last_four():
    assert mask_fields({2: "4532015112830366"})[2] == "...0366"
    assert mask_pan("4532015112830366") == "...0366"
    assert mask_pan(None) == "-"


def test_track_data_is_masked_because_it_contains_the_full_pan():
    """DE 35/45 embed the PAN. Masking DE 2 while logging track data in full
    would be security theatre."""
    masked = mask_fields({35: "4532015112830366=25121010000012300000"})
    assert "4532015112830366" not in masked[35]


def test_json_payload_masking_covers_the_rest_hops():
    """Between services the same secrets travel as JSON, not ISO 8583, and
    are just as easy to log by accident."""
    payload = {
        "card_number": "4532015112830366",
        "pin": "1234",
        "amount": 50.0,
        "nested": {"password": "hunter2", "sender_pin": "9999"},
        "items": [{"access_token": "eyJhbGciOi..."}],
    }

    safe = mask_payload(payload)

    assert safe["card_number"] == "...0366"
    assert safe["pin"] == "<redacted>"
    assert safe["amount"] == 50.0
    assert safe["nested"]["password"] == "<redacted>"
    assert safe["nested"]["sender_pin"] == "<redacted>"
    assert safe["items"][0]["access_token"] == "<redacted>"
    assert "hunter2" not in str(safe)
    assert "1234" not in str(safe)


def test_breaker_opens_only_after_the_threshold():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=60)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is False, "opened early, one blip would stop all traffic"

    breaker.record_failure()
    assert breaker.is_open is True


def test_success_resets_the_failure_count():
    """Failures must be CONSECUTIVE. Counting them cumulatively means a
    healthy service that has been up for a month eventually trips on
    unrelated blips."""
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is False


def test_breaker_half_opens_after_the_cooldown():
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=0.1)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True

    time.sleep(0.15)

    # First read after the cooldown lets one probe through...
    assert breaker.is_open is False
    # ...and a single further failure re-opens it immediately, rather than
    # requiring the full threshold again. A still-broken downstream should
    # not receive threshold-many more doomed calls per cooldown window.
    breaker.record_failure()
    assert breaker.is_open is True
