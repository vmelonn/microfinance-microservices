"""
Ledger correctness tests.

The concurrency test is the one that matters. Everything else in the platform
-- the gateway's idempotency claim, the saga's retry policy, the SOAP
client's reversal-on-timeout, is best-effort protection against
double-processing. The PRIMARY KEY on transactions.rrn is the guarantee that
cannot be wrong, and this is where it is proven.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from mfcommon.db.dialect import Database


# What the `accounts` fixture credits Alice with before any test runs.
OPENING_BALANCE = 1_000_000


@pytest.fixture
def repo(tmp_path):
    from app.repository import LedgerRepository

    repository = LedgerRepository(Database(str(tmp_path / "ledger.db")))
    repository.init_schema()
    repository.ensure_system_accounts()
    return repository


@pytest.fixture
def accounts(repo):
    """
    Alice arrives FUNDED, because since overdraft protection landed an empty
    wallet cannot spend at all and every posting test would be testing the
    refusal instead of the thing it is named after. The funding is a real
    top-up through the real code path, not a hand-written row.
    """
    alice = repo.create_account("usr_alice", "4111111111111111")
    merchant = repo.create_account("usr_merchant", "merchant:demo")
    repo.topup(alice["account_id"], OPENING_BALANCE, "rrnfund00001")
    return alice, merchant


def test_posting_creates_a_balanced_pair(repo, accounts):
    alice, merchant = accounts

    result = repo.record_posting("rrn000000001", alice["account_id"], merchant["account_id"], 5000)

    assert result["status"] == "recorded"
    assert repo.balance(alice["account_id"]) == OPENING_BALANCE - 5000
    assert repo.balance(merchant["account_id"]) == 5000
    assert repo.is_balanced()


def test_duplicate_rrn_is_a_no_op_not_a_second_posting(repo, accounts):
    alice, merchant = accounts

    first = repo.record_posting("rrn000000002", alice["account_id"], merchant["account_id"], 5000)
    second = repo.record_posting("rrn000000002", alice["account_id"], merchant["account_id"], 5000)

    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"
    # The balance moved once, not twice. This is what makes the saga's retry
    # of a lost ledger response safe.
    assert repo.balance(alice["account_id"]) == OPENING_BALANCE - 5000


def test_ten_concurrent_writers_on_one_rrn_produce_exactly_one_posting(repo, accounts):
    """
    The guarantee everything else leans on.

    Ten threads race to post the same RRN. Exactly one must win; the other
    nine must be told "already recorded". If this ever fails, every
    idempotency mechanism above it is decoration.
    """
    alice, merchant = accounts
    results = []
    errors = []
    barrier = threading.Barrier(10)

    def attempt():
        try:
            barrier.wait(timeout=10)  # maximise genuine contention
            results.append(
                repo.record_posting("rrn000000003", alice["account_id"], merchant["account_id"], 2500)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent posting raised: {errors}"
    recorded = [r for r in results if r["status"] == "recorded"]
    duplicates = [r for r in results if r["status"] == "already_recorded"]

    assert len(recorded) == 1, f"expected exactly one write, got {len(recorded)}"
    assert len(duplicates) == 9
    assert repo.balance(alice["account_id"]) == OPENING_BALANCE - 2500
    assert repo.is_balanced()


def test_posting_to_a_nonexistent_account_raises_rather_than_reporting_success(repo, accounts):
    """
    SQLite raises the SAME IntegrityError for a duplicate primary key and for
    a foreign-key violation. Only the first means "already recorded".
    Swallowing the second would report success while the money went nowhere
    -- which is strictly worse than an error, because nothing would ever
    catch it.
    """
    _alice, merchant = accounts

    with pytest.raises(Exception) as exc_info:
        repo.record_posting("rrn000000004", "acc_does_not_exist", merchant["account_id"], 1000)

    assert "already_recorded" not in str(exc_info.value)
    assert repo.find_transaction("rrn000000004") is None


def test_ledger_stays_balanced_across_many_postings(repo, accounts):
    alice, merchant = accounts
    for i in range(50):
        repo.record_posting(f"rrn{i:09d}", alice["account_id"], merchant["account_id"], 100 + i)

    assert repo.is_balanced()
    assert repo.balance(alice["account_id"]) == OPENING_BALANCE - sum(100 + i for i in range(50))


def test_resolve_finds_an_account_by_card_or_by_id(repo, accounts):
    alice, _merchant = accounts
    assert repo.resolve_account("4111111111111111") == alice["account_id"]
    assert repo.resolve_account(alice["account_id"]) == alice["account_id"]
    assert repo.resolve_account("not-a-real-identifier") is None


def test_export_is_ordered_and_respects_the_watermark(repo, accounts):
    """
    analytics-sync advances its watermark to the last row's created_at.
    Without ORDER BY, a partial batch would leave the watermark pointing past
    rows that were never read, and they would be skipped forever.
    """
    alice, merchant = accounts
    for i in range(5):
        repo.record_posting(f"rrnexport{i:03d}", alice["account_id"], merchant["account_id"], 1000)

    # [1:] skips the fixture's opening top-up, which is a real posting and
    # correctly appears in the export.
    rows = repo.export_since(None)[1:]
    assert len(rows) == 5
    timestamps = [r["created_at"] for r in rows]
    assert timestamps == sorted(timestamps), "export must be ordered by created_at"

    # Resuming from the second row's timestamp yields only what follows it.
    later = repo.export_since(rows[1]["created_at"])
    assert len(later) == 3
    assert all(r["created_at"] > rows[1]["created_at"] for r in later)


def test_export_carries_both_account_ids(repo, accounts):
    """The transactions table alone has no account IDs, only the entries
    tied to it do, which is why export joins them."""
    alice, merchant = accounts
    repo.record_posting("rrnjoin00001", alice["account_id"], merchant["account_id"], 750)

    row = repo.export_since(None)[1]   # [0] is the fixture's opening top-up
    assert row["debit_account_id"] == alice["account_id"]
    assert row["credit_account_id"] == merchant["account_id"]
    assert row["amount_cents"] == 750


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DSN", str(tmp_path / "http-ledger.db"))
    monkeypatch.setenv("ALLOW_LEDGER_RESET", "0")

    import importlib

    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as test_client:
        yield test_client


def test_posting_endpoint_is_idempotent(client):
    client.post("/internal/ledger/accounts", json={"user_id": "u1", "card_number": "4111111111111111"})
    client.post("/internal/ledger/accounts", json={"user_id": "u2", "card_number": "merchant:demo"})

    debit = client.post("/internal/ledger/resolve", json={"identifier": "4111111111111111"}).json()
    credit = client.post("/internal/ledger/resolve", json={"identifier": "merchant:demo"}).json()

    client.post("/internal/ledger/topup", json={
        "rrn": "rrnfundhttp1", "account_id": debit["account_id"], "amount_cents": 10_000,
    })

    payload = {
        "rrn": "rrnhttp00001",
        "debit_account": debit["account_id"],
        "credit_account": credit["account_id"],
        "amount_cents": 4200,
    }

    assert client.post("/internal/ledger/postings", json=payload).json()["status"] == "recorded"
    assert client.post("/internal/ledger/postings", json=payload).json()["status"] == "already_recorded"

    balance = client.get(f"/internal/ledger/accounts/{debit['account_id']}/balance").json()
    assert balance["balance_cents"] == 10_000 - 4200


def test_posting_to_a_missing_account_returns_422_not_a_500(client):
    """A 5xx would be retried by the caller's HTTP client; a 422 correctly
    signals that retrying will not help."""
    response = client.post(
        "/internal/ledger/postings",
        json={
            "rrn": "rrnhttp00002",
            "debit_account": "acc_nope",
            "credit_account": "acc_also_nope",
            "amount_cents": 100,
        },
    )
    assert response.status_code == 422


def test_reset_is_refused_when_not_explicitly_enabled(client):
    """The monolith's equivalent endpoint let any authenticated user wipe the
    whole ledger. Config-gating is not authorization, but it does keep the
    endpoint from existing at all in environments that never enable it."""
    assert client.post("/internal/ledger/reset").status_code == 403


def test_duplicate_card_registration_is_rejected(client):
    client.post("/internal/ledger/accounts", json={"user_id": "u1", "card_number": "4222222222222222"})
    second = client.post(
        "/internal/ledger/accounts", json={"user_id": "u2", "card_number": "4222222222222222"}
    )
    assert second.status_code == 409

# ---------------------------------------------------------------------------
# Solvency
#
# Before this existed, every customer balance in the platform was negative.
# Nothing ever credited a wallet, so purchases only subtracted, and the demo
# database showed everyone thousands of rupees in debt. The fix is not a
# validation rule bolted on top, it is the pair of facts below: money enters
# through a funding account, and a debit that would overdraw is refused
# inside the same transaction that would otherwise have written it.
# ---------------------------------------------------------------------------

def test_a_new_wallet_starts_at_zero_and_cannot_spend(repo):
    from app.repository import InsufficientFunds

    bob = repo.create_account("usr_bob", "4222222222222222")
    merchant = repo.create_account("usr_m2", "merchant:two")
    assert repo.balance(bob["account_id"]) == 0

    with pytest.raises(InsufficientFunds):
        repo.record_posting("rrnbroke00001", bob["account_id"], merchant["account_id"], 1)


def test_the_refused_posting_leaves_no_trace(repo):
    """
    A rejected debit must not write a half-posting. If the transactions row
    survived, its RRN would be taken, and the retry after the customer tops
    up would come back "already_recorded" while no money had moved.
    """
    from app.repository import InsufficientFunds

    bob = repo.create_account("usr_bob", "4222222222222222")
    merchant = repo.create_account("usr_m2", "merchant:two")

    with pytest.raises(InsufficientFunds):
        repo.record_posting("rrnrollback01", bob["account_id"], merchant["account_id"], 5000)

    assert repo.export_since(None) == []
    assert repo.is_balanced()

    repo.topup(bob["account_id"], 5000, "rrnfundbob001")
    assert repo.record_posting(
        "rrnrollback01", bob["account_id"], merchant["account_id"], 5000
    )["status"] == "recorded"
    assert repo.balance(bob["account_id"]) == 0


def test_topup_moves_money_from_the_funding_account(repo):
    bob = repo.create_account("usr_bob", "4222222222222222")

    repo.topup(bob["account_id"], 25_000, "rrntopup00001")

    assert repo.balance(bob["account_id"]) == 25_000
    # The mirror image, and the reason the ledger still balances: the float
    # customers hold is exactly what the funding account owes.
    assert repo.balance(repo.FUNDING_ACCOUNT) == -25_000
    assert repo.is_balanced()


def test_the_funding_account_is_allowed_to_go_negative(repo):
    """It is the one account that must, or no money could ever enter."""
    bob = repo.create_account("usr_bob", "4222222222222222")
    for i in range(5):
        repo.topup(bob["account_id"], 10_000, f"rrnfloat{i:05d}")
    assert repo.balance(repo.FUNDING_ACCOUNT) == -50_000


def test_topup_is_idempotent_on_rrn(repo):
    """A retried top-up after a timeout must not credit twice. The same
    PRIMARY KEY that protects purchases, exercised on the way in."""
    bob = repo.create_account("usr_bob", "4222222222222222")

    first = repo.topup(bob["account_id"], 7_500, "rrntopupdup1")
    second = repo.topup(bob["account_id"], 7_500, "rrntopupdup1")

    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"
    assert repo.balance(bob["account_id"]) == 7_500


def test_spending_exactly_the_balance_is_allowed(repo):
    """The boundary, pinned deliberately. An off-by-one here either strands
    the last rupee in every wallet or permits a one-rupee overdraft."""
    bob = repo.create_account("usr_bob", "4222222222222222")
    merchant = repo.create_account("usr_m2", "merchant:two")
    repo.topup(bob["account_id"], 5_000, "rrnexact00001")

    repo.record_posting("rrnexact00002", bob["account_id"], merchant["account_id"], 5_000)
    assert repo.balance(bob["account_id"]) == 0


def test_concurrent_spends_cannot_overdraw(repo):
    """
    THE test for this feature.

    A sequential balance check is easy and useless: the failure mode is two
    debits reading the same sufficient balance at the same moment and both
    posting. Ten threads each try to spend the entire balance; at most one
    may succeed, and the balance must never end below zero.

    On SQLite the writer lock serialises them. On Postgres the SELECT FOR
    UPDATE in _assert_solvent does. Either way the guarantee comes from the
    database rather than from Python, which is what this proves.
    """
    from app.repository import InsufficientFunds

    bob = repo.create_account("usr_bob", "4222222222222222")
    merchant = repo.create_account("usr_m2", "merchant:two")
    # A prefix the spending threads cannot generate. An earlier version funded
    # with "rrnrace000001", which thread 1 then reused: it passed the solvency
    # check, collided on the transactions primary key, and came back
    # "already_recorded". Correct behaviour, but the assertion below counted
    # it as a second posting and the test failed on Linux roughly one run in
    # eight while the ledger was doing exactly the right thing.
    repo.topup(bob["account_id"], 10_000, "rrnfundrace1")

    outcomes, lock = [], threading.Lock()

    def spend(n):
        try:
            # The RETURNED status, not a hardcoded string. Hardcoding is what
            # let an idempotent replay masquerade as a successful posting, and
            # it meant the assertion was not measuring what it claimed to.
            result = repo.record_posting(f"rrnrace{n:06d}", bob["account_id"],
                                         merchant["account_id"], 10_000)["status"]
        except InsufficientFunds:
            result = "refused"
        except Exception as exc:  # noqa: BLE001
            result = f"error:{exc!r}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("recorded") == 1, outcomes
    assert not [o for o in outcomes if o.startswith("error")], outcomes

    # The invariant, checked against the database rather than against what the
    # threads reported. A wrong outcome label cannot fool this.
    purchases = [r for r in repo.export_since(None) if r["kind"] == "purchase"]
    assert len(purchases) == 1, purchases
    assert repo.balance(bob["account_id"]) == 0
    assert repo.is_balanced()


def test_the_http_surface_returns_409_not_422(client):
    """
    409 is load-bearing. transaction-service compensates with a switch
    reversal when the ledger rejects a posting, and it has to tell an
    unaffordable debit apart from a nonexistent account. A 422 for both
    would make the two indistinguishable.
    """
    client.post("/internal/ledger/accounts", json={"user_id": "u1", "card_number": "4111111111111111"})
    client.post("/internal/ledger/accounts", json={"user_id": "u2", "card_number": "merchant:demo"})
    debit = client.post("/internal/ledger/resolve", json={"identifier": "4111111111111111"}).json()
    credit = client.post("/internal/ledger/resolve", json={"identifier": "merchant:demo"}).json()

    response = client.post("/internal/ledger/postings", json={
        "rrn": "rrn409000001",
        "debit_account": debit["account_id"],
        "credit_account": credit["account_id"],
        "amount_cents": 100,
    })

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "insufficient_funds"
    assert response.json()["detail"]["balance_cents"] == 0


def test_reset_returns_every_balance_to_zero(client, monkeypatch):
    """What an operator actually wants from a reset: logins and cards intact,
    every balance back at zero."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "ALLOW_RESET", True)

    client.post("/internal/ledger/accounts", json={"user_id": "u1", "card_number": "4111111111111111"})
    account = client.post("/internal/ledger/resolve", json={"identifier": "4111111111111111"}).json()
    client.post("/internal/ledger/topup", json={
        "rrn": "rrnresetfund", "account_id": account["account_id"], "amount_cents": 90_000,
    })
    balance_url = f"/internal/ledger/accounts/{account['account_id']}/balance"
    assert client.get(balance_url).json()["balance_cents"] == 90_000

    body = client.post("/internal/ledger/reset").json()
    assert body["status"] == "reset"
    assert body["transactions_deleted"] == 1

    assert client.get(balance_url).json()["balance_cents"] == 0
    # The card still resolves: accounts survive a reset, only postings go.
    assert client.post("/internal/ledger/resolve",
                       json={"identifier": "4111111111111111"}).status_code == 200


