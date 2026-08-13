"""
The operator console's backend.

Everything here is a DEBUG SURFACE on a service that has the only public
Route, so the whole router is gated behind ENABLE_CONSOLE and every endpoint
requires a valid token. Unset the variable and none of it exists.

Three capabilities, each with a different safety story:

  TRACES        read-only, from Redis, already masked when written
  LEDGER        read-only, through curated ledger-service endpoints
  SQL           free-form, but ONLY against ClickHouse

That last distinction is the important one. ClickHouse is a derived store
that can be rebuilt from the ledger by rerunning the sync with an empty
watermark, so an arbitrary SELECT there is recoverable at worst. The same
freedom against ledger-service's Postgres would put ad-hoc SQL on the money
database, reachable from the internet, and no amount of "it is only SELECT"
makes that a good idea: read-only still leaks, and one typo in a guard turns
it into something worse.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mfcommon.observability import trace

log = logging.getLogger("api-gateway.console")

router = APIRouter()

CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "analytics")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")

# Only these may start a query. Anything else is refused before it reaches
# ClickHouse, so the readonly setting below is a second line of defence
# rather than the only one.
ALLOWED_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")

MAX_ROWS = 500


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=4000)


# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------

@router.get("/console/traces")
def recent_traces(request: Request):
    """Most recent correlation IDs, newest first."""
    client = getattr(request.app.state, "trace_redis", None)
    if client is None:
        return {"enabled": False, "traces": [],
                "reason": "REDIS_URL is not set, so nothing records traces"}

    entries = []
    for raw in trace.recent_ids(client, limit=30):
        parts = raw.split("|", 2)
        entries.append({
            "correlation_id": parts[0],
            "at": float(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
            "summary": parts[2] if len(parts) > 2 else "",
        })
    return {"enabled": True, "traces": entries}


@router.get("/console/traces/{correlation_id}")
def one_trace(correlation_id: str, request: Request):
    """
    The timeline for a single request: which layer, when, what happened.

    Returned oldest first with a millisecond offset from the first event, so
    the console can show elapsed time without doing date arithmetic in the
    browser.
    """
    client = getattr(request.app.state, "trace_redis", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Tracing is not enabled (no REDIS_URL).")

    events = trace.read(client, correlation_id)
    if not events:
        raise HTTPException(
            status_code=404,
            detail=f"No trace for {correlation_id}. Traces expire after 15 minutes.",
        )

    start = events[0]["ts"]
    for event in events:
        event["offset_ms"] = round((event["ts"] - start) * 1000, 1)

    return {
        "correlation_id": correlation_id,
        "events": events,
        "total_ms": round((events[-1]["ts"] - start) * 1000, 1),
    }


# --------------------------------------------------------------------------
# Ledger inspection, through curated endpoints only
# --------------------------------------------------------------------------

@router.get("/console/ledger/accounts")
def accounts(request: Request):
    return _ledger(request, "/internal/ledger/inspect/accounts")


@router.get("/console/ledger/transactions")
def transactions(request: Request, limit: int = 50):
    return _ledger(request, f"/internal/ledger/inspect/transactions?limit={min(limit, 200)}")


@router.get("/console/ledger/integrity")
def integrity(request: Request):
    """Total debits versus total credits. If this is ever false, stop."""
    return _ledger(request, "/internal/ledger/integrity")


@router.post("/console/ledger/reset")
def reset_ledger(request: Request):
    """
    Wipe every posting and return all balances to zero.

    Proxied rather than exposed directly: ledger-service is not reachable
    from outside the cluster, and its own ALLOW_LEDGER_RESET gate is what
    actually decides whether this works. The gateway adds authentication (the
    whole console router sits behind current_user) but deliberately adds no
    second gate, so there is exactly one place to look when it is refused.
    """
    from mfcommon.http.client import ServiceCallError, ServiceRejectedError

    try:
        result = request.app.state.ledger.post("/internal/ledger/reset", {}, retries=0)
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")

    log.warning("ledger reset requested from the console")
    return result


@router.post("/console/purge")
def purge_platform(request: Request):
    """
    Empty the platform: accounts, cards, postings and users.

    TWO services, two databases, and no distributed transaction across them.
    Both halves are attempted regardless of what the first one does, and the
    response reports each separately, because the useful question after a
    partial failure is "which half" and a single ok/failed cannot answer it.

    Attempting both rather than stopping at the first failure is deliberate.
    The operator asked for an empty platform; getting them most of the way
    there and naming what is left beats stopping early and leaving a state
    they now have to work out for themselves. Both halves are idempotent, so
    running it again is always safe and is the fix for any partial result.

    Ledger first. If only one half lands, orphaned users who cannot reach
    anything are a tidier state than accounts nobody can log in to, and the
    error message can say so.
    """
    from mfcommon.http.client import ServiceCallError, ServiceRejectedError

    outcome = {}
    for name, client, path in (
        ("ledger", request.app.state.ledger, "/internal/ledger/purge"),
        ("auth", request.app.state.auth, "/internal/auth/purge"),
    ):
        try:
            outcome[name] = {"ok": True, **client.post(path, {}, retries=0)}
        except ServiceRejectedError as exc:
            # Almost always the config gate: the flag is off in this
            # environment. Say that rather than echoing a bare 403.
            outcome[name] = {"ok": False, "status_code": exc.status_code,
                             "error": str(exc.detail)}
        except ServiceCallError as exc:
            outcome[name] = {"ok": False, "error": f"{name}-service unreachable: {exc}"}

    everything = all(part["ok"] for part in outcome.values())
    log.warning(f"PLATFORM PURGE requested from the console: {outcome}")

    if not everything:
        failed = [n for n, part in outcome.items() if not part["ok"]]
        raise HTTPException(status_code=207, detail={
            "error": "partial_purge",
            "failed": failed,
            "message": ("The platform is now in a mixed state. Running this "
                        "again is safe and is the fix."),
            "detail": outcome,
        })

    return {"status": "purged", **outcome}


def _ledger(request: Request, path: str):
    from mfcommon.http.client import ServiceCallError, ServiceRejectedError

    try:
        return request.app.state.ledger.get(path, retries=1)
    except ServiceRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail))
    except ServiceCallError as exc:
        raise HTTPException(status_code=503, detail=f"Ledger unavailable: {exc}")


# --------------------------------------------------------------------------
# Ad-hoc SQL, ClickHouse only
# --------------------------------------------------------------------------

@router.post("/console/query")
def run_query(body: QueryRequest):
    """
    Runs a read-only query against ClickHouse.

    Four guards, deliberately layered rather than trusting any single one:

      1. the statement must START with a read-only keyword
      2. semicolons are rejected, so nothing can be chained onto the end
      3. ClickHouse itself runs the session with readonly=1
      4. max_execution_time caps a runaway scan

    None of this makes arbitrary SQL safe in general. It is acceptable here
    only because ClickHouse holds derived analytics that can be rebuilt from
    the ledger, and never the ledger itself.
    """
    import httpx

    sql = body.sql.strip().rstrip(";").strip()
    lowered = sql.lower()

    if not lowered.startswith(ALLOWED_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"Only {', '.join(p.upper() for p in ALLOWED_PREFIXES)} are allowed here.",
        )
    if ";" in sql:
        # A trailing one was already stripped, so any remaining semicolon is
        # an attempt to chain a second statement.
        raise HTTPException(status_code=400, detail="Multiple statements are not allowed.")

    # Add a LIMIT if the query has none, so a bare `SELECT * FROM
    # fact_transactions` cannot try to return the whole table to a browser.
    if lowered.startswith(("select", "with")) and not re.search(r"\blimit\s+\d+", lowered):
        sql = f"{sql} LIMIT {MAX_ROWS}"

    url = f"http://{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/"
    params = {
        "database": CLICKHOUSE_DB,
        "default_format": "JSONCompact",
        "readonly": "1",
        "max_execution_time": "10",
        "max_result_rows": str(MAX_ROWS),
    }

    try:
        response = httpx.post(
            url, params=params, content=sql.encode(),
            auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD),
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"ClickHouse unreachable: {exc}")

    if response.status_code != 200:
        # ClickHouse returns a genuinely useful plain-text error. Passing it
        # through beats replacing it with "query failed".
        raise HTTPException(status_code=400, detail=response.text.strip()[:600])

    payload = response.json()
    return {
        "columns": [c["name"] for c in payload.get("meta", [])],
        "rows": payload.get("data", []),
        "row_count": len(payload.get("data", [])),
        "elapsed_ms": round(payload.get("statistics", {}).get("elapsed", 0) * 1000, 1),
        "sql": sql,
    }


@router.get("/console/query/schema")
def schema():
    """The tables and columns available, so the query box is usable without
    reading analytics/warehouse.py first."""
    return {
        "tables": {
            "fact_transactions": [
                "rrn", "amount_cents", "transaction_ts", "kind",
                "debit_account_id", "credit_account_id", "loaded_at",
            ],
            "agg_daily_volume": ["day", "account_id", "txn_count", "total_cents"],
            "etl_watermark": [
                "table_name", "last_loaded_ts", "last_run_id", "rows_loaded", "updated_at",
            ],
        },
        "notes": [
            "ClickHouse lags the ledger by up to the sync interval (15 min in dev).",
            "fact_transactions is a ReplacingMergeTree: use FINAL to avoid seeing "
            "duplicates that have not been merged away yet.",
            "agg_daily_volume is maintained by a materialized view that fires on "
            "INSERT, before deduplication.",
        ],
        "examples": [
            # -- getting your bearings ------------------------------------
            "SHOW TABLES",
            "DESCRIBE fact_transactions",
            "SELECT count() FROM fact_transactions FINAL",
            "SELECT * FROM fact_transactions FINAL ORDER BY transaction_ts DESC LIMIT 20",

            # -- where the money went -------------------------------------
            "SELECT kind, count() AS txns, sum(amount_cents)/100 AS total "
            "FROM fact_transactions FINAL GROUP BY kind ORDER BY total DESC",

            "SELECT debit_account_id, count() AS txns, sum(amount_cents)/100 AS spent "
            "FROM fact_transactions FINAL GROUP BY debit_account_id "
            "ORDER BY spent DESC LIMIT 10",

            # The funding account is the debit side of every top-up, so this
            # is the float customers are holding, from the warehouse side.
            "SELECT sum(amount_cents)/100 AS float_issued FROM fact_transactions FINAL "
            "WHERE debit_account_id = 'acc_system_funding'",

            # -- shape over time ------------------------------------------
            "SELECT toStartOfHour(transaction_ts) AS hour, count() AS txns, "
            "sum(amount_cents)/100 AS total FROM fact_transactions FINAL "
            "GROUP BY hour ORDER BY hour DESC LIMIT 24",

            "SELECT day, sum(total_cents)/100 AS total FROM agg_daily_volume "
            "GROUP BY day ORDER BY day",

            # -- the traps, made visible ----------------------------------
            # ReplacingMergeTree deduplicates on a background merge, not on
            # insert. If these two disagree, unmerged duplicates are sitting
            # in the table and any query WITHOUT FINAL is over-counting.
            "SELECT (SELECT count() FROM fact_transactions) AS raw_rows, "
            "(SELECT count() FROM fact_transactions FINAL) AS deduplicated",

            # The materialized view fires on INSERT, before deduplication, so
            # agg_daily_volume can exceed the deduplicated fact table. That is
            # a property of the design, not a bug, and this is how you see it.
            "SELECT (SELECT sum(txn_count) FROM agg_daily_volume) AS from_the_view, "
            "(SELECT count() FROM fact_transactions FINAL) AS from_the_facts",

            # An RRN should identify one transaction. More than one row per
            # RRN after FINAL means two different transactions collided on a
            # reference, which the ledger now refuses but older rows may show.
            "SELECT rrn, count() AS rows FROM fact_transactions FINAL "
            "GROUP BY rrn HAVING rows > 1 ORDER BY rows DESC",

            # -- is the pipeline actually running? ------------------------
            # last_loaded_ts is a String on purpose: it is the exact text the
            # source emitted, fed back unchanged, so nothing is lost to a
            # cross-engine timestamp parse.
            "SELECT * FROM etl_watermark FINAL",

            "SELECT table_name, rows_loaded, updated_at, "
            "dateDiff('minute', updated_at, now64(3)) AS minutes_since_last_sync "
            "FROM etl_watermark FINAL",

            # -- storage ---------------------------------------------------
            "SELECT partition, sum(rows) AS rows, "
            "formatReadableSize(sum(bytes_on_disk)) AS on_disk "
            "FROM system.parts WHERE table = 'fact_transactions' AND active "
            "GROUP BY partition ORDER BY partition",
        ],
    }
