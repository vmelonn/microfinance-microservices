"""
ledger-service, owns accounts, cards, and the double-entry ledger.

Every route is under /internal. This service has NO OpenShift Route and is
reachable only from inside the namespace, enforced by a NetworkPolicy that
admits traffic solely from api-gateway and transaction-service. A ledger
endpoint exposed to the internet is a ledger endpoint someone will
eventually call.

The service is deliberately dumb about business rules. It does not know what
a "purchase" is, does not evaluate risk, and does not check that a debit
leaves an account solvent. It records balanced journal entries against
account IDs, idempotently, and reports balances. Every policy decision
belongs to a caller. That is what keeps it reusable and what keeps its
tests about accounting rather than about payments.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.db.dialect import Database
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)

from app.repository import LedgerRepository

LEDGER_DSN = os.environ.get("LEDGER_DSN", "ledger.db")
ALLOW_RESET = os.environ.get("ALLOW_LEDGER_RESET", "0") == "1"

log = configure_logging("ledger-service", os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(LEDGER_DSN)
    # Wait for Postgres rather than crashing when it is not up yet. On a cold
    # namespace the database and this service start together, and failing
    # fast here just means restart-looping until the timing happens to work.
    db.wait_until_available()
    repo = LedgerRepository(db)
    repo.init_schema()
    app.state.repo = repo
    log.info(f"ledger-service ready on {db.dialect}")
    yield


app = FastAPI(title="Ledger Service", lifespan=lifespan)


@app.middleware("http")
async def adopt_correlation_id(request: Request, call_next):
    incoming = request.headers.get(CORRELATION_HEADER)
    if incoming:
        set_correlation_id(incoming)
    return await call_next(request)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class CreateAccountRequest(BaseModel):
    user_id: str
    card_number: str = Field(..., min_length=12, max_length=19)
    account_type: str = "checking"


class PostingRequest(BaseModel):
    rrn: str = Field(..., min_length=6, max_length=12)
    debit_account: str
    credit_account: str
    # Integer cents, never a float. 0.1 + 0.2 != 0.3 in binary floating
    # point, and a ledger that drifts by fractions of a cent per posting is
    # a ledger that stops balancing. The float->cents conversion happens
    # exactly once, at the gateway, and everything below it is integers.
    amount_cents: int = Field(..., gt=0)
    kind: str = "purchase"


class ResolveRequest(BaseModel):
    identifier: str


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.post("/internal/ledger/accounts")
def create_account(body: CreateAccountRequest, request: Request):
    repo: LedgerRepository = request.app.state.repo
    try:
        return repo.create_account(body.user_id, body.card_number, body.account_type)
    except Exception as exc:
        if repo.db.is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="That card number is already registered.")
        raise


@app.post("/internal/ledger/resolve")
def resolve(body: ResolveRequest, request: Request):
    repo: LedgerRepository = request.app.state.repo
    account_id = repo.resolve_account(body.identifier)
    if account_id is None:
        raise HTTPException(status_code=404, detail="No account or card matches that identifier.")
    return {"account_id": account_id, "user_id": repo.owner_of(account_id)}


@app.post("/internal/ledger/postings")
def create_posting(body: PostingRequest, request: Request):
    """
    Idempotent on RRN. Safe to retry from anywhere, including after a
    timeout where the caller cannot tell whether the first attempt landed,
    which is exactly the situation transaction-service finds itself in.
    """
    repo: LedgerRepository = request.app.state.repo
    try:
        result = repo.record_posting(
            rrn=body.rrn,
            debit_account=body.debit_account,
            credit_account=body.credit_account,
            amount_cents=body.amount_cents,
            kind=body.kind,
        )
    except Exception as exc:
        # A foreign-key violation reaches here rather than being mislabelled
        # as "already recorded", see repository.record_posting.
        log.error(f"posting failed for rrn={body.rrn}: {exc!r}")
        raise HTTPException(
            status_code=422,
            detail=f"Posting rejected, debit or credit account does not exist: {exc}",
        )

    log.info(f"posting rrn={body.rrn} status={result['status']} amount_cents={body.amount_cents}")
    return result


@app.get("/internal/ledger/accounts/{account_id}/balance")
def balance(account_id: str, request: Request):
    repo: LedgerRepository = request.app.state.repo
    if repo.owner_of(account_id) is None:
        raise HTTPException(status_code=404, detail="Unknown account.")
    cents = repo.balance(account_id)
    return {"account_id": account_id, "balance_cents": cents, "balance": cents / 100.0}


@app.get("/internal/ledger/transactions/{rrn}")
def find(rrn: str, request: Request):
    found = request.app.state.repo.find_transaction(rrn)
    if found is None:
        raise HTTPException(status_code=404, detail="No transaction with that RRN.")
    return found


@app.get("/internal/ledger/export")
def export(request: Request, since: str | None = None, limit: int = 50_000):
    """Feeds analytics-sync's incremental load."""
    rows = request.app.state.repo.export_since(since, limit)
    return {"rows": rows, "count": len(rows)}


@app.get("/internal/ledger/integrity")
def integrity(request: Request):
    """Total debits must equal total credits. A scheduled caller asserts
    this; a false result means stop and investigate, not retry."""
    balanced = request.app.state.repo.is_balanced()
    return {"balanced": balanced}


@app.post("/internal/ledger/reset")
def reset(request: Request):
    """
    Guarded by ALLOW_LEDGER_RESET, which is unset in every non-dev overlay.

    The monolith's equivalent endpoint required only a valid token, and its
    own docstring admitted any authenticated user could wipe the entire
    ledger. Config-gating is a genuine improvement but still not
    authorization, proper role-based access is a follow-up, and this
    comment exists so that is not forgotten.
    """
    if not ALLOW_LEDGER_RESET_ENABLED():
        raise HTTPException(status_code=403, detail="Ledger reset is disabled in this environment.")
    request.app.state.repo.reset()
    log.warning("ledger wiped via /internal/ledger/reset")
    return {"status": "reset"}


def ALLOW_LEDGER_RESET_ENABLED() -> bool:
    # Read through a function rather than the module constant so tests can
    # monkeypatch it without reimporting the module.
    return ALLOW_RESET


@app.get("/health")
def health():
    return {"status": "ok", "service": "ledger-service"}


@app.get("/ready")
def ready(request: Request):
    """Readiness genuinely touches the database. A ledger pod that cannot
    reach Postgres must not receive traffic, and only a real query proves
    it can."""
    try:
        request.app.state.repo.is_balanced()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    return {"status": "ready"}