# ---------------------------------------------------------------------------
# Upgrading an EXISTING database
#
# Every test above starts from an empty file, so they all exercise
# CREATE TABLE. A deployed database never does: it was created by an earlier
# release, it already holds rows, and CREATE TABLE IF NOT EXISTS is a no-op
# against it. That gap is why a schema change can pass every test and then
# crashloop on the cluster.
# ---------------------------------------------------------------------------

def _pre_msisdn_database(path):
    """
    The schema exactly as it shipped before phone numbers, with a customer
    already in it.

    Lifted verbatim from 18bb26e^ rather than written from memory. An earlier
    version of this fixture invented a created_at column on ledger_entries
    that never existed, and the resulting NOT NULL failure looked like a
    product bug for several minutes. A fixture that claims to be a past
    release has to actually be one.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (
            account_id  TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'checking',
            created_at  TIMESTAMP NOT NULL
        );
        CREATE TABLE cards (
            card_number TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES accounts(account_id),
            status      TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE transactions (
            rrn           TEXT PRIMARY KEY,
            amount_cents  BIGINT NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'purchase',
            created_at    TIMESTAMP NOT NULL
        );
        CREATE TABLE ledger_entries (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rrn          TEXT NOT NULL REFERENCES transactions(rrn),
            account_id   TEXT NOT NULL REFERENCES accounts(account_id),
            entry_type   TEXT NOT NULL CHECK (entry_type IN ('debit','credit')),
            amount_cents BIGINT NOT NULL
        );
        CREATE INDEX idx_ledger_entries_account ON ledger_entries(account_id);
        CREATE INDEX idx_cards_account ON cards(account_id);

        INSERT INTO accounts VALUES
            ('acc_legacy0001', 'usr_legacy', 'checking', '2026-01-01T00:00:00+00:00');
        INSERT INTO cards VALUES ('4111222233334444', 'acc_legacy0001', 'active');
    """)
    conn.commit()
    conn.close()


