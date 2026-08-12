"""
auth-service, owns user identity and issues JWTs.

Owns its OWN database. ledger-service cannot read the users table and
auth-service cannot read the ledger; the only link between them is
accounts.user_id, which is an opaque string on the ledger side with no
foreign key behind it. That is deliberate. A shared users table is the most
common way a microservice split quietly becomes a distributed monolith,
two services coupled through a schema, unable to deploy or migrate
independently.

REGISTRATION IS A TWO-SERVICE OPERATION, and therefore a saga:
    1. auth-service creates the user            (here)
    2. ledger-service creates account + card    (a separate call)
If step 2 fails, step 1 is compensated by deleting the user. There is no
distributed transaction available, so the compensation is explicit. The
gateway orchestrates it, because the gateway is the only participant that
sees both halves.

TOKEN VERIFICATION IS LOCAL, NOT A NETWORK CALL. api-gateway verifies the
HS256 signature itself using the shared secret, rather than calling
/introspect on every request. Introspection exists for the cases that
genuinely need current state (has this user been deleted?), but making it
mandatory would put auth-service in the hot path of every single request,
one extra network round trip per call, and a hard dependency that turns an
auth-service outage into a total platform outage.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.auth.passwords import hash_password, verify_password
from mfcommon.auth.tokens import TokenError, create_token, decode_token
from mfcommon.db.dialect import Database, utc_now_param
from mfcommon.identity.msisdn import InvalidMsisdn, mask, normalise
from mfcommon.observability import trace
from mfcommon.observability.correlation import (
    CORRELATION_HEADER,
    configure_logging,
    set_correlation_id,
)

AUTH_DSN = os.environ.get("AUTH_DSN", "auth.db")
TOKEN_LIFETIME = int(os.environ.get("ACCESS_TOKEN_LIFETIME_SECONDS", "3600"))

_DEV_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_SECRET)

ALLOW_PURGE = os.environ.get("ALLOW_AUTH_PURGE", "0") == "1"

TRACE_REDIS_URL = os.environ.get("REDIS_URL")

log = configure_logging("auth-service", os.environ.get("LOG_LEVEL", "INFO"))

if JWT_SECRET == _DEV_SECRET:
    log.warning(
        "JWT_SECRET is unset, using a development default that is published in "
        "this source file. Every token issued is forgeable by anyone who can read "
        "the repo. Set JWT_SECRET before deploying anywhere real."
    )


def _init_schema(db: Database) -> None:
    with db.transaction() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                full_name     TEXT NOT NULL,
                msisdn        TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    {db.timestamp_type} NOT NULL
            )
        """)
        _migrate_cnic_to_msisdn(db, cur)


