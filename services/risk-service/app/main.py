"""
risk-service, decides whether a transaction should be attempted at all.

Called by transaction-service BEFORE anything else happens: before the PIN
is encrypted, before a message is built, before the switch is contacted. If
this declines, nothing downstream runs.

WHY THIS IS ITS OWN SERVICE, when the rules are ninety lines: because the
rules change constantly and everything else here does not. Fraud thresholds
get retuned weekly in response to actual attack patterns. Double-entry
accounting does not. Splitting them means a threshold change is a
risk-service deploy, not a redeploy of the money path.

The velocity state lives in Redis rather than in this process, and that is
not an optimisation. With per-pod memory, an attacker spreading attempts
across replicas is invisible to whichever pod did not see the earlier ones:
six attempts across three pods look like two attempts each, and every one
is approved. Shared state is what makes the sliding window mean anything at
all once there is more than one replica.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from mfcommon.observability.audit import mask_pan
from mfcommon.observability import trace
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)

from app.rules import RiskEngine
from app.velocity import InMemoryVelocityTracker, RedisVelocityTracker

REDIS_URL = os.environ.get("REDIS_URL")

# Every threshold is configurable without a code change, because these are
# the values most likely to need adjusting at short notice.
CONFIG = dict(
    velocity_window_seconds=float(os.environ.get("RISK_VELOCITY_WINDOW_SECONDS", "60")),
    velocity_decline_count=int(os.environ.get("RISK_VELOCITY_DECLINE_COUNT", "5")),
    velocity_review_count=int(os.environ.get("RISK_VELOCITY_REVIEW_COUNT", "3")),
    amount_decline_cents=int(os.environ.get("RISK_AMOUNT_DECLINE_CENTS", "1000000")),
    amount_review_cents=int(os.environ.get("RISK_AMOUNT_REVIEW_CENTS", "200000")),
    manual_entry_review_cents=int(os.environ.get("RISK_MANUAL_ENTRY_REVIEW_CENTS", "50000")),
)

TRACE_REDIS_URL = os.environ.get("REDIS_URL")

log = configure_logging("risk-service", os.environ.get("LOG_LEVEL", "INFO"))


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
            trace.configure(_tc, "risk-service")
            app.state.trace_redis = _tc
            log.info("request tracing enabled")
        except Exception as exc:  # noqa: BLE001
            app.state.trace_redis = None
            log.warning(f"tracing disabled, Redis unreachable: {exc}")
    else:
        app.state.trace_redis = None

    if REDIS_URL:
        import redis

        client = redis.Redis.from_url(REDIS_URL)
        client.ping()  # fail at startup, not on the first transaction
        tracker = RedisVelocityTracker(client)
        app.state.redis = client
        log.info(f"velocity state in Redis ({REDIS_URL}), shared across replicas")
    else:
        tracker = InMemoryVelocityTracker()
        app.state.redis = None
        log.warning(
            "REDIS_URL is not set, velocity state is per-process. Correct for a "
            "single replica ONLY; with more than one, attempts split across pods "
            "will not be counted together and velocity rules become bypassable."
        )

    app.state.engine = RiskEngine(velocity_tracker=tracker, **CONFIG)
    yield


app = FastAPI(title="Risk Service", lifespan=lifespan)


@app.middleware("http")
async def adopt_correlation_id(request: Request, call_next):
    incoming = request.headers.get(CORRELATION_HEADER)
    if incoming:
        set_correlation_id(incoming)
    return await call_next(request)


class EvaluateRequest(BaseModel):
    card_number: str = Field(..., min_length=12, max_length=19)
    amount_cents: int = Field(..., gt=0)
    entry_mode: str = "05"


class EvaluateResponse(BaseModel):
    outcome: str          # "approve" | "review" | "decline"
    reasons: list[str]


@app.post("/internal/risk/evaluate", response_model=EvaluateResponse)
def evaluate(body: EvaluateRequest, request: Request):
    """
    NOTE: this call has a side effect. Evaluating RECORDS the attempt in the
    velocity window, so calling it twice for one transaction counts twice
    and inflates the caller's own velocity toward a decline.

    That is why transaction-service must never retry this endpoint, and why
    the shared HTTP client defaults POST retries to zero. An endpoint that
    is not idempotent should be hard to retry by accident.
    """
    decision = request.app.state.engine.evaluate(
        card_number=body.card_number,
        amount_cents=body.amount_cents,
        entry_mode=body.entry_mode,
    )

    # Reported by the service that MADE the decision, not only by the one
    # that asked for it. transaction-service already traces the outcome it
    # received; this is the rule engine's own account of why, and without it
    # risk-service never appeared in a timeline at all.
    trace.emit(
        "risk", f"{decision.outcome}: " + ("; ".join(decision.reasons) or "no rule fired"),
        {"card": mask_pan(body.card_number), "amount_cents": body.amount_cents,
         "entry_mode": body.entry_mode},
        level="info" if decision.outcome == "approve" else "warn",
    )

    if decision.outcome != "approve":
        log.warning(
            f"risk {decision.outcome} for card={mask_pan(body.card_number)} "
            f"amount_cents={body.amount_cents}: {'; '.join(decision.reasons)}"
        )

    return EvaluateResponse(outcome=decision.outcome, reasons=decision.reasons)


@app.get("/internal/risk/config")
def config():
    """Exposed so the current thresholds are observable without shelling
    into a pod, a decline nobody can explain is a support burden."""
    return {"thresholds": CONFIG, "velocity_backend": "redis" if REDIS_URL else "in-memory"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "risk-service"}


@app.get("/ready")
def ready(request: Request):
    client = request.app.state.redis
    if client is not None:
        try:
            client.ping()
        except Exception as exc:
            # Fail readiness rather than silently degrading to per-pod
            # counting. Quietly losing shared velocity state is worse than
            # visibly refusing traffic, because nothing looks wrong while
            # the fraud controls stop working.
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail=f"redis unreachable: {exc}")
    return {"status": "ready"}
