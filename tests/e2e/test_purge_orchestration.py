"""
The gateway half of a platform wipe.

Two services, two databases, no distributed transaction. What matters is not
the happy path but what the operator is told when only one half lands,
because the state that leaves behind looks fine and is not:

    ledger only   logins work, everything they touch 404s
    auth only     accounts nobody can reach, phone numbers still taken

Both are fixed by running it again, so the response has to say which half
failed rather than collapsing to a boolean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

GATEWAY = Path(__file__).resolve().parents[2] / "services" / "api-gateway"
sys.path.insert(0, str(GATEWAY))


class StubClient:
    """A ServiceClient that answers however the test needs."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def post(self, path, body, retries=0):
        self.calls.append(path)
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


@pytest.fixture
def app_with(monkeypatch):
    def build(ledger, auth):
        from app.console import router

        app = FastAPI()
        app.include_router(router)          # no auth dependency, tested elsewhere
        app.state.ledger = StubClient(ledger)
        app.state.auth = StubClient(auth)
        return app, TestClient(app, raise_server_exceptions=False)
    return build


def test_both_halves_are_reported_on_success(app_with):
    app, client = app_with(
        {"status": "purged", "accounts": 4, "cards": 4,
         "transactions": 9, "ledger_entries": 18},
        {"status": "purged", "users": 4},
    )

    response = client.post("/console/purge")

    assert response.status_code == 200
    body = response.json()
    assert body["ledger"]["accounts"] == 4
    assert body["auth"]["users"] == 4
    assert app.state.ledger.calls == ["/internal/ledger/purge"]
    assert app.state.auth.calls == ["/internal/auth/purge"]


def test_the_second_half_is_attempted_even_when_the_first_fails(app_with):
    """
    Stopping at the first failure would leave the operator further from an
    empty platform AND still needing to work out what happened. Both halves
    are idempotent, so attempting both is free.
    """
    from mfcommon.http.client import ServiceCallError

    app, client = app_with(ServiceCallError("ledger-service", "connection refused"),
                           {"status": "purged", "users": 2})

    response = client.post("/console/purge")

    assert app.state.auth.calls == ["/internal/auth/purge"], \
        "auth was skipped because ledger failed"
    assert response.status_code == 207


def test_a_partial_purge_names_the_half_that_failed(app_with):
    from mfcommon.http.client import ServiceCallError

    app, client = app_with({"status": "purged", "accounts": 1},
                           ServiceCallError("auth-service", "connection refused"))

    detail = client.post("/console/purge").json()["detail"]

    assert detail["error"] == "partial_purge"
    assert detail["failed"] == ["auth"]
    assert "again" in detail["message"], "the operator is not told the fix"
    assert detail["detail"]["ledger"]["ok"] is True


def test_a_disabled_gate_is_reported_as_such(app_with):
    """The common case in a non-dev environment: the flag is off. That is a
    403 from the service, not an outage, and should not read like one."""
    from mfcommon.http.client import ServiceRejectedError

    app, client = app_with(
        ServiceRejectedError("ledger-service", 403, "Ledger purge is disabled in this environment."),
        ServiceRejectedError("auth-service", 403, "User purge is disabled in this environment."),
    )

    detail = client.post("/console/purge").json()["detail"]

    assert sorted(detail["failed"]) == ["auth", "ledger"]
    assert detail["detail"]["ledger"]["status_code"] == 403
    assert "disabled" in detail["detail"]["ledger"]["error"]
