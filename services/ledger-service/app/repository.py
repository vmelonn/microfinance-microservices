"""
The ledger's data layer -- the only code in the platform that writes money.

The single most important line in this file is the PRIMARY KEY on
transactions.rrn. Everything upstream (the gateway's idempotency claim, the
saga's retry policy, the SOAP client's reversal-on-timeout) is a
best-effort attempt to avoid double-processing. This constraint is the one
that cannot be wrong. It is enforced by the database, in a single atomic
statement, and it holds regardless of how many replicas race.

That property is why record_posting() is safe to retry from anywhere, and
why transaction-service can retry a failed ledger call without reasoning
about whether the first attempt got through.
"""

from __future__ import annotations

import uuid

from mfcommon.db.dialect import Database, utc_now_param


class AccountNotFound(Exception):
    pass


class LedgerRepository:
    def __init__(self, db: Database):
        self.db = db

    # -- schema -------------------------------------------------------------

    def init_schema(self) -> None:
        ts = self.db.timestamp_type
        pk = self.db.autoincrement_pk

        with self.db.transaction() as conn:
            cur = conn.cursor()
            # accounts.user_id is NOT a foreign key to a users table, because
            # users live in auth-service's OWN database. Referential
            # integrity across a service boundary cannot be enforced by the
            # database; it is enforced by the fact that only auth-service
            # creates users, and account creation is driven by an event from
            # it. Pretending otherwise with a cross-database FK is how a
            # microservice split ends up with a shared schema.
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id  TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    type        TEXT NOT NULL DEFAULT 'checking',
                    created_at  {ts} NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cards (
                    card_number TEXT PRIMARY KEY,
                    account_id  TEXT NOT NULL REFERENCES accounts(account_id),
                    status      TEXT NOT NULL DEFAULT 'active'
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS transactions (
                    rrn           TEXT PRIMARY KEY,
                    amount_cents  BIGINT NOT NULL,
                    kind          TEXT NOT NULL DEFAULT 'purchase',
                    created_at    {ts} NOT NULL
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id           {pk},
                    rrn          TEXT NOT NULL REFERENCES transactions(rrn),
                    account_id   TEXT NOT NULL REFERENCES accounts(account_id),
                    entry_type   TEXT NOT NULL CHECK (entry_type IN ('debit','credit')),
                    amount_cents BIGINT NOT NULL
                )
            """)
            # Balance is computed by summing this account's entries, so
            # without this index every balance check is a full table scan.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_entries_account "
                "ON ledger_entries(account_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_account ON cards(account_id)"
            )

    # -- identity -----------------------------------------------------------

    def create_account(self, user_id: str, card_number: str, account_type: str = "checking") -> dict:
        account_id = f"acc_{uuid.uuid4().hex[:12]}"
        now = utc_now_param()

        with self.db.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                self.db.sql("INSERT INTO accounts (account_id, user_id, type, created_at) VALUES (?, ?, ?, ?)"),
                (account_id, user_id, account_type, now),
            )
            cur.execute(
                self.db.sql("INSERT INTO cards (card_number, account_id) VALUES (?, ?)"),
                (card_number, account_id),
            )
        return {"account_id": account_id, "user_id": user_id, "card_number": card_number}

    def resolve_account(self, identifier: str) -> str | None:
        """
        Translates a card number OR a raw account ID into the real account ID.

        Card first, because that is the common path -- a purchase carries a
        PAN, not an account ID.
        """
        with self.db.cursor() as cur:
            cur.execute(
                self.db.sql("SELECT account_id FROM cards WHERE card_number = ?"), (identifier,)
            )
            row = cur.fetchone()
            if row:
                return row[0]

            cur.execute(
                self.db.sql("SELECT account_id FROM accounts WHERE account_id = ?"), (identifier,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def owner_of(self, account_id: str) -> str | None:
        with self.db.cursor() as cur:
            cur.execute(
                self.db.sql("SELECT user_id FROM accounts WHERE account_id = ?"), (account_id,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    # -- money --------------------------------------------------------------

    def record_posting(
        self, rrn: str, debit_account: str, credit_account: str, amount_cents: int, kind: str = "purchase"
    ) -> dict:
        """
        Writes one balanced journal entry: a debit and a matching credit,
        both tied to the same RRN, in one atomic transaction.

        Returns status "recorded" on first write, "already_recorded" if this
        RRN was seen before. The caller does not need to distinguish them for
        correctness -- that is the point -- but the saga logs which happened.

        The IntegrityError handling here is the subtle part. SQLite raises
        the SAME exception type for a duplicate primary key and for a
        foreign-key violation. Only the first is safe to report as
        "already_recorded". Swallowing the second would turn "you posted to
        an account that does not exist" into a reassuring success message,
        and the money would simply not be recorded anywhere.
        """
        now = utc_now_param()
        try:
            with self.db.transaction() as conn:
                cur = conn.cursor()
                cur.execute(
                    self.db.sql(
                        "INSERT INTO transactions (rrn, amount_cents, kind, created_at) VALUES (?, ?, ?, ?)"
                    ),
                    (rrn, amount_cents, kind, now),
                )
                cur.execute(
                    self.db.sql(
                        "INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) "
                        "VALUES (?, ?, 'debit', ?)"
                    ),
                    (rrn, debit_account, amount_cents),
                )
                cur.execute(
                    self.db.sql(
                        "INSERT INTO ledger_entries (rrn, account_id, entry_type, amount_cents) "
                        "VALUES (?, ?, 'credit', ?)"
                    ),
                    (rrn, credit_account, amount_cents),
                )
        except Exception as exc:
            if self.db.is_unique_violation(exc, "transactions.rrn"):
                return {"status": "already_recorded", "rrn": rrn}
            raise

        return {"status": "recorded", "rrn": rrn, "amount_cents": amount_cents}

    def balance(self, account_id: str) -> int:
        """Credits minus debits, in cents."""
        with self.db.cursor() as cur:
            cur.execute(
                self.db.sql("""
                    SELECT
                        COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount_cents ELSE 0 END), 0) -
                        COALESCE(SUM(CASE WHEN entry_type='debit'  THEN amount_cents ELSE 0 END), 0)
                    FROM ledger_entries WHERE account_id = ?
                """),
                (account_id,),
            )
            return int(cur.fetchone()[0] or 0)

    def is_balanced(self) -> bool:
        """
        Across the WHOLE ledger, total debits must equal total credits. If
        this is ever false, something has gone genuinely wrong -- a partial
        write, a bug, or tampering. Exposed as an endpoint so the
        reconciliation job can assert it on a schedule rather than only in
        tests.
        """
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type='debit'  THEN amount_cents ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount_cents ELSE 0 END), 0)
                FROM ledger_entries
            """)
            debits, credits = cur.fetchone()
            return int(debits) == int(credits)

    def find_transaction(self, rrn: str) -> dict | None:
        with self.db.cursor() as cur:
            cur.execute(
                self.db.sql("SELECT rrn, amount_cents, kind, created_at FROM transactions WHERE rrn = ?"),
                (rrn,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "rrn": row[0],
                "amount_cents": int(row[1]),
                "kind": row[2],
                "created_at": str(row[3]),
            }

    def export_since(self, since: str | None, limit: int = 50_000) -> list[dict]:
        """
        Feeds analytics-sync. Joins each transaction to its debit and credit
        entries, because the transactions table alone does not carry account
        IDs -- only the entries tied to it do.

        Ordered by created_at so the caller's watermark advances
        monotonically; without ORDER BY, a partial batch would leave the
        watermark pointing past rows that were never read.
        """
        query = """
            SELECT t.rrn, t.amount_cents, t.created_at, t.kind,
                   d.account_id AS debit_account_id,
                   c.account_id AS credit_account_id
            FROM transactions t
            JOIN ledger_entries d ON d.rrn = t.rrn AND d.entry_type = 'debit'
            JOIN ledger_entries c ON c.rrn = t.rrn AND c.entry_type = 'credit'
        """
        params: tuple = ()
        if since:
            query += " WHERE t.created_at > ?"
            params = (since,)
        query += f" ORDER BY t.created_at ASC LIMIT {int(limit)}"

        with self.db.cursor() as cur:
            cur.execute(self.db.sql(query), params)
            return [
                {
                    "rrn": r[0],
                    "amount_cents": int(r[1]),
                    "created_at": str(r[2]),
                    "kind": r[3],
                    "debit_account_id": r[4],
                    "credit_account_id": r[5],
                }
                for r in cur.fetchall()
            ]

    def reset(self) -> None:
        """Sandbox only. Wired to an endpoint that production config disables."""
        with self.db.transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM ledger_entries")
            cur.execute("DELETE FROM transactions")
