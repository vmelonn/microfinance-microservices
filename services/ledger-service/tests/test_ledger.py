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


@pytest.fixture
def repo(tmp_path):
    from app.repository import LedgerRepository

    repository = LedgerRepository(Database(str(tmp_path / "ledger.db")))
    repository.init_schema()
    return repository


@pytest.fixture
def accounts(repo):
    alice = repo.create_account("usr_alice", "4111111111111111")
    merchant = repo.create_account("usr_merchant", "merchant:demo")
    return alice, merchant


def test_posting_creates_a_balanced_pair(repo, accounts):
    alice, merchant = accounts

    result = repo.record_posting("rrn000000001", alice["account_id"], merchant["account_id"], 5000)

    assert result["status"] == "recorded"
    assert repo.balance(alice["account_id"]) == -5000
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
    assert repo.balance(alice["account_id"]) == -5000


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
    assert repo.balance(alice["account_id"]) == -2500
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
    assert repo.balance(alice["account_id"]) == -sum(100 + i for i in range(50))


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

    rows = repo.export_since(None)
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

    row = repo.export_since(None)[0]
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

    payload = {
        "rrn": "rrnhttp00001",
        "debit_account": debit["account_id"],
        "credit_account": credit["account_id"],
        "amount_cents": 4200,
    }

    assert client.post("/internal/ledger/postings", json=payload).json()["status"] == "recorded"
    assert client.post("/internal/ledger/postings", json=payload).json()["status"] == "already_recorded"

    balance = client.get(f"/internal/ledger/accounts/{debit['account_id']}/balance").json()
    assert balance["balance_cents"] == -4200


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
