"""
transaction-service, the saga orchestrator.

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
  3. ISO 8583 authorization         (MOVES MONEY at the switch, the point of no return)
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
from mfcommon.observability import trace
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

TRACE_REDIS_URL = os.environ.get("REDIS_URL")

log = configure_logging("transaction-service", os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Optional request tracing. A no-op without REDIS_URL, so local runs and
    # tests pay nothing and behave identically. Wrapped because tracing is a
    # debug aid and must never stop a service starting.
    if TRACE_REDIS_URL:
        try:
            import redis as _redis

            _tc = _redis.Redis.from_url(TRACE_REDIS_URL)
            _tc.ping()
            trace.configure(_tc, "transaction-service")
            app.state.trace_redis = _tc
            log.info("request tracing enabled")
        except Exception as exc:  # noqa: BLE001
            app.state.trace_redis = None
            log.warning(f"tracing disabled, Redis unreachable: {exc}")
    else:
        app.state.trace_redis = None

    app.state.risk = ServiceClient("risk-service", RISK_URL, timeout=3.0)
    app.state.ledger = ServiceClient("ledger-service", LEDGER_URL, timeout=5.0)
    # The adapter's timeout must EXCEED the switch timeout it wraps,
    # otherwise this service gives up first and reports "unknown" for
    # transactions the adapter was about to resolve cleanly, manufacturing
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


class TopupRequest(BaseModel):
    user_id: str
    card_number: str = Field(..., min_length=12, max_length=19)
    amount_cents: int = Field(..., gt=0)


class TransferRequest(BaseModel):
    user_id: str
    sender_card_number: str = Field(..., min_length=12, max_length=19)
    sender_pin: str = Field(..., min_length=4, max_length=12)
    recipient_account: str
    amount_cents: int = Field(..., gt=0)


class TransactionResponse(BaseModel):
    status: str   # approved | declined | review | reversed | unknown | error
    reason: str | None = None
    rrn: str | None = None
    stan: str | None = None
    authorization_id: str | None = None
    ledger_status: str | None = None
    # Set when the switch outcome could not be determined. A client seeing
    # this must NOT retry blindly, the original may have succeeded.
    requires_reconciliation: bool = False


def _is_rrn_collision(exc) -> bool:
    """
    A 409 carrying error "rrn_collision", as opposed to the other 409 the
    ledger returns, which is insufficient funds. Retrying a collision with a
    new reference is correct; retrying an overdraft is pointless.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return detail.get("error") == "rrn_collision"
    return "rrn_collision" in str(detail)


