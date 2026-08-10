"""
ClickHouse analytics warehouse.

WHY A SEPARATE STORE AT ALL. The ledger's Postgres is tuned for "does this
account have the funds, right now" -- a handful of rows read and written per
transaction, indexed for point lookups. Analytics asks the opposite
question: "what was transaction volume by day, by entry mode, across the
last year", touching millions of rows and almost never a single one. One
engine being genuinely good at both is rare, which is why real systems
export from the operational database into a columnar one.

WHY CLICKHOUSE RATHER THAN REDSHIFT. Redshift was the original plan and was
dropped for a practical reason and a technical one. Practical: it needs an
AWS account, a provisioned cluster, and credentials, none of which were
available -- so the integration would have been correct-shaped, untestable
code, exactly the position `AWSKeyManagementService` is already stuck in.
ClickHouse self-hosts in a container, so this code is genuinely exercised by
CI. Technical: the loading model suits this workload better, as below.

THE LOADING MODEL IS THE OPPOSITE OF REDSHIFT'S. Redshift punishes
row-by-row INSERT and wants COPY FROM S3 -- an S3 bucket, an IAM role, a
staging table, and MERGE grammar. ClickHouse wants large batched INSERTs and
needs none of that. Several hundred lines of planned infrastructure simply
do not exist here, which is a real simplification rather than a shortcut.

TWO TRAPS, BOTH DOCUMENTED WHERE THEY BITE:
  1. ReplacingMergeTree deduplicates EVENTUALLY, not on insert.
  2. A materialized view fires BEFORE that deduplication happens.
Read the notes on ensure_schema() and load_transactions() before changing
either.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Inserted in batches of this size. ClickHouse strongly prefers few large
# inserts to many small ones -- every INSERT creates a "part" on disk that
# the background merge process must later consolidate, so thousands of tiny
# inserts produce thousands of parts and eventually a "too many parts"
# error that stalls ingestion entirely.
DEFAULT_BATCH_SIZE = 50_000

FACT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fact_transactions (
    rrn                String,
    amount_cents       Int64,
    transaction_ts     DateTime64(3, 'UTC'),
    kind               LowCardinality(String),
    debit_account_id   LowCardinality(String),
    credit_account_id  LowCardinality(String),
    loaded_at          DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(loaded_at)
PARTITION BY toYYYYMM(transaction_ts)
ORDER BY (transaction_ts, rrn)
TTL toDateTime(transaction_ts) + INTERVAL 7 YEAR
"""
# ORDER BY (transaction_ts, rrn):
#   - transaction_ts leads because every analytical query here is
#     time-bounded, and the sparse primary index lets ClickHouse skip whole
#     granules outside the range.
#   - rrn completes the key, which makes (ts, rrn) the DEDUPLICATION key.
#     Both components are stable for a given transaction -- transaction_ts
#     comes from the operational row's created_at and never changes -- so
#     re-loading the same RRN collapses correctly.
#
# PARTITION BY toYYYYMM: makes the 7-year TTL a metadata-only DROP PARTITION
# rather than a mass row delete.
#
# LowCardinality: dictionary encoding. These columns have few distinct values
# across millions of rows; it is a large win on both storage and filtering,
# and it is free.

