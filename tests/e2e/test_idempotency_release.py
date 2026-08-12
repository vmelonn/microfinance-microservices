"""
When a claimed idempotency key is given back, and when it must not be.

Found by driving the console: a purchase failed because merchant:demo was
not registered, and every retry with that key then returned 409 "This request
is already being processed". The gateway claims a key before doing any work
and stores a response on success, but nothing released the claim when the
work FAILED, so the key stayed claimed for its full 24 hour TTL. Nothing had
happened, and the customer was locked out of their own key.

The asymmetry is the reason this file exists rather than a one-line fix:

    released when it should not be    a retry double-processes, and on the
                                      purchase path that is a second real
                                      authorisation. Money moves twice.

    held when it could be released    the key is stuck and the customer
                                      starts a new one.

The second is an annoyance. The first is a financial incident. So the tests
below care much more about the HELD cases than the released one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[2] / "services" / "api-gateway"
sys.path.insert(0, str(GATEWAY))

from app.idempotency import InMemoryIdempotencyStore  # noqa: E402


@pytest.fixture
def store():
    return InMemoryIdempotencyStore()


def test_a_released_key_can_be_claimed_again(store):
    """The bug, directly. Without release the second claim is in_progress."""
    assert store.claim("k1", "hash-a").status == "new"
    assert store.claim("k1", "hash-a").status == "in_progress"

    store.release("k1")

    assert store.claim("k1", "hash-a").status == "new"


def test_releasing_an_unknown_key_is_harmless(store):
    """It runs on an error path, where the claim may already be gone. A
    release that can fail is not much use on an error path."""
    store.release("never-seen")
    assert store.claim("never-seen", "h").status == "new"


def test_release_does_not_resurrect_a_cached_response(store):
    """A released key is genuinely fresh, not a duplicate holding a stale
    answer from the attempt that failed."""
    store.claim("k2", "hash-a")
    store.store_response("k2", {"status": "approved", "rrn": "111"})
    store.release("k2")

    outcome = store.claim("k2", "hash-a")

    assert outcome.status == "new"
    assert outcome.cached_response is None


# ---------------------------------------------------------------------------
# The rule the gateway applies
# ---------------------------------------------------------------------------

class Spy:
    def __init__(self):
        self.released = []

    def release(self, key):
        self.released.append(key)


class State:
    def __init__(self):
        self.idempotency = Spy()


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_a_4xx_gives_the_key_back(status_code):
    """
    Every 4xx from the saga is decided before the switch is contacted: an
    unregistered card, an unregistered merchant, a card the caller does not
    own. Nothing happened, so the key is safe to reuse.
    """
    from app.main import _release_on_definite_failure
    from mfcommon.http.client import ServiceRejectedError

    state = State()
    _release_on_definite_failure(
        state, "key-1", ServiceRejectedError("transaction-service", status_code, "no"))

    assert state.idempotency.released == ["key-1"]


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_a_5xx_keeps_the_claim(status_code):
    """
    THE test in this file. A 5xx is exactly the case where the switch may
    already hold an authorisation, so the claim must survive: releasing it
    would let a retry authorise a second time.
    """
    from app.main import _release_on_definite_failure
    from mfcommon.http.client import ServiceRejectedError

    state = State()
    _release_on_definite_failure(
        state, "key-1", ServiceRejectedError("transaction-service", status_code, "boom"))

    assert state.idempotency.released == [], (
        "a 5xx released the key, so a retry could authorise a second time"
    )


def test_the_timeout_path_never_reaches_the_release_helper():
    """
    ServiceCallError, a timeout or an unreachable service, is the most
    dangerous case of all and is handled by a separate except branch that
    does not call this helper at all. Pinned so that a later refactor
    collapsing the two branches has to fail a test first.
    """
    import inspect

    from app import main

    source = inspect.getsource(main)
    for handler in ("purchase", "topup", "transfer"):
        fn = inspect.getsource(getattr(main, handler))
        call_error = fn.index("except ServiceCallError")
        release_calls = [
            i for i in range(len(fn))
            if fn.startswith("_release_on_definite_failure", i)
        ]
        assert all(i < call_error for i in release_calls), (
            f"{handler} releases the idempotency key on a ServiceCallError, "
            f"which is the one case where the switch may already have "
            f"authorised"
        )
    assert "def _release_on_definite_failure" in source