def _generate_rrn() -> str:
    """
    12 characters: 10 digits of epoch seconds plus 2 random.

    Collision risk is real but bounded, two transactions in the same second
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

    # -- Step 2: can they actually afford it?
    #
    # A pre-check, NOT the guarantee. ledger-service enforces solvency
    # atomically when it posts, and that is what makes an overdraft
    # impossible. This exists so an unaffordable purchase never reaches the
    # switch: without it the switch approves, the ledger refuses, and the
    # saga reverses an authorisation that should never have been requested.
    # That path works, but it burns a switch round trip and leaves a reversal
    # in the host's logs for what is really just an empty wallet.
    #
    # BEFORE risk, deliberately. Risk evaluation is stateful, it records an
    # attempt, which is why the call below is never retried. Feeding it a
    # transaction that was never going to proceed means a customer with an
    # empty wallet tapping "pay" three times inflates their own velocity and
    # sends their next funded transaction to review.
    #
    # Racy by nature: the balance can change between here and the posting.
    # That is fine. This is the friendly path; the ledger is the correct one.
    if not _can_afford(state, sender["account_id"], body.amount_cents):
        log.warning(f"insufficient funds for {mask_pan(body.card_number)}, declining early")
        trace.emit("saga", "declined: insufficient funds, the switch was never called",
                   {"amount_cents": body.amount_cents}, level="warn")
        return TransactionResponse(
            status="declined",
            reason="Insufficient funds. Top up your wallet and try again.",
        )

    # -- Step 3: risk. Never retried: evaluating records an attempt, so a
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
        # service that approves everything, that turns an outage into an
        # open fraud window.
        log.error(f"risk-service unavailable, failing closed: {exc}")
        raise HTTPException(status_code=503, detail="Risk evaluation unavailable; transaction refused.")

    trace.emit("risk", f"risk decision: {decision['outcome']}",
               {"reasons": decision["reasons"]},
               level="warn" if decision["outcome"] != "approve" else "info")

    if decision["outcome"] in ("decline", "review"):
        reason = "; ".join(decision["reasons"])
        log.warning(f"risk {decision['outcome']} card={mask_pan(body.card_number)}: {reason}")
        return TransactionResponse(status=decision["outcome"], reason=reason)

    # -- Step 4: authorize at the switch. THE POINT OF NO RETURN.
    rrn = _generate_rrn()
    trace.emit("saga", "RRN generated, entering the point of no return", {"rrn": rrn})
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
        log.error(f"UNKNOWN outcome rrn={rrn}, no ledger posting, reconciliation required")
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

    # -- Step 5: record it. Idempotent on RRN, so retries are safe.
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

    return _respond(auth, confirmed_rrn, ledger_status)


@app.post("/internal/transactions/transfer", response_model=TransactionResponse)
def transfer(body: TransferRequest, request: Request):
    """Same saga, different processing code (DE 3 = 400000) and a real
    recipient account instead of the demo merchant."""
    state = request.app.state
    trace.emit("saga", "transfer: processing code 400000, payee in DE 103",
               {"amount_cents": body.amount_cents})

    sender = _resolve_or_404(state, body.sender_card_number, "Sender card is not registered.")
    recipient = _resolve_or_404(state, body.recipient_account, "Recipient is not registered.")

    if sender.get("user_id") != body.user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to use this card.")
    if sender["account_id"] == recipient["account_id"]:
        # A self-transfer would post a debit and a credit to the same
        # account: the ledger still balances, the balance is unchanged, and
        # the row is pure noise. Reject it rather than record it.
        raise HTTPException(status_code=400, detail="Sender and recipient are the same account.")

    # Same pre-check as a purchase, in the same position and for the same
    # reasons. See the long comment there.
    if not _can_afford(state, sender["account_id"], body.amount_cents):
        log.warning("insufficient funds for transfer, declining early")
        return TransactionResponse(
            status="declined",
            reason="Insufficient funds. Top up your wallet and try again.",
        )

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

    return _respond(auth, confirmed_rrn, ledger_status)


# --------------------------------------------------------------------------
# Saga steps
# --------------------------------------------------------------------------

def _can_afford(state, account_id: str, amount_cents: int) -> bool:
    """Best-effort balance check. Fails OPEN: if the ledger cannot be reached
    the atomic check at posting time still protects us, and refusing every
    transaction because a read failed would be worse."""
    try:
        result = state.ledger.get(f"/internal/ledger/accounts/{account_id}/balance", retries=1)
        return int(result.get("balance_cents", 0)) >= amount_cents
    except Exception:  # noqa: BLE001
        return True


@app.post("/internal/transactions/topup", response_model=TransactionResponse)
def topup(body: TopupRequest, request: Request):
    """
    Put money into a wallet.

    Deliberately does NOT go through the switch. A card-funded top-up in a
    real platform would (processing code 21, a deposit), but this models an
    agent cash-in: the customer hands over cash and the agent credits the
    wallet. There is no card transaction to authorise, only a ledger movement.
    """
    state = request.app.state
    sender = _resolve_or_404(state, body.card_number, "Card is not registered.")
    if sender.get("user_id") != body.user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to use this card.")

    # Retried with a FRESH RRN on collision. The reference is ten digits of
    # epoch seconds plus two random, so two transactions in the same second
    # collide once in a hundred. A top-up touches no switch, so a second
    # attempt cannot authorise anything twice, which makes retrying safe here
    # in a way it would not be on the purchase path.
    #
    # Before the ledger learned to tell a collision from a replay, this
    # returned "approved" with ledger_status "already_recorded" and moved no
    # money at all.
    trace.emit("saga", "top-up: no switch, this is an agent cash-in",
               {"amount_cents": body.amount_cents})

    result = last_error = None
    for attempt in range(3):
        rrn = _generate_rrn()
        try:
            result = state.ledger.post("/internal/ledger/topup", {
                "rrn": rrn,
                "account_id": sender["account_id"],
                "amount_cents": body.amount_cents,
            }, retries=3)   # idempotent on RRN, so retrying cannot double-credit
            break
        except ServiceRejectedError as exc:
            if _is_rrn_collision(exc) and attempt < 2:
                trace.emit("saga", "RRN collision, retrying with a new reference",
                           {"rrn": rrn}, level="warn")
                log.warning(f"RRN collision on {rrn}, retrying with a new reference")
                last_error = exc
                continue
            raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
        except ServiceCallError as exc:
            raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")

    if result is None:  # pragma: no cover - three collisions in a row
        raise HTTPException(
            status_code=503,
            detail=f"Could not allocate a unique reference: {last_error}")

    log.info(f"topup rrn={rrn} amount_cents={body.amount_cents}")
    return TransactionResponse(status="approved", reason="Wallet topped up",
                               rrn=rrn, ledger_status=result["status"])


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
        # is a second, real authorization, the cardholder is debited twice
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


def _respond(auth: dict, rrn: str, ledger_status: dict) -> TransactionResponse:
    """
    Turn the saga's internal outcome into what the customer is told.

    The distinction that matters: the switch approving is NOT the transaction
    succeeding. If the ledger then refused and the authorisation was reversed,
    no money moved, and saying "approved" because the switch said so would be
    reporting an internal step as the result.

    Three outcomes, deliberately not two:

        approved                the money moved
        reversed                it did not, and we successfully undid the hold
        reconciliation_required we do not know, and a human has to look

    That last one is the honest answer to a genuinely unknown state, and it
    is why this returns a status rather than a boolean.
    """
    posting = ledger_status["status"]

    if posting == "reversed":
        return TransactionResponse(
            status="reversed",
            reason="The payment could not be recorded and the authorization was reversed. "
                   "No money left your account.",
            rrn=rrn,
            stan=auth.get("stan"),
            authorization_id=auth.get("authorization_id"),
            ledger_status=posting,
            requires_reconciliation=False,
        )

    if posting == "reconciliation_required":
        return TransactionResponse(
            status="unknown",
            reason="The payment is in an uncertain state and is being reconciled. "
                   "Do not retry; you will be contacted.",
            rrn=rrn,
            stan=auth.get("stan"),
            authorization_id=auth.get("authorization_id"),
            ledger_status=posting,
            requires_reconciliation=True,
        )

    return TransactionResponse(
        status="approved",
        reason=auth.get("response_text"),
        rrn=rrn,
        stan=auth.get("stan"),
        authorization_id=auth.get("authorization_id"),
        ledger_status=posting,
        requires_reconciliation=False,
    )


def _post_to_ledger(state, *, rrn, debit, credit, amount_cents, kind, card_number, stan) -> dict:
    """
    Step 4, plus its compensation.

    Retried, because the posting is idempotent on RRN, a retry after a lost
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