WATERMARK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS etl_watermark (
    table_name     String,
    last_loaded_ts String,
    last_run_id    String,
    rows_loaded    UInt64,
    updated_at     DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY table_name
"""
# last_loaded_ts is a String, not a DateTime.
#
# This is deliberate, and it is the fix for a bug the monolith actually hit:
# comparing timestamps as text across two engines silently broke incremental
# sync, because Postgres's plain TIMESTAMP drops the UTC offset that
# SQLite's string format carries, so every sync re-processed the same rows
# forever. Storing the watermark as the EXACT string the source database
# emitted, and feeding that identical string back as the `since` parameter,
# removes the cross-engine conversion entirely -- there is no parse, so
# there is nothing to lose in translation.

AGG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS agg_daily_volume (
    day        Date,
    account_id LowCardinality(String),
    txn_count  UInt64,
    total_cents Int64
)
ENGINE = SummingMergeTree
ORDER BY (day, account_id)
"""

MATERIALIZED_VIEW_DDL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_volume TO agg_daily_volume AS
SELECT
    toDate(transaction_ts) AS day,
    debit_account_id       AS account_id,
    count()                AS txn_count,
    sum(amount_cents)      AS total_cents
FROM fact_transactions
GROUP BY day, account_id
"""
# The thing ClickHouse is genuinely great at: daily volume becomes a
# single-digit-millisecond read regardless of fact table size, maintained
# incrementally at insert time.
#
# TRAP: this view fires on INSERT, BEFORE ReplacingMergeTree deduplicates.
# Re-loading a duplicate RRN double-counts here even though
# fact_transactions ends up correct. The watermark is therefore not merely
# an optimisation -- it is what keeps these aggregates honest, and that is
# why sync.py refuses to advance it on a failed batch.


class DataWarehouse(ABC):
    """The seam is kept even though there is currently one implementation.
    It is what made replacing Redshift with ClickHouse a contained change
    rather than a rewrite."""

    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def get_watermark(self, table_name: str) -> str | None: ...

    @abstractmethod
    def set_watermark(self, table_name: str, last_ts: str, run_id: str, rows: int) -> None: ...

    @abstractmethod
    def load_transactions(self, rows: list[dict]) -> int: ...

    @abstractmethod
    def query(self, sql: str) -> list: ...


class ClickHouseWarehouse(DataWarehouse):
    """
    Uses clickhouse-connect, the official driver, over HTTP.

    HTTP rather than the native TCP protocol on purpose: it traverses an
    OpenShift Service and Route without special handling, works through
    ordinary ingress, and is far easier to debug with curl when something is
    wrong at 2am.
    """

    def __init__(self, host: str, port: int = 8123, database: str = "analytics",
                 username: str = "default", password: str = "", secure: bool = False):
        import clickhouse_connect

        self._connect = lambda: clickhouse_connect.get_client(
            host=host, port=port, database=database,
            username=username, password=password, secure=secure,
        )
        self._database = database
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self._connect()
        return self._client

    def ensure_schema(self) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self._database}")
        for ddl in (FACT_TABLE_DDL, WATERMARK_TABLE_DDL, AGG_TABLE_DDL, MATERIALIZED_VIEW_DDL):
            self.client.command(ddl)
        log.info("ClickHouse schema ensured")

    def get_watermark(self, table_name: str) -> str | None:
        # FINAL forces deduplication at read time. Without it, a watermark
        # updated twice could return the OLDER row -- and a watermark that
        # goes backwards re-loads rows that are already present, which the
        # materialized view would then double-count.
        result = self.client.query(
            "SELECT last_loaded_ts FROM etl_watermark FINAL WHERE table_name = {t:String}",
            parameters={"t": table_name},
        )
        return result.result_rows[0][0] if result.result_rows else None

    def set_watermark(self, table_name: str, last_ts: str, run_id: str, rows: int) -> None:
        self.client.insert(
            "etl_watermark",
            [[table_name, last_ts, run_id, rows, datetime.now(timezone.utc)]],
            column_names=["table_name", "last_loaded_ts", "last_run_id", "rows_loaded", "updated_at"],
        )

    def load_transactions(self, rows: list[dict], batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        if not rows:
            return 0

        columns = [
            "rrn", "amount_cents", "transaction_ts", "kind",
            "debit_account_id", "credit_account_id", "loaded_at",
        ]
        now = datetime.now(timezone.utc)
        loaded = 0

        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            self.client.insert(
                "fact_transactions",
                [
                    [
                        r["rrn"],
                        int(r["amount_cents"]),
                        _parse_ts(r["created_at"]),
                        r.get("kind", "purchase"),
                        r["debit_account_id"],
                        r["credit_account_id"],
                        now,
                    ]
                    for r in batch
                ],
                column_names=columns,
            )
            loaded += len(batch)
            log.info(f"inserted batch of {len(batch)} ({loaded}/{len(rows)})")

        return loaded

    def count_transactions(self) -> int:
        """FINAL, so the count reflects post-deduplication reality rather
        than however many parts happen to be unmerged at this instant."""
        return self.client.query("SELECT count() FROM fact_transactions FINAL").result_rows[0][0]

    def query(self, sql: str) -> list:
        return self.client.query(sql).result_rows

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _parse_ts(value) -> datetime:
    """
    Source timestamps arrive as strings. Anything without an explicit offset
    is treated as UTC -- never as local time, which would shift every row by
    the pod's timezone and make cross-region reports disagree.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
