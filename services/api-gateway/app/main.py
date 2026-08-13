"""
api-gateway, the only service with an OpenShift Route.

Everything else in the platform is namespace-internal. This is the single
door, and it owns the concerns that apply to EVERY request rather than to
any one use case:

  - correlation ID minting (every trace starts here)
  - JWT verification, locally, without a network hop
  - idempotency claiming, before any downstream work begins
  - translating internal service errors into client-appropriate HTTP

WHY IDEMPOTENCY IS CLAIMED HERE and not in transaction-service: because it
must happen before ANY step of the saga runs. Claiming inside the
orchestrator would still allow two concurrent duplicates to both pass risk
evaluation before either claimed, so a retried purchase would count twice
against the velocity window and could push a legitimate customer into a
decline. The claim is the first thing that happens after authentication.

WHY JWT VERIFICATION IS LOCAL: calling auth-service on every request would
add a round trip to every call and make an auth-service outage a total
platform outage. The signature is verifiable with the shared secret alone.
The cost is a window, bounded by the token lifetime, in which a deleted
user's token still works, an acceptable trade at a one-hour lifetime, and
/internal/auth/introspect exists for callers that need certainty.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.auth.tokens import TokenError, decode_token
from mfcommon.identity.msisdn import InvalidMsisdn, normalise
from mfcommon.http.client import ServiceCallError, ServiceClient, ServiceRejectedError
from mfcommon.observability import trace
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    new_correlation_id,
    set_correlation_id,
)

from app.idempotency import InMemoryIdempotencyStore, RedisIdempotencyStore

AUTH_URL = os.environ.get("AUTH_SERVICE_URL", "http://auth-service:8081")
TRANSACTION_URL = os.environ.get("TRANSACTION_SERVICE_URL", "http://transaction-service:8082")
LEDGER_URL = os.environ.get("LEDGER_SERVICE_URL", "http://ledger-service:8084")
REDIS_URL = os.environ.get("REDIS_URL")

# The operator console: a live trace view, a ledger browser, an ad-hoc
# ClickHouse query box and a load generator. All of it is a debug surface
# on the one service with a public Route, so it is off unless asked for.
ENABLE_CONSOLE = os.environ.get("ENABLE_CONSOLE", "0") == "1"

_DEV_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_SECRET)

TRACE_REDIS_URL = os.environ.get("REDIS_URL")

log = configure_logging("api-gateway", os.environ.get("LOG_LEVEL", "INFO"))


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
            trace.configure(_tc, "api-gateway")
            app.state.trace_redis = _tc
            log.info("request tracing enabled")
        except Exception as exc:  # noqa: BLE001
            app.state.trace_redis = None
            log.warning(f"tracing disabled, Redis unreachable: {exc}")
    else:
        app.state.trace_redis = None

    if JWT_SECRET == _DEV_SECRET:
        log.warning("JWT_SECRET is unset, using the published development default.")

    app.state.auth = ServiceClient("auth-service", AUTH_URL, timeout=5.0)
    # Generous: this one wraps the whole saga including the switch round trip.
    app.state.transactions = ServiceClient("transaction-service", TRANSACTION_URL, timeout=35.0)
    app.state.ledger = ServiceClient("ledger-service", LEDGER_URL, timeout=5.0)

    if REDIS_URL:
        import redis

        client = redis.Redis.from_url(REDIS_URL)
        client.ping()
        app.state.idempotency = RedisIdempotencyStore(client)
        app.state.redis = client
        log.info("idempotency backed by Redis, safe across replicas")
    else:
        app.state.idempotency = InMemoryIdempotencyStore()
        app.state.redis = None
        log.warning(
            "REDIS_URL is not set, idempotency is per-process. With more than one "
            "gateway replica a retry landing on a different pod will be processed "
            "again as if new, which for a purchase means charging twice."
        )

    yield

    for client in (app.state.auth, app.state.transactions, app.state.ledger):
        client.close()


app = FastAPI(title="Microfinance API Gateway", lifespan=lifespan)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """
    Mints the correlation ID if the client did not supply one, and echoes it
    back on the response so a client reporting a problem can quote the exact
    identifier that appears in every service's logs.
    """
    incoming = request.headers.get(CORRELATION_HEADER)
    correlation_id = incoming or new_correlation_id()
    set_correlation_id(correlation_id)

    if request.url.path.startswith(("/transactions", "/accounts", "/users", "/auth")):
        trace.emit("gateway", f"{request.method} {request.url.path}",
                   {"client": request.client.host if request.client else "?"})
        if getattr(request.app.state, "trace_redis", None) is not None:
            trace.register(request.app.state.trace_redis, correlation_id,
                           f"{request.method} {request.url.path}")

    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    trace.emit("gateway", f"responded {response.status_code}",
               {"status": response.status_code},
               level="warn" if response.status_code >= 400 else "info")
    return response


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def current_user(request: Request, authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header. Expected: Bearer <token>")

    try:
        claims = decode_token(authorization[len("Bearer "):].strip(), secret=JWT_SECRET)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token carries no subject claim.")
    return {"user_id": user_id}


# --------------------------------------------------------------------------
# Schemas, the PUBLIC contract. Amounts are decimal currency here and
# converted to integer cents exactly once, on the way in.
# --------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    full_name: str
    # The MSISDN is the account. Any format the customer might type is
    # accepted and normalised to one stored value.
    msisdn: str = Field(..., min_length=8, max_length=20)
    bind_card_number: str = Field(..., min_length=12, max_length=19)
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    msisdn: str = Field(..., min_length=8, max_length=20)
    password: str


class PurchaseRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Whole currency units, e.g. 50.00")
    card_number: str = Field(..., min_length=12, max_length=19)
    pin: str = Field(..., min_length=4, max_length=12)
    idempotency_key: str = Field(..., min_length=8)
    entry_mode: str = "05"


class TopupRequest(BaseModel):
    # Capped. Not a risk control, an accident control: the amount is typed by
    # hand into a demo console, and a stray keypress that credits a wallet
    # with 100 million distorts every dashboard afterwards. Real cash-in
    # limits are a regulatory matter (CDD tiers) and belong in risk-service.
    amount: float = Field(..., gt=0, le=500_000)
    card_number: str = Field(..., min_length=12, max_length=19)
    idempotency_key: str = Field(..., min_length=8)


class TransferRequest(BaseModel):
    amount: float = Field(..., gt=0)
    sender_card_number: str = Field(..., min_length=12, max_length=19)
    sender_pin: str = Field(..., min_length=4, max_length=12)
    recipient_account: str
    idempotency_key: str = Field(..., min_length=8)


def _to_cents(amount: float) -> int:
    """
    The single float -> integer boundary in the platform.

    round(), not int(): int(0.29 * 100) is 28, because 0.29 has no exact
    binary representation and lands just below 29. Everything downstream of
    this line is integer cents.
    """
    return round(amount * 100)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

@app.post("/users/register")
def register(body: RegisterRequest, request: Request):
    """
    A two-service saga: create the user in auth-service, then the account
    and card in ledger-service. If the second fails, the first is
    compensated by deleting the user, otherwise a failed registration
    leaves a user who can log in, owns nothing, and whose CNIC now blocks
    them from trying again.
    """
    state = request.app.state

    try:
        user = state.auth.post(
            "/internal/auth/users",
            {"full_name": body.full_name, "msisdn": body.msisdn, "password": body.password},
            retries=0,  # not idempotent: a retry creates a second user
        )
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Auth service unavailable: {exc}")

    try:
        account = state.ledger.post(
            "/internal/ledger/accounts",
            {"user_id": user["user_id"], "card_number": body.bind_card_number,
             # auth-service returns the normalised form. Passing the raw input
             # instead would store two different spellings of one number and
             # every transfer to it would miss.
             "msisdn": user.get("msisdn")},
            retries=0,
        )
    except (ServiceRejectedError, ServiceCallError) as exc:
        log.error(f"account creation failed for {user['user_id']}, compensating: {exc}")
        try:
            state.auth._request(
                "DELETE", f"/internal/auth/users/{user['user_id']}", retries=2, timeout=None
            )
        except Exception as compensation_error:  # noqa: BLE001
            # The compensation itself failed. Say so loudly, this leaves
            # an orphaned user row that a human must clean up.
            log.error(
                f"COMPENSATION FAILED: user {user['user_id']} exists with no account "
                f"and could not be deleted: {compensation_error!r}"
            )
        status = getattr(exc, "status_code", 503) or 503
        raise HTTPException(status_code=status, detail=f"Could not create the account: {exc}")

    return {
        "status": "success",
        "user_id": user["user_id"],
        "msisdn": user.get("msisdn"),
        "account_id": account["account_id"],
        "message": "User registered, wallet created, and card bound.",
    }


@app.post("/auth/login")
def login(body: LoginRequest, request: Request):
    try:
        return request.app.state.auth.post(
            "/internal/auth/login", {"msisdn": body.msisdn, "password": body.password}, retries=0
        )
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Auth service unavailable: {exc}")


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

def _release_on_definite_failure(state, key: str, exc: ServiceRejectedError):
    """
    Give a claimed idempotency key back after a downstream 4xx.

    The asymmetry here is the whole point. Releasing when we should not means
    a retry double-processes, and on the purchase path that is a second real
    authorisation at the switch: money moves twice. Holding when we could
    have released means the key is stuck until its TTL and the customer
    starts a new one. The second is an annoyance; the first is an incident.

    So the rule is narrow. A ServiceRejectedError is a 4xx: the service
    understood the request and refused it, for an unregistered card, an
    unregistered merchant, a card the caller does not own. Every one of those
    is decided BEFORE the saga contacts the switch, so nothing happened.

    ServiceCallError is never handled here. A timeout or a 5xx is exactly the
    case where the switch may already hold an authorisation.
    """
    if 400 <= exc.status_code < 500:
        state.idempotency.release(key)
        log.info(f"released idempotency key after a {exc.status_code}, nothing happened")


def _claim(state, key: str, body: BaseModel):
    """
    Atomic claim before any downstream work. Returns a cached response for a
    genuine duplicate, or raises for the conflict cases.
    """
    request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    outcome = state.idempotency.claim(key, request_hash)

    if outcome.status == "mismatch":
        # Same key, DIFFERENT body. Almost always a client bug, reusing a
        # key for a new transaction. Returning the cached response would
        # silently answer the wrong question, so this is rejected loudly.
        raise HTTPException(
            status_code=400,
            detail="This idempotency key was already used for a different request body.",
        )
    if outcome.status == "duplicate":
        trace.emit("gateway", "idempotent replay, returning cached response",
                   {"key": key}, level="warn")
        return outcome.cached_response
    if outcome.status == "new":
        # Traced because whether a request was CLAIMED or REPLAYED is the
        # first fork in every money path, and it was the one step in the
        # documented purchase flow with no evidence behind it: only the
        # replay branch below emitted anything.
        trace.emit("gateway", "idempotency key claimed", {"key": key})
        return None
    if outcome.status == "in_progress":
        # A rare but genuine race: another request holds the claim and has
        # not finished. 409 rather than processing again.
        raise HTTPException(
            status_code=409, detail="This request is already being processed. Retry shortly."
        )
    return None


@app.post("/transactions/purchase")
def purchase(body: PurchaseRequest, request: Request, user: dict = Depends(current_user)):
    state = request.app.state

    cached = _claim(state, body.idempotency_key, body)
    if cached is not None:
        log.info(f"idempotent replay for key={body.idempotency_key}")
        return cached

    try:
        result = state.transactions.post(
            "/internal/transactions/purchase",
            {
                "user_id": user["user_id"],
                "card_number": body.card_number,
                "pin": body.pin,
                "amount_cents": _to_cents(body.amount),
                "entry_mode": body.entry_mode,
            },
            retries=0,
        )
    except ServiceRejectedError as exc:
        # Refused before the switch was contacted, so the key goes back.
        # Without this a mistyped card locks that key for a full day.
        _release_on_definite_failure(state, body.idempotency_key, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        # Deliberately NOT cached, and the claim is deliberately HELD. The
        # outcome is unknown, and caching an error would make every retry of
        # this key return the same error forever, even though the
        # transaction may have succeeded. Releasing would be worse still: a
        # retry could authorise a second time.
        raise HTTPException(status_code=503, detail=f"Transaction service unavailable: {exc}")

    state.idempotency.store_response(body.idempotency_key, result)
    return result


@app.post("/transactions/topup")
def topup(body: TopupRequest, request: Request, user: dict = Depends(current_user)):
    """
    Put money into a wallet.

    No PIN, deliberately, and worth being explicit about why rather than
    leaving it looking like an omission. A PIN authorises money LEAVING an
    account; there is nothing to authorise about money arriving. What does
    need checking is that the card belongs to the caller, which
    transaction-service enforces, so nobody can inflate a stranger's balance.

    In production the authorisation that matters here is on the funding side:
    the agent's till, the card being charged, the bank debit. This platform
    has none of those, so a top-up is a ledger movement from the funding
    account and nothing more.
    """
    state = request.app.state

    cached = _claim(state, body.idempotency_key, body)
    if cached is not None:
        return cached

    try:
        result = state.transactions.post(
            "/internal/transactions/topup",
            {
                "user_id": user["user_id"],
                "card_number": body.card_number,
                "amount_cents": _to_cents(body.amount),
            },
            retries=0,
        )
    except ServiceRejectedError as exc:
        # Refused before anything happened, so the key goes back.
        _release_on_definite_failure(state, body.idempotency_key, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        # Claim HELD on purpose: the switch may already have authorised.
        raise HTTPException(status_code=503, detail=f"Transaction service unavailable: {exc}")

    state.idempotency.store_response(body.idempotency_key, result)
    return result


@app.post("/transactions/transfer")
def transfer(body: TransferRequest, request: Request, user: dict = Depends(current_user)):
    state = request.app.state

    cached = _claim(state, body.idempotency_key, body)
    if cached is not None:
        return cached

    try:
        result = state.transactions.post(
            "/internal/transactions/transfer",
            {
                "user_id": user["user_id"],
                "sender_card_number": body.sender_card_number,
                "sender_pin": body.sender_pin,
                "recipient_account": body.recipient_account,
                "amount_cents": _to_cents(body.amount),
            },
            retries=0,
        )
    except ServiceRejectedError as exc:
        # Refused before anything happened, so the key goes back.
        _release_on_definite_failure(state, body.idempotency_key, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        # Claim HELD on purpose: the switch may already have authorised.
        raise HTTPException(status_code=503, detail=f"Transaction service unavailable: {exc}")

    state.idempotency.store_response(body.idempotency_key, result)
    return result


@app.get("/accounts/{identifier}/balance")
def balance(identifier: str, request: Request, user: dict = Depends(current_user)):
    """identifier may be a card number or an account ID."""
    state = request.app.state

    try:
        resolved = state.ledger.post("/internal/ledger/resolve", {"identifier": identifier}, retries=2)
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")

    # Ownership, not just authentication. Without this any authenticated
    # user could read any balance by guessing an account ID.
    if resolved.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="You are not authorized to view this account.")

    try:
        return state.ledger.get(f"/internal/ledger/accounts/{resolved['account_id']}/balance")
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway"}


@app.get("/ready")
def ready(request: Request):
    state = request.app.state
    if state.redis is not None:
        try:
            state.redis.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"redis unreachable: {exc}")
    return {
        "status": "ready",
        "breakers": {
            "auth-service": "open" if state.auth.breaker.is_open else "closed",
            "transaction-service": "open" if state.transactions.breaker.is_open else "closed",
            "ledger-service": "open" if state.ledger.breaker.is_open else "closed",
        },
    }


# --------------------------------------------------------------------------
# Operator console
# --------------------------------------------------------------------------
#
# Mounted last so its catch-all static route cannot shadow an API path, and
# only when ENABLE_CONSOLE=1. In an environment where it is off, none of
# these routes exist at all rather than existing and refusing.
if ENABLE_CONSOLE:
    from pathlib import Path as _Path

    from fastapi.responses import FileResponse

    from app.console import router as console_router

    # Auth applied at the router level, so console.py never has to import
    # current_user from this module and create a circular import. Every
    # console route needs a token: traces carry transaction detail, and
    # the ledger browser is the ledger.
    app.include_router(console_router, dependencies=[Depends(current_user)])

    _STATIC = _Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def console_index():
        index = _STATIC / "index.html"
        if not index.exists():
            return {"error": "console assets missing", "looked_in": str(_STATIC)}
        return FileResponse(index)

    log.info("operator console enabled at /")
