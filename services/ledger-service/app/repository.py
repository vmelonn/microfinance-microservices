"""
The ledger's data layer, the only code in the platform that writes money.

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
from mfcommon.identity.msisdn import is_msisdn, normalise


class AccountNotFound(Exception):
    pass


class RrnCollision(Exception):
    """
    An RRN already exists, carrying DIFFERENT details.

    Distinct from a duplicate, which is the idempotency guarantee working. A
    collision is two unrelated transactions that happened to generate the
    same reference, and reporting it as a replay tells the caller their money
    moved when it did not.
    """

    def __init__(self, rrn: str, existing: dict, attempted: dict):
        self.rrn = rrn
        self.existing = existing
        self.attempted = attempted
        super().__init__(
            f"RRN {rrn} already belongs to a different transaction: "
            f"stored {existing}, attempted {attempted}"
        )


class InsufficientFunds(Exception):
    """
    The debit would take an account below zero.

    Its own type rather than a generic error, because the caller has to treat
    it differently from a foreign-key violation or a duplicate RRN: this one
    is the customer's problem and is safe to show them, the others are bugs.
    """

    def __init__(self, account_id: str, balance_cents: int, amount_cents: int):
        self.account_id = account_id
        self.balance_cents = balance_cents
        self.amount_cents = amount_cents
        super().__init__(
            f"Account {account_id} has {balance_cents} cents; "
            f"{amount_cents} would overdraw it."
        )


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
                    -- The customer's phone number, stored normalised. Held
                    -- here as well as in auth-service's users table, which
                    -- looks like duplication and is not: this service cannot
                    -- query that database, and resolving a payee by phone
                    -- number is a ledger operation. It is a deliberate copy
                    -- across a service boundary, kept in step because only
                    -- registration writes it.
                    msisdn      TEXT,
                    -- Only two values, and only one of them carries
                    -- logic: 'system' is what the solvency check reads to
                    -- decide who may go below zero. 'wallet' is everyone
                    -- else. It said 'checking' until an operator asked what
                    -- that meant on a mobile money platform, which was a
                    -- fair question with no good answer.
                    type        TEXT NOT NULL DEFAULT 'wallet',
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
            # MIGRATIONS BEFORE INDEXES. Ordering, not style.
            #
            # CREATE TABLE IF NOT EXISTS is a no-op against a database that
            # already exists, so on an upgraded database the msisdn column
            # only exists once this migration has run. Creating the index
            # first worked on every fresh database, which is every test and
            # every local run, and raised "no such column: msisdn" against
            # the deployed one, killing the container at startup and
            # crashlooping the pod.
            self._migrate_add_msisdn(cur)
            self._migrate_checking_to_wallet(cur)

            # Every transfer to a phone number hits this, so it is not
            # optional at any real volume.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_accounts_msisdn ON accounts(msisdn)"
            )

    def _migrate_add_msisdn(self, cur) -> None:
        """
        CREATE TABLE IF NOT EXISTS does nothing to an accounts table that
        already exists, so a database created before MSISDNs would silently
        lack the column and every transfer to a phone number would fail.

        Existing rows get NULL, which resolves to nothing rather than to the
        wrong account. Those customers can be paid by account ID or card until
        they re-register.
        """
        if "msisdn" in self._account_columns(cur):
            return

        cur.execute("ALTER TABLE accounts ADD COLUMN msisdn TEXT")

        # Re-READ the schema rather than assuming the ALTER did what it said.
        # An earlier version added "msisdn" to the local set and then checked
        # that set, which verifies a variable this method just mutated and
        # can never fail.
        #
        # The failure this guards against is illegible without it: if column
        # detection is ever wrong on some dialect, the next statement in
        # init_schema is an index on this column, and the pod dies with
        # `UndefinedColumn: column "msisdn" does not exist` pointing at a
        # CREATE INDEX line that says nothing about the migration that was
        # supposed to run first. That exact traceback cost a deploy cycle.
        if "msisdn" not in self._account_columns(cur):
            raise RuntimeError(
                "the msisdn migration did not apply: column detection on "
                f"{self.db.dialect} still reports it missing after the ALTER. "
                "Everything below depends on it, so refusing to start."
            )

    def _migrate_checking_to_wallet(self, cur) -> None:
        """
        Rows written before the rename still say 'checking'. A DEFAULT only
        applies to new rows, so without this the console would show two
        different words for the same kind of account and the older ones would
        look like a distinct product.

        Value-only, so it is safe to run repeatedly and needs no schema
        inspection: after the first pass there is simply nothing to update.
        """
        cur.execute(
            self.db.sql("UPDATE accounts SET type = ? WHERE type = ?"),
            ("wallet", "checking"),
        )

    def _account_columns(self, cur) -> set:
        """Column names on `accounts`, per dialect."""
        if self.db.is_postgres:
            # current_schema() matters. Without it this matches ANY schema
            # holding a table called accounts, and the answer to "does the
            # column exist" could come from a table this connection is never
            # going to write to.
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'accounts' AND table_schema = current_schema()
            """)
            return {r[0] for r in cur.fetchall()}

        cur.execute("PRAGMA table_info(accounts)")
        return {r[1] for r in cur.fetchall()}

    # -- system accounts ------------------------------------------------------

    # Money cannot appear from nowhere in a double-entry ledger: crediting a
    # customer requires debiting something. This is that something.
    #
    # It represents value entering the platform from outside, an agent taking
    # cash, a bank transfer, a card load. Its balance is therefore NEGATIVE by
    # design, and its magnitude is the total float customers are holding. That
    # is not a bug to fix; it is the number a treasury team would reconcile
    # against the real bank account backing the wallet.
    FUNDING_ACCOUNT = "acc_system_funding"

    def ensure_system_accounts(self) -> None:
        """Idempotent, called at startup."""
        with self.db.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                self.db.sql("SELECT account_id FROM accounts WHERE account_id = ?"),
                (self.FUNDING_ACCOUNT,),
            )
            if cur.fetchone():
                return
            cur.execute(
                self.db.sql(
                    "INSERT INTO accounts (account_id, user_id, msisdn, type, created_at) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (self.FUNDING_ACCOUNT, "system", None, "system", utc_now_param()),
            )

    # -- identity -------------------------------------------------------------

    def create_account(self, user_id: str, card_number: str, account_type: str = "wallet",
                       msisdn: str | None = None) -> dict:
        account_id = f"acc_{uuid.uuid4().hex[:12]}"
        now = utc_now_param()

        with self.db.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                self.db.sql("INSERT INTO accounts (account_id, user_id, msisdn, type, created_at) "
                            "VALUES (?, ?, ?, ?, ?)"),
                (account_id, user_id, msisdn, account_type, now),
            )
            cur.execute(
                self.db.sql("INSERT INTO cards (card_number, account_id) VALUES (?, ?)"),
                (card_number, account_id),
            )
        return {"account_id": account_id, "user_id": user_id,
                "card_number": card_number, "msisdn": msisdn}

    def resolve_account(self, identifier: str) -> str | None:
        """
        Translates a card number OR a raw account ID into the real account ID.

        Card first, because that is the common path, a purchase carries a
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
            if row:
                return row[0]

            # Finally a phone number. LAST on purpose: a 12 to 15 digit card
            # number is indistinguishable from an MSISDN by length, so trying
            # this first could route a payment to whichever account happened
            # to match. Cards are checked first and this is the fallback.
            if is_msisdn(identifier):
                cur.execute(
                    self.db.sql("SELECT account_id FROM accounts WHERE msisdn = ?"),
                    (normalise(identifier),),
                )
                row = cur.fetchone()
                if row:
                    return row[0]

            return None

    def accounts_for_user(self, user_id: str) -> list[dict]:
        """
        Every account this user owns, with its cards and balance.

        Keyed on user_id rather than on anything the caller supplies, which
        is what makes it safe to return card numbers: there is no identifier
        to tamper with.
        """
        with self.db.cursor() as cur:
            cur.execute(self.db.sql("""
                SELECT a.account_id, a.msisdn, a.type,
                       COALESCE((
                           SELECT SUM(CASE WHEN entry_type='credit' THEN amount_cents
                                           ELSE -amount_cents END)
                           FROM ledger_entries e WHERE e.account_id = a.account_id
                       ), 0)
                FROM accounts a
                WHERE a.user_id = ?
                ORDER BY a.created_at
            """), (user_id,))
            rows = cur.fetchall()

            accounts = []
            for account_id, msisdn, kind, balance in rows:
                cur.execute(
                    self.db.sql("SELECT card_number FROM cards "
                                "WHERE account_id = ? AND status = 'active' "
                                "ORDER BY card_number"),
                    (account_id,),
                )
                accounts.append({
                    "account_id": account_id,
                    "msisdn": msisdn,
                    "type": kind,
                    "balance_cents": int(balance or 0),
                    "cards": [r[0] for r in cur.fetchall()],
                })
        return accounts

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
        correctness, that is the point, but the saga logs which happened.

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

                # Solvency is checked INSIDE the same transaction as the
                # insert, not before it. A check-then-write across two
                # statements is a race: two concurrent purchases can each read
                # a sufficient balance and both post, overdrawing the account.
                #
                # On Postgres the account row is locked FOR UPDATE, so the
                # second transaction blocks until the first commits and then
                # reads the balance the first one produced. On SQLite the
                # equivalent comes from Database.transaction() opening with
                # BEGIN IMMEDIATE; without that the read below runs in
                # autocommit and the race is wide open.
                self._assert_solvent(cur, debit_account, amount_cents)

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
                # A duplicate RRN is one of two very different things, and
                # the difference decides whether the caller may report
                # success. A retry of the SAME posting is the idempotency
                # guarantee working. A DIFFERENT posting that happened to
                # generate the same reference is a collision, and calling
                # that "already recorded" tells somebody their money moved
                # when it did not.
                attempted = {"amount_cents": amount_cents, "kind": kind,
                             "debit": debit_account, "credit": credit_account}
                existing = self._posting_details(rrn)
                if existing is not None and existing != attempted:
                    raise RrnCollision(rrn, existing, attempted) from exc
                return {"status": "already_recorded", "rrn": rrn}
            raise

        return {"status": "recorded", "rrn": rrn, "amount_cents": amount_cents}

    def _posting_details(self, rrn: str) -> dict | None:
        """
        What is already stored under this RRN, in the same shape as an
        attempted posting so the two can be compared directly.

        Returns None if the row cannot be read back, in which case the caller
        treats it as a replay: refusing a posting because a diagnostic query
        failed would turn a read problem into a payment failure.
        """
        try:
            with self.db.cursor() as cur:
                cur.execute(self.db.sql("""
                    SELECT t.amount_cents, t.kind,
                           MAX(CASE WHEN e.entry_type='debit'  THEN e.account_id END),
                           MAX(CASE WHEN e.entry_type='credit' THEN e.account_id END)
                    FROM transactions t
                    LEFT JOIN ledger_entries e ON e.rrn = t.rrn
                    WHERE t.rrn = ?
                    GROUP BY t.amount_cents, t.kind
                """), (rrn,))
                row = cur.fetchone()
        except Exception:  # noqa: BLE001
            return None

        if row is None:
            return None
        return {"amount_cents": int(row[0]), "kind": row[1],
                "debit": row[2], "credit": row[3]}

    def _assert_solvent(self, cur, account_id: str, amount_cents: int) -> None:
        """
        Raises InsufficientFunds unless the account can cover the debit.

        System accounts are exempt: the funding account is where money enters
        the platform, so it is negative by definition and blocking it would
        make top-ups impossible.
        """
        if self.db.is_postgres:
            cur.execute(
                "SELECT type FROM accounts WHERE account_id = %s FOR UPDATE",
                (account_id,),
            )
        else:
            cur.execute(
                "SELECT type FROM accounts WHERE account_id = ?", (account_id,)
            )
        row = cur.fetchone()
        if row is None:
            # Not our error to raise: the foreign key on ledger_entries will
            # reject this in a moment, and record_posting distinguishes that
            # from a duplicate RRN. Pre-empting it here would mislabel it.
            return
        if row[0] == "system":
            return

        cur.execute(
            self.db.sql("""
                SELECT COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount_cents
                                         ELSE -amount_cents END), 0)
                FROM ledger_entries WHERE account_id = ?
            """),
            (account_id,),
        )
        current = int(cur.fetchone()[0] or 0)
        if current - amount_cents < 0:
            raise InsufficientFunds(account_id, current, amount_cents)

    def topup(self, account_id: str, amount_cents: int, rrn: str) -> dict:
        """
        Move money INTO a customer wallet from the funding account.

        A normal double-entry posting, not a special case: the funding account
        is debited and the customer credited, so the ledger still balances and
        the RRN still makes it idempotent. The only thing that makes it a
        top-up is which account is on which side.
        """
        return self.record_posting(
            rrn=rrn,
            debit_account=self.FUNDING_ACCOUNT,
            credit_account=account_id,
            amount_cents=amount_cents,
            kind="topup",
        )

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
        this is ever false, something has gone genuinely wrong, a partial
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
        IDs, only the entries tied to it do.

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

    # -- read-only inspection, for the operator console ---------------------
    #
    # Curated rather than an arbitrary SQL endpoint. This is the money
    # database behind the only public Route, and "it is only SELECT" is not
    # a safety argument: read-only still leaks, and one typo in a guard turns
    # it into something worse. Free-form querying is available against
    # ClickHouse, which is derived and rebuildable.

    def list_accounts(self, limit: int = 100) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute(self.db.sql(f"""
                SELECT a.account_id, a.user_id, a.msisdn, a.type, a.created_at,
                       (SELECT COUNT(*) FROM cards c WHERE c.account_id = a.account_id) AS cards,
                       COALESCE((
                           SELECT SUM(CASE WHEN entry_type='credit' THEN amount_cents
                                           ELSE -amount_cents END)
                           FROM ledger_entries e WHERE e.account_id = a.account_id
                       ), 0) AS balance_cents
                FROM accounts a
                ORDER BY a.created_at DESC
                LIMIT {int(limit)}
            """))
            return [
                {"account_id": r[0], "user_id": r[1], "msisdn": r[2],
                 "type": r[3], "created_at": str(r[4]), "cards": int(r[5]),
                 "balance_cents": int(r[6])}
                for r in cur.fetchall()
            ]

    def list_transactions(self, limit: int = 50) -> list[dict]:
        """Newest first, with both sides of the journal entry joined on."""
        with self.db.cursor() as cur:
            cur.execute(self.db.sql(f"""
                SELECT t.rrn, t.amount_cents, t.kind, t.created_at,
                       d.account_id, c.account_id
                FROM transactions t
                JOIN ledger_entries d ON d.rrn = t.rrn AND d.entry_type = 'debit'
                JOIN ledger_entries c ON c.rrn = t.rrn AND c.entry_type = 'credit'
                ORDER BY t.created_at DESC
                LIMIT {int(limit)}
            """))
            return [
                {"rrn": r[0], "amount_cents": int(r[1]), "kind": r[2],
                 "created_at": str(r[3]), "debit_account_id": r[4],
                 "credit_account_id": r[5]}
                for r in cur.fetchall()
            ]

    def purge(self) -> dict:
        """
        Everything: postings, cards, accounts. Then the funding account is
        recreated, because the service cannot take a top-up without it and a
        wiped platform should be immediately usable rather than needing a
        restart to become so.

        Deletion order follows the foreign keys, children first. Postgres
        would reject the reverse with a constraint violation, and SQLite has
        `PRAGMA foreign_keys = ON` set in the dialect, so it would too.
        """
        with self.db.transaction() as conn:
            cur = conn.cursor()
            counts = {}
            for table in ("ledger_entries", "transactions", "cards", "accounts"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = int(cur.fetchone()[0])
            for table in ("ledger_entries", "transactions", "cards", "accounts"):
                cur.execute(f"DELETE FROM {table}")

        # Outside the transaction above, so the wipe is committed before the
        # funding account is written. Recreating it inside would make the
        # whole thing one unit, and a failure there would silently roll the
        # purge back while reporting counts that no longer happened.
        self.ensure_system_accounts()
        return counts

    def reset(self) -> dict:
        """
        Sandbox only. Wired to an endpoint that production config disables.

        Deletes the POSTINGS, not the accounts. Balances are derived by
        summing ledger_entries, so removing every entry returns every account
        to zero, including the funding account, while leaving customers able
        to log in with the cards and numbers they already have.

        Returns what it removed, so the caller can show something more
        convincing than "ok" after an irreversible operation.
        """
        with self.db.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM ledger_entries")
            entries = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM transactions")
            transactions = int(cur.fetchone()[0])
            cur.execute("DELETE FROM ledger_entries")
            cur.execute("DELETE FROM transactions")
        return {"entries_deleted": entries, "transactions_deleted": transactions}