def test_an_existing_database_upgrades_without_losing_anything(tmp_path):
    """init_schema and ensure_system_accounts must both be safe to run
    against a database that predates them."""
    from app.repository import LedgerRepository

    path = tmp_path / "legacy.db"
    _pre_msisdn_database(str(path))

    repo = LedgerRepository(Database(str(path)))
    repo.init_schema()
    repo.ensure_system_accounts()

    # The legacy customer survived, still resolves by card, and has no number.
    assert repo.resolve_account("4111222233334444") == "acc_legacy0001"
    legacy = [a for a in repo.list_accounts() if a["account_id"] == "acc_legacy0001"]
    assert legacy and legacy[0]["msisdn"] is None

    # And the funding account was created alongside it.
    assert repo.balance(repo.FUNDING_ACCOUNT) == 0


def test_the_upgraded_database_can_take_a_topup_and_a_purchase(tmp_path):
    """The upgrade is only real if money can move afterwards. This is the
    path a deployed pod takes on its first request after a release."""
    from app.repository import InsufficientFunds, LedgerRepository

    path = tmp_path / "legacy.db"
    _pre_msisdn_database(str(path))

    repo = LedgerRepository(Database(str(path)))
    repo.init_schema()
    repo.ensure_system_accounts()

    merchant = repo.create_account("usr_m", "merchant:demo")

    with pytest.raises(InsufficientFunds):
        repo.record_posting("rrnlegacy001", "acc_legacy0001",
                            merchant["account_id"], 100)

    repo.topup("acc_legacy0001", 5_000, "rrnlegacy002")
    repo.record_posting("rrnlegacy003", "acc_legacy0001",
                        merchant["account_id"], 2_000)

    assert repo.balance("acc_legacy0001") == 3_000
    assert repo.balance(repo.FUNDING_ACCOUNT) == -5_000
    assert repo.is_balanced()


