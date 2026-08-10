"""
transaction-service -- the saga orchestrator.

In the monolith, a purchase was one function calling risk, then security,
then the switch, then the ledger, inside a single process. If anything threw,
the whole thing unwound and nothing had happened.

That guarantee is gone. Each of those steps is now a network call to a
separate database, and there is no distributed transaction to roll them
back together. What replaces it is an explicit saga: an ordered sequence of
steps, each with a named compensating action, and a coordinator that knows
which compensations to run when a step fails.

THE ORDER IS CHOSEN, NOT ARBITRARY. Steps are sequenced cheapest-and-most-
reversible first:

  1. resolve + authorize ownership  (read-only, free to abandon)
  2. risk evaluation                (has a side effect on velocity, but no money)
  3. ISO 8583 authorization         (MOVES MONEY at the switch -- the point of no return)
  4. ledger posting                 (records what step 3 did)

Everything reversible happens before the irreversible step. Only step 4
follows it, and step 4 is idempotent on RRN, which is what makes retrying
it safe.

THE HARD CASE is step 3 succeeding and step 4 failing: the switch has
approved and the cardholder is debited, but our books do not show it. There
is no clean unwind. The compensation is to reverse at the switch, and if
that also fails, to record a reconciliation exception for a human. This is
the situation the monolith could not produce at all, and the main thing the
decomposition costs.
"""

from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.http.client import ServiceCallError, ServiceClient, ServiceRejectedError
from mfcommon.observability.audit import mask_pan
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)

RISK_URL = os.environ.get("RISK_SERVICE_URL", "http://risk-service:8083")
LEDGER_URL = os.environ.get("LEDGER_SERVICE_URL", "http://ledger-service:8084")
ADAPTER_URL = os.environ.get("ISO8583_ADAPTER_URL", "http://iso8583-adapter:8085")

# The merchant every demo purchase credits. In a real acquirer this arrives
# on the transaction as DE 42 (merchant ID); the monolith hardcoded it too,
# and it is called out here rather than hidden so nobody mistakes it for a
# modelled concept.
MERCHANT_IDENTIFIER = os.environ.get("MERCHANT_IDENTIFIER", "merchant:demo")

