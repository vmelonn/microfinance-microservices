"""
analytics-sync -- incremental load from ledger-service into ClickHouse.

Runs as an OpenShift CronJob, not as a long-lived service. It has no HTTP
port, no readiness probe, and exits with a status code the scheduler keys
alerting off. Same shape as the monolith's reconciliation job.

INCREMENTAL, NOT A FULL RELOAD. It asks ClickHouse for the watermark and
pulls only rows newer than it. A full reload is fine at demo scale and a
genuinely bad idea at real scale, so this is built the way it would have to
work rather than the way that is easiest to demonstrate.

IT READS THROUGH ledger-service's API, NOT ITS DATABASE. Reaching straight
into another service's Postgres is faster and is the single most common way
a microservice architecture quietly collapses back into a shared-schema
monolith: the moment this job SELECTs from ledger_entries, ledger-service
can no longer change that table without breaking a job it does not know
exists. The /internal/ledger/export endpoint is a contract; the table is not.

WATERMARK DISCIPLINE. The watermark advances only after a batch has been
confirmed loaded. On failure it stays put and the next run re-reads the same
rows. Re-reading is safe for fact_transactions -- ReplacingMergeTree
collapses the duplicates -- but NOT for the materialized view, which fires
on insert and would double-count. So "advance only on success" is a
correctness requirement here, not tidiness.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

from mfcommon.observability.correlation import configure_logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.warehouse import ClickHouseWarehouse  # noqa: E402

LEDGER_URL = os.environ.get("LEDGER_SERVICE_URL", "http://ledger-service:8084")
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "analytics")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_SECURE = os.environ.get("CLICKHOUSE_SECURE", "0") == "1"

PAGE_SIZE = int(os.environ.get("SYNC_PAGE_SIZE", "10000"))
FACT_TABLE = "fact_transactions"

log = configure_logging("analytics-sync", os.environ.get("LOG_LEVEL", "INFO"))


def fetch_page(since: str | None, limit: int) -> list[dict]:
    import httpx

    params = {"limit": limit}
    if since:
        params["since"] = since

    response = httpx.get(f"{LEDGER_URL}/internal/ledger/export", params=params, timeout=60.0)
    response.raise_for_status()
    return response.json()["rows"]


def main() -> int:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    log.info(f"starting sync {run_id}")

    warehouse = ClickHouseWarehouse(
        host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT, database=CLICKHOUSE_DB,
        username=CLICKHOUSE_USER, password=CLICKHOUSE_PASSWORD, secure=CLICKHOUSE_SECURE,
    )

    try:
        warehouse.ensure_schema()
        watermark = warehouse.get_watermark(FACT_TABLE)
        log.info(f"watermark: {watermark or '(empty -- first run, full load)'}")

        total = 0
        while True:
            rows = fetch_page(watermark, PAGE_SIZE)
            if not rows:
                break

            loaded = warehouse.load_transactions(rows)
            total += loaded

            # The last row's created_at, verbatim. Passed back unparsed on
            # the next call, so no timestamp conversion can lose precision
            # or an offset between the two engines.
            watermark = rows[-1]["created_at"]
            warehouse.set_watermark(FACT_TABLE, watermark, run_id, total)
            log.info(f"advanced watermark to {watermark} after {loaded} rows")

            if len(rows) < PAGE_SIZE:
                break  # last page

        log.info(f"sync {run_id} complete: {total} row(s) loaded, warehouse holds "
                 f"{warehouse.count_transactions()} transaction(s)")
        return 0

    except Exception as exc:  # noqa: BLE001
        # Non-zero exit is what the CronJob's alerting keys off. The
        # watermark was NOT advanced past anything unconfirmed, so the next
        # run resumes from the last good point rather than skipping rows.
        log.error(f"sync {run_id} FAILED: {exc!r}")
        return 1

    finally:
        warehouse.close()


if __name__ == "__main__":
    sys.exit(main())