def test_startup_is_idempotent_across_restarts(tmp_path):
    """A pod restarts. init_schema and ensure_system_accounts run again, on a
    database that now has everything. Running them twice must be a no-op, or
    every restart after the first would crash the container."""
    from app.repository import LedgerRepository

    path = tmp_path / "restart.db"
    _pre_msisdn_database(str(path))

    for _ in range(3):
        repo = LedgerRepository(Database(str(path)))
        repo.init_schema()
        repo.ensure_system_accounts()

    funding = [a for a in repo.list_accounts()
               if a["account_id"] == repo.FUNDING_ACCOUNT]
    assert len(funding) == 1, "startup created the funding account more than once"


# ---------------------------------------------------------------------------
# The Postgres branch of the migration
#
# The crash that took ledger-service down was Postgres-only, and the tests
# proving the fix were SQLite-only. _migrate_add_msisdn takes a completely
# different path per dialect, so passing on SQLite says nothing about the
# path that actually runs in the cluster. A fake cursor exercises it without
# needing a server.
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records SQL and answers information_schema with a fixed column set."""

    def __init__(self, existing_columns):
        self.existing = set(existing_columns)
        self.executed = []
        self._result = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))
        if "information_schema.columns" in sql:
            self._result = [(c,) for c in sorted(self.existing)]
        elif sql.strip().upper().startswith("ALTER TABLE ACCOUNTS ADD COLUMN MSISDN"):
            self.existing.add("msisdn")
            self._result = []
        else:
            self._result = []

    def fetchall(self):
        return self._result


class _PostgresLike:
    """Just enough Database for the migration to take the Postgres branch."""
    is_postgres = True
    dialect = "postgresql"


def _postgres_repo():
    from app.repository import LedgerRepository

    repo = LedgerRepository.__new__(LedgerRepository)
    repo.db = _PostgresLike()
    return repo


def test_postgres_migration_adds_the_column_when_it_is_absent():
    """The upgrade case: a database created before phone numbers existed."""
    repo = _postgres_repo()
    cur = FakeCursor({"account_id", "user_id", "type", "created_at"})

    repo._migrate_add_msisdn(cur)

    assert any("ALTER TABLE accounts ADD COLUMN msisdn" in s for s in cur.executed), \
        cur.executed
    assert "msisdn" in cur.existing


def test_postgres_migration_is_a_no_op_when_the_column_exists():
    """The restart case. Running it again must not attempt a second ALTER,
    which Postgres would reject and which would crashloop every pod after the
    first successful start."""
    repo = _postgres_repo()
    cur = FakeCursor({"account_id", "user_id", "msisdn", "type", "created_at"})

    repo._migrate_add_msisdn(cur)

    assert not any("ALTER TABLE" in s for s in cur.executed), cur.executed


def test_postgres_column_lookup_is_scoped_to_the_current_schema():
    """Without the scope, the answer to "does this column exist" can come
    from a table in another schema that this connection never writes to."""
    repo = _postgres_repo()
    cur = FakeCursor({"account_id"})

    repo._migrate_add_msisdn(cur)

    lookup = next(s for s in cur.executed if "information_schema.columns" in s)
    assert "table_schema = current_schema()" in lookup, lookup


def test_a_migration_that_silently_fails_refuses_to_start():
    """
    The post-condition. If detection is ever wrong, the next statement in
    init_schema is an index on the column, and the pod dies with
    UndefinedColumn pointing at a CREATE INDEX line that says nothing about
    the migration. This turns that into a sentence naming the real problem.
    """
    repo = _postgres_repo()

    class BrokenCursor(FakeCursor):
        def execute(self, sql, params=None):        # ALTER silently does nothing
            self.executed.append(" ".join(sql.split()))
            self._result = ([(c,) for c in sorted(self.existing)]
                            if "information_schema.columns" in sql else [])

    with pytest.raises(RuntimeError, match="msisdn migration did not apply"):
        repo._migrate_add_msisdn(BrokenCursor({"account_id"}))


# ---------------------------------------------------------------------------
# Purge: emptying the platform, as opposed to zeroing it
#
# reset() deletes postings and keeps accounts, which is the right default and
# is not what "start fresh" means. purge() also deletes cards and accounts,
# and is half of a two-database operation, auth-service owning the other.
# ---------------------------------------------------------------------------

def test_purge_removes_accounts_and_cards_not_just_postings(repo, accounts):
    alice, merchant = accounts
    repo.record_posting("rrnpurge0001", alice["account_id"], merchant["account_id"], 500)

    removed = repo.purge()

    assert removed["accounts"] >= 2 and removed["cards"] >= 1
    assert removed["transactions"] >= 1 and removed["ledger_entries"] >= 2
    assert repo.list_accounts() == [] or [
        a for a in repo.list_accounts() if a["type"] != "system"
    ] == []
    assert repo.export_since(None) == []
    assert repo.resolve_account("4111111111111111") is None


def test_purge_leaves_the_platform_immediately_usable(repo, accounts):
    """
    The funding account is recreated, so a top-up works straight away. Without
    that, the first action after a wipe fails on a foreign key and the fix is
    a pod restart, which is a miserable thing to discover.
    """
    repo.purge()

    assert repo.balance(repo.FUNDING_ACCOUNT) == 0
    bob = repo.create_account("usr_fresh", "4999888877776666")
    repo.topup(bob["account_id"], 1_000, "rrnafterpurge")
    assert repo.balance(bob["account_id"]) == 1_000
    assert repo.is_balanced()


def test_purge_deletes_children_before_parents(repo, accounts):
    """
    ledger_entries references both transactions and accounts, and cards
    references accounts. Deleting accounts first would violate those
    constraints on Postgres, and on SQLite too since the dialect sets
    PRAGMA foreign_keys = ON. Reaching a clean state proves the order held.
    """
    alice, merchant = accounts
    repo.record_posting("rrnfk00000001", alice["account_id"], merchant["account_id"], 100)

    repo.purge()   # would raise on a constraint violation

    assert repo.is_balanced()


def test_purge_is_idempotent(repo, accounts):
    """Running it twice is the documented fix for a partial wipe, so the
    second run must not fail on an already-empty database."""
    repo.purge()
    second = repo.purge()
    assert second["accounts"] == 1, "only the recreated funding account remains"


def test_purge_is_refused_when_reset_is(client):
    """One gate for both. Anything that can empty the ledger is as dangerous
    as anything else that can, and a second flag is one more thing to get
    wrong in production config."""
    assert client.post("/internal/ledger/purge").status_code == 403


# ---------------------------------------------------------------------------
# An RRN collision is not a replay
#
# The reference is ten digits of epoch seconds plus two random, so two
# transactions in the same second collide once in a hundred. The ledger used
# to answer "already_recorded" for both cases, and transaction-service passed
# that through as approved, so a customer could be told "Wallet topped up"
# while nothing moved. Caught as an intermittent scenario failure: a victim's
# balance read 0.00 after a top-up that returned approved.
# ---------------------------------------------------------------------------

def test_a_genuine_retry_is_still_a_replay(repo, accounts):
    """The idempotency guarantee, unchanged. Identical posting, same RRN."""
    alice, merchant = accounts

    first = repo.record_posting("rrnsame00001", alice["account_id"],
                                merchant["account_id"], 2_500)
    second = repo.record_posting("rrnsame00001", alice["account_id"],
                                 merchant["account_id"], 2_500)

    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"


def test_a_different_amount_on_the_same_rrn_is_a_collision(repo, accounts):
    from app.repository import RrnCollision

    alice, merchant = accounts
    repo.record_posting("rrnclash0001", alice["account_id"],
                        merchant["account_id"], 2_500)

    with pytest.raises(RrnCollision):
        repo.record_posting("rrnclash0001", alice["account_id"],
                            merchant["account_id"], 9_900)


def test_different_accounts_on_the_same_rrn_is_a_collision(repo, accounts):
    """The case that actually bit: two unrelated top-ups in the same second."""
    from app.repository import RrnCollision

    alice, merchant = accounts
    bob = repo.create_account("usr_bob", "4222222222222222")
    repo.topup(alice["account_id"], 5_000, "rrnclash0002")

    with pytest.raises(RrnCollision):
        repo.topup(bob["account_id"], 5_000, "rrnclash0002")


def test_a_collision_moves_no_money(repo, accounts):
    """The refusal has to be total. A partial write would be worse than the
    bug it replaces."""
    from app.repository import RrnCollision

    alice, merchant = accounts
    bob = repo.create_account("usr_bob", "4222222222222222")
    repo.topup(alice["account_id"], 5_000, "rrnclash0003")
    before = repo.balance(bob["account_id"])

    with pytest.raises(RrnCollision):
        repo.topup(bob["account_id"], 5_000, "rrnclash0003")

    assert repo.balance(bob["account_id"]) == before
    assert repo.is_balanced()


def test_the_collision_names_both_sides(repo, accounts):
    """An operator reading this needs to know which transaction owns the
    reference, not just that something clashed."""
    from app.repository import RrnCollision

    alice, merchant = accounts
    repo.record_posting("rrnclash0004", alice["account_id"],
                        merchant["account_id"], 2_500)

    with pytest.raises(RrnCollision) as caught:
        repo.record_posting("rrnclash0004", alice["account_id"],
                            merchant["account_id"], 7_700)

    assert caught.value.existing["amount_cents"] == 2_500
    assert caught.value.attempted["amount_cents"] == 7_700


def test_the_http_surface_separates_a_collision_from_an_overdraft(client):
    """
    Both are 409 and the caller treats them oppositely: retrying a collision
    with a new reference is correct, retrying an overdraft is pointless. The
    error code is what distinguishes them.
    """
    client.post("/internal/ledger/accounts", json={"user_id": "u1", "card_number": "4111111111111111"})
    account = client.post("/internal/ledger/resolve", json={"identifier": "4111111111111111"}).json()
    client.post("/internal/ledger/topup", json={
        "rrn": "rrnhttpclash", "account_id": account["account_id"], "amount_cents": 5_000})

    clash = client.post("/internal/ledger/topup", json={
        "rrn": "rrnhttpclash", "account_id": account["account_id"], "amount_cents": 9_999})

    assert clash.status_code == 409
    assert clash.json()["detail"]["error"] == "rrn_collision"