def _migrate_cnic_to_msisdn(db: Database, cur) -> None:
    """
    CREATE TABLE IF NOT EXISTS does nothing to a users table that already has
    a cnic column, so an existing database would keep the old identifier and
    every login would fail on a column that is not there.

    Renamed rather than dropped: the values are real customers. They are NOT
    re-normalised into MSISDN form, because a CNIC is not a phone number and
    pretending otherwise would invent numbers that belong to somebody else.
    Those rows keep whatever they had and simply will not match a phone-number
    login, which is the honest outcome.
    """
    if db.is_postgres:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'users'
        """)
        columns = {r[0] for r in cur.fetchall()}
    else:
        cur.execute("PRAGMA table_info(users)")
        columns = {r[1] for r in cur.fetchall()}

    if "cnic" in columns and "msisdn" not in columns:
        cur.execute("ALTER TABLE users RENAME COLUMN cnic TO msisdn")


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
            trace.configure(_tc, "auth-service")
            app.state.trace_redis = _tc
            log.info("request tracing enabled")
        except Exception as exc:  # noqa: BLE001
            app.state.trace_redis = None
            log.warning(f"tracing disabled, Redis unreachable: {exc}")
    else:
        app.state.trace_redis = None

    db = Database(AUTH_DSN)
    # See the note in ledger-service: wait for the database instead of
    # relying on container restarts to eventually catch it awake.
    db.wait_until_available()
    _init_schema(db)
    app.state.db = db
    log.info(f"auth-service ready on {db.dialect}")
    yield


app = FastAPI(title="Auth Service", lifespan=lifespan)


@app.middleware("http")
async def adopt_correlation_id(request: Request, call_next):
    incoming = request.headers.get(CORRELATION_HEADER)
    if incoming:
        set_correlation_id(incoming)
    return await call_next(request)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=1)
    # The MSISDN is the customer identifier, not a contact detail. Accepted in
    # any format the customer might type and normalised to one stored form,
    # because "0300 123 4567" and "+923001234567" are one subscriber.
    msisdn: str = Field(..., min_length=8, max_length=20)
    # Login password, NOT the card PIN. The two are deliberately separate
    # secrets: this one is verified locally by us, the PIN is verified by
    # the switch at transaction time. Conflating them would mean one
    # compromise exposes both.
    password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    msisdn: str = Field(..., min_length=8, max_length=20)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user_id: str


class IntrospectRequest(BaseModel):
    token: str


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.post("/internal/auth/users")
def register(body: RegisterRequest, request: Request):
    db: Database = request.app.state.db
    user_id = f"usr_{uuid.uuid4().hex[:12]}"

    try:
        msisdn = normalise(body.msisdn)
    except InvalidMsisdn as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        with db.transaction() as conn:
            conn.cursor().execute(
                db.sql(
                    "INSERT INTO users (user_id, full_name, msisdn, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (user_id, body.full_name, msisdn, hash_password(body.password), utc_now_param()),
            )
    except Exception as exc:
        if db.is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="That phone number is already registered.")
        raise

    log.info(f"registered user_id={user_id} msisdn={mask(msisdn)}")
    return {"user_id": user_id, "full_name": body.full_name, "msisdn": msisdn}


@app.delete("/internal/auth/users/{user_id}")
def delete_user(user_id: str, request: Request):
    """
    The compensating action for the registration saga. Called by the gateway
    when account creation fails after the user row was already written,
    without it, a failed registration leaves an orphaned user who can log in
    but owns no account, and whose CNIC now blocks them from retrying.

    Idempotent: deleting an already-deleted user succeeds. A compensation
    that can fail because it already ran is not much of a compensation.
    """
    db: Database = request.app.state.db
    with db.transaction() as conn:
        conn.cursor().execute(db.sql("DELETE FROM users WHERE user_id = ?"), (user_id,))
    log.warning(f"compensating delete of user_id={user_id}")
    return {"status": "deleted", "user_id": user_id}


@app.post("/internal/auth/purge")
def purge_users(request: Request):
    """
    Delete every user. Half of a platform wipe; ledger-service does the
    other half.

    Gated by ALLOW_AUTH_PURGE, which is set only in the dev overlay. This is
    a separate flag from the ledger's rather than a shared one because the
    two services own separate config and neither can read the other's, so a
    shared name would be a convention nothing enforces.

    Tokens already issued keep working until they expire, which is inherent
    to stateless JWTs: there is no session table to clear. A token whose user
    no longer exists still authenticates, and every call it makes then fails
    on a missing account. Introspection does not check the user still exists,
    and making it do so would put a database read on the hot path of every
    request to buy correctness in a case that only arises here.
    """
    if not ALLOW_PURGE:
        raise HTTPException(status_code=403, detail="User purge is disabled in this environment.")

    db: Database = request.app.state.db
    with db.transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = int(cur.fetchone()[0])
        cur.execute("DELETE FROM users")

    log.warning(f"ALL USERS PURGED via /internal/auth/purge: {count} rows")
    return {"status": "purged", "users": count}


@app.post("/internal/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request):
    db: Database = request.app.state.db
    try:
        msisdn = normalise(body.msisdn)
    except InvalidMsisdn as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with db.cursor() as cur:
        cur.execute(db.sql("SELECT user_id, password_hash FROM users WHERE msisdn = ?"), (msisdn,))
        row = cur.fetchone()

    # Identical error for "no such user" and "wrong password". Distinguishing
    # them turns this endpoint into a phone-number enumeration oracle,
    # which matters more for an MSISDN than a CNIC: the number space is
    # small and guessable.
    if row is None or not verify_password(body.password, row[1]):
        log.warning("failed login attempt")
        raise HTTPException(status_code=401, detail="Invalid phone number or password.")

    user_id = row[0]
    token = create_token({"sub": user_id}, secret=JWT_SECRET, expires_in_seconds=TOKEN_LIFETIME)
    log.info(f"issued token for user_id={user_id}")
    return TokenResponse(access_token=token, expires_in_seconds=TOKEN_LIFETIME, user_id=user_id)


@app.post("/internal/auth/introspect")
def introspect(body: IntrospectRequest, request: Request):
    """
    Verifies a token AND confirms the user still exists.

    The second half is what api-gateway's local verification cannot do. A
    validly-signed token for a deleted user stays cryptographically valid
    until it expires; only a lookup catches that. Callers that need
    certainty use this. Callers that need speed verify locally and accept a
    window bounded by the token lifetime.
    """
    try:
        claims = decode_token(body.token, secret=JWT_SECRET)
    except TokenError as exc:
        return {"active": False, "reason": str(exc)}

    db: Database = request.app.state.db
    with db.cursor() as cur:
        cur.execute(
            db.sql("SELECT user_id, full_name FROM users WHERE user_id = ?"), (claims.get("sub"),)
        )
        row = cur.fetchone()

    if row is None:
        return {"active": False, "reason": "Token refers to a user that no longer exists."}

    return {"active": True, "user_id": row[0], "full_name": row[1], "expires_at": claims.get("exp")}


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth-service"}


@app.get("/ready")
def ready(request: Request):
    db: Database = request.app.state.db
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    return {"status": "ready"}