log = configure_logging("transaction-service", os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.risk = ServiceClient("risk-service", RISK_URL, timeout=3.0)
    app.state.ledger = ServiceClient("ledger-service", LEDGER_URL, timeout=5.0)
    # The adapter's timeout must EXCEED the switch timeout it wraps,
    # otherwise this service gives up first and reports "unknown" for
    # transactions the adapter was about to resolve cleanly -- manufacturing
    # ambiguity that did not exist.
    app.state.adapter = ServiceClient("iso8583-adapter", ADAPTER_URL, timeout=30.0)
    yield
    for client in (app.state.risk, app.state.ledger, app.state.adapter):
        client.close()


app = FastAPI(title="Transaction Service", lifespan=lifespan)


@app.middleware("http")
async def adopt_correlation_id(request: Request, call_next):
    incoming = request.headers.get(CORRELATION_HEADER)
    if incoming:
        set_correlation_id(incoming)
    return await call_next(request)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class PurchaseRequest(BaseModel):
    user_id: str
    card_number: str = Field(..., min_length=12, max_length=19)
    pin: str = Field(..., min_length=4, max_length=12)
    amount_cents: int = Field(..., gt=0)
    entry_mode: str = "05"


class TransferRequest(BaseModel):
    user_id: str
    sender_card_number: str = Field(..., min_length=12, max_length=19)
    sender_pin: str = Field(..., min_length=4, max_length=12)
    recipient_account: str
    amount_cents: int = Field(..., gt=0)


class TransactionResponse(BaseModel):
    status: str                       # approved | declined | review | unknown | error
    reason: str | None = None
    rrn: str | None = None
    stan: str | None = None
    authorization_id: str | None = None
    ledger_status: str | None = None
    # Set when the switch outcome could not be determined. A client seeing
    # this must NOT retry blindly -- the original may have succeeded.
    requires_reconciliation: bool = False


def _generate_rrn() -> str:
    """
    12 characters: 10 digits of epoch seconds plus 2 random.

    Collision risk is real but bounded -- two transactions in the same second
    have a 1-in-100 chance of colliding, and the ledger's PRIMARY KEY turns a
    collision into a rejected duplicate rather than corrupted money. Ported
    unchanged from the monolith deliberately, with the flaw documented rather
    than quietly improved, because changing the RRN format is a switch
    integration decision, not a code cleanup.
    """
    return f"{int(time.time()) % 10**10:010d}{secrets.randbelow(100):02d}"


# --------------------------------------------------------------------------
# The saga
# --------------------------------------------------------------------------

@app.post("/internal/transactions/purchase", response_model=TransactionResponse)
def purchase(body: PurchaseRequest, request: Request):
    state = request.app.state

    # -- Step 1: resolve both sides, and prove the caller owns the debit side.
    # Read-only. Failing here costs nothing and touches no money.
    sender = _resolve_or_404(state, body.card_number, "Sender card is not registered.")
    merchant = _resolve_or_404(state, MERCHANT_IDENTIFIER, f"Merchant '{MERCHANT_IDENTIFIER}' is not registered.")

    if sender.get("user_id") != body.user_id:
        # Authentication proved WHO is calling; this proves they are allowed
        # to debit THIS card. Without it, any valid token could spend from
        # any card whose number the holder happened to know.
        raise HTTPException(status_code=403, detail="You are not authorized to use this card.")

    # -- Step 2: risk. Never retried: evaluating records an attempt, so a
    # retry inflates the caller's own velocity toward a decline.
    try:
        decision = state.risk.post(
            "/internal/risk/evaluate",
            {
                "card_number": body.card_number,
                "amount_cents": body.amount_cents,
                "entry_mode": body.entry_mode,
            },
            retries=0,
        )
    except ServiceCallError as exc:
        # Fail CLOSED. A risk service that is down must not become a risk
        # service that approves everything -- that turns an outage into an
        # open fraud window.
        log.error(f"risk-service unavailable, failing closed: {exc}")
        raise HTTPException(status_code=503, detail="Risk evaluation unavailable; transaction refused.")

    if decision["outcome"] in ("decline", "review"):
        reason = "; ".join(decision["reasons"])
        log.warning(f"risk {decision['outcome']} card={mask_pan(body.card_number)}: {reason}")
        return TransactionResponse(status=decision["outcome"], reason=reason)

    # -- Step 3: authorize at the switch. THE POINT OF NO RETURN.
    rrn = _generate_rrn()
    auth = _authorize(
        state,
        rrn=rrn,
        card_number=body.card_number,
        pin=body.pin,
        amount_cents=body.amount_cents,
        entry_mode=body.entry_mode,
        processing_code="000000",
    )

    if auth["outcome"] == "unknown":
        # Money may or may not have moved. Do NOT post to the ledger: a
        # posting for a transaction that never happened is worse than a
        # missing posting for one that did, because the second is caught by
        # daily reconciliation and the first silently invents money.
        log.error(f"UNKNOWN outcome rrn={rrn} -- no ledger posting, reconciliation required")
        return TransactionResponse(
            status="unknown",
            reason=auth.get("response_text") or "Switch outcome could not be determined.",
            rrn=rrn,
            requires_reconciliation=True,
        )

    if auth["outcome"] != "approved":
        return TransactionResponse(
            status="declined",
            reason=auth.get("response_text") or "Declined by the issuer.",
            rrn=auth.get("rrn", rrn),
            stan=auth.get("stan"),
        )

    # -- Step 4: record it. Idempotent on RRN, so retries are safe.
    confirmed_rrn = auth.get("rrn") or rrn
    ledger_status = _post_to_ledger(
        state,
        rrn=confirmed_rrn,
        debit=sender["account_id"],
        credit=merchant["account_id"],
        amount_cents=body.amount_cents,
        kind="purchase",
        # For the compensation path, if the posting cannot be made to stick.
        card_number=body.card_number,
        stan=auth.get("stan"),
    )

    return TransactionResponse(
        status="approved",
        reason=auth.get("response_text"),
        rrn=confirmed_rrn,
        stan=auth.get("stan"),
        authorization_id=auth.get("authorization_id"),
        ledger_status=ledger_status["status"],
        requires_reconciliation=ledger_status["requires_reconciliation"],
    )


@app.post("/internal/transactions/transfer", response_model=TransactionResponse)
def transfer(body: TransferRequest, request: Request):
    """Same saga, different processing code (DE 3 = 400000) and a real
    recipient account instead of the demo merchant."""
    state = request.app.state

    sender = _resolve_or_404(state, body.sender_card_number, "Sender card is not registered.")
    recipient = _resolve_or_404(state, body.recipient_account, "Recipient is not registered.")

    if sender.get("user_id") != body.user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to use this card.")
    if sender["account_id"] == recipient["account_id"]:
        # A self-transfer would post a debit and a credit to the same
        # account: the ledger still balances, the balance is unchanged, and
        # the row is pure noise. Reject it rather than record it.
        raise HTTPException(status_code=400, detail="Sender and recipient are the same account.")

    try:
        decision = state.risk.post(
            "/internal/risk/evaluate",
            {"card_number": body.sender_card_number, "amount_cents": body.amount_cents, "entry_mode": "01"},
            retries=0,
        )
    except ServiceCallError as exc:
        log.error(f"risk-service unavailable, failing closed: {exc}")
        raise HTTPException(status_code=503, detail="Risk evaluation unavailable; transaction refused.")

    if decision["outcome"] in ("decline", "review"):
        return TransactionResponse(status=decision["outcome"], reason="; ".join(decision["reasons"]))

    rrn = _generate_rrn()
    auth = _authorize(
        state,
        rrn=rrn,
        card_number=body.sender_card_number,
        pin=body.sender_pin,
        amount_cents=body.amount_cents,
        entry_mode="01",
        processing_code="400000",
        credit_account=recipient["account_id"],
    )

    if auth["outcome"] == "unknown":
        return TransactionResponse(
            status="unknown",
            reason=auth.get("response_text") or "Switch outcome could not be determined.",
            rrn=rrn,
            requires_reconciliation=True,
        )
    if auth["outcome"] != "approved":
        return TransactionResponse(
            status="declined", reason=auth.get("response_text"), rrn=rrn, stan=auth.get("stan")
        )

    confirmed_rrn = auth.get("rrn") or rrn
    ledger_status = _post_to_ledger(
        state,
        rrn=confirmed_rrn,
        debit=sender["account_id"],
        credit=recipient["account_id"],
        amount_cents=body.amount_cents,
        kind="transfer",
        card_number=body.sender_card_number,
        stan=auth.get("stan"),
    )

    return TransactionResponse(
        status="approved",
        reason=auth.get("response_text"),
        rrn=confirmed_rrn,
        stan=auth.get("stan"),
        authorization_id=auth.get("authorization_id"),
        ledger_status=ledger_status["status"],
        requires_reconciliation=ledger_status["requires_reconciliation"],
    )


# --------------------------------------------------------------------------
# Saga steps
# --------------------------------------------------------------------------

def _resolve_or_404(state, identifier: str, message: str) -> dict:
    try:
        # Safe to retry: pure lookup, no side effects.
        return state.ledger.post("/internal/ledger/resolve", {"identifier": identifier}, retries=2)
    except ServiceRejectedError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")


def _authorize(state, **kwargs) -> dict:
    payload = {
        "rrn": kwargs["rrn"],
        "card_number": kwargs["card_number"],
        "pin": kwargs["pin"],
        "amount_cents": kwargs["amount_cents"],
        "entry_mode": kwargs["entry_mode"],
        "processing_code": kwargs["processing_code"],
    }
    if kwargs.get("credit_account"):
        payload["credit_account"] = kwargs["credit_account"]

    try:
        # retries=0, emphatically. This is the step that moves money. An
        # automatic retry of an authorization whose response was merely lost
        # is a second, real authorization -- the cardholder is debited twice
        # and only one is ever recorded.
        return state.adapter.post("/internal/iso8583/authorize", payload, retries=0)
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        # The adapter itself was unreachable or timed out. Same ambiguity
        # class as a switch timeout, and treated identically.
        log.error(f"adapter call failed rrn={kwargs['rrn']}: {exc}")
        return {
            "outcome": "unknown",
            "response_text": f"ISO 8583 adapter unreachable: {exc}",
            "rrn": kwargs["rrn"],
        }


def _post_to_ledger(state, *, rrn, debit, credit, amount_cents, kind, card_number, stan) -> dict:
    """
    Step 4, plus its compensation.

    Retried, because the posting is idempotent on RRN -- a retry after a lost
    response finds the row already there and reports "already_recorded"
    rather than double-posting. This is the payoff for the PRIMARY KEY.
    """
    try:
        result = state.ledger.post(
            "/internal/ledger/postings",
            {
                "rrn": rrn,
                "debit_account": debit,
                "credit_account": credit,
                "amount_cents": amount_cents,
                "kind": kind,
            },
            retries=3,
        )
        return {"status": result["status"], "requires_reconciliation": False}

    except (ServiceCallError, ServiceRejectedError) as exc:
        # THE HARD CASE. The switch approved; our books cannot record it.
        log.error(
            f"LEDGER POSTING FAILED after an APPROVED authorization. rrn={rrn} "
            f"amount_cents={amount_cents} card={mask_pan(card_number)}: {exc}. "
            f"Attempting compensating reversal."
        )
        compensated = _compensate_with_reversal(state, rrn, amount_cents, card_number, stan)

        if compensated:
            return {"status": "reversed", "requires_reconciliation": False}

        # Both the posting and its compensation failed. Nothing automated
        # can fix this; the daily reconciliation job is the backstop and a
        # human has to act on it.
        log.error(
            f"RECONCILIATION EXCEPTION rrn={rrn}: authorized at the switch, not "
            f"recorded in the ledger, and the reversal was not acknowledged."
        )
        return {"status": "reconciliation_required", "requires_reconciliation": True}


def _compensate_with_reversal(state, rrn, amount_cents, card_number, stan) -> bool:
    try:
        state.adapter.post(
            "/internal/iso8583/reverse",
            {
                "rrn": rrn,
                "original_stan": stan or "000000",
                "original_mti": "0200",
                "amount_cents": amount_cents,
                "card_number": card_number,
            },
            retries=2,  # reversals ARE idempotent at the switch
        )
        log.warning(f"compensating reversal acknowledged rrn={rrn}")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error(f"compensating reversal FAILED rrn={rrn}: {exc!r}")
        return False


@app.get("/health")
def health():
    return {"status": "ok", "service": "transaction-service"}


@app.get("/ready")
def ready(request: Request):
    """
    Reports which downstreams have an open circuit breaker, but stays READY
    regardless.

    Deliberate: this service is not itself broken when a downstream is, and
    marking it not-ready would remove the pod that returns the correct
    "risk unavailable, refused" answer. Readiness means "can serve
    requests", not "will every request succeed".
    """
    state = request.app.state
    return {
        "status": "ready",
        "breakers": {
            "risk-service": "open" if state.risk.breaker.is_open else "closed",
            "ledger-service": "open" if state.ledger.breaker.is_open else "closed",
            "iso8583-adapter": "open" if state.adapter.breaker.is_open else "closed",
        },
    }
