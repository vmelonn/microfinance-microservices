"""
Build a practice database for learning SQL, on this platform's own domain.

    python scripts/practice_db.py                 # -> practice.db (SQLite)
    python scripts/practice_db.py --scale 3       # three times the volume
    python scripts/practice_db.py --postgres "postgresql://microfinance:PW@127.0.0.1:5432/practice"

Open the SQLite file directly in DBeaver, or port-forward the cluster Postgres
and point DBeaver at localhost. The data comes from a fixed seed, so both
targets hold exactly the same rows and an answer worked out against one is
correct against the other.

WHY A SEPARATE DATABASE. The live ledger is small, recent and real. Learning
to query wants volume, a year of history, and shapes the production schema
does not have: a hierarchy, a many-to-many, nullable foreign keys. So this
mirrors the five real tables and adds the dimensions a microfinance platform
would genuinely carry around them. Nothing here touches the live ledger.

WHAT IS DELIBERATELY MESSY. Real data is not tidy, and a query that only works
on tidy data is not worth much:

  - some accounts have no transactions at all      (LEFT JOIN versus INNER)
  - many transactions are declined or reversed     (filter before you aggregate)
  - disputes.resolved_at is NULL while open        (NULL handling, COALESCE)
  - agent_id and merchant_id are both nullable and mutually exclusive
  - blocked cards, inactive agents, a merchant with no sales
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SEED = 20260814

TABLES = [
    "dispute_tags", "disputes", "tags", "ledger_entries", "transactions",
    "cards", "merchants", "merchant_categories", "accounts", "agents",
    "users", "kyc_tiers", "branches",
]

ORDER = [
    "branches", "kyc_tiers", "users", "agents", "merchant_categories",
    "accounts", "merchants", "cards", "transactions", "ledger_entries",
    "disputes", "tags", "dispute_tags",
]


def ddl(pg: bool) -> list[str]:
    """
    Written out per dialect rather than through an abstraction, because the
    differences are exactly what is worth seeing when you move between them.
    """
    pk = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMPTZ" if pg else "TIMESTAMP"
    return [
        f"""CREATE TABLE branches (
            branch_id        TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            city             TEXT NOT NULL,
            region           TEXT NOT NULL,
            -- Self-referencing. A branch reports to a regional hub, a hub
            -- reports to head office. This is what recursive CTEs are for.
            parent_branch_id TEXT REFERENCES branches(branch_id),
            opened_at        {ts} NOT NULL
        )""",
        """CREATE TABLE kyc_tiers (
            tier_id             INTEGER PRIMARY KEY,
            name                TEXT NOT NULL,
            daily_limit_cents   BIGINT NOT NULL,
            monthly_limit_cents BIGINT NOT NULL
        )""",
        f"""CREATE TABLE users (
            user_id    TEXT PRIMARY KEY,
            full_name  TEXT NOT NULL,
            msisdn     TEXT NOT NULL UNIQUE,
            tier_id    INTEGER NOT NULL REFERENCES kyc_tiers(tier_id),
            branch_id  TEXT REFERENCES branches(branch_id),
            created_at {ts} NOT NULL
        )""",
        f"""CREATE TABLE agents (
            agent_id  TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            branch_id TEXT NOT NULL REFERENCES branches(branch_id),
            joined_at {ts} NOT NULL,
            active    INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE merchant_categories (
            category_id INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            mcc         TEXT NOT NULL
        )""",
        f"""CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            user_id    TEXT REFERENCES users(user_id),
            msisdn     TEXT,
            type       TEXT NOT NULL DEFAULT 'wallet',
            opened_at  {ts} NOT NULL
        )""",
        """CREATE TABLE merchants (
            merchant_id TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            category_id INTEGER NOT NULL REFERENCES merchant_categories(category_id),
            account_id  TEXT NOT NULL REFERENCES accounts(account_id),
            city        TEXT NOT NULL
        )""",
        f"""CREATE TABLE cards (
            card_number TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES accounts(account_id),
            status      TEXT NOT NULL DEFAULT 'active',
            issued_at   {ts} NOT NULL
        )""",
        f"""CREATE TABLE transactions (
            rrn          TEXT PRIMARY KEY,
            amount_cents BIGINT NOT NULL,
            kind         TEXT NOT NULL,
            status       TEXT NOT NULL,
            channel      TEXT NOT NULL,
            -- Both nullable, and at most one is ever set. A cash-in comes
            -- through an agent, a purchase goes to a merchant, a transfer
            -- has neither.
            agent_id     TEXT REFERENCES agents(agent_id),
            merchant_id  TEXT REFERENCES merchants(merchant_id),
            created_at   {ts} NOT NULL
        )""",
        f"""CREATE TABLE ledger_entries (
            entry_id     {pk},
            rrn          TEXT NOT NULL REFERENCES transactions(rrn),
            account_id   TEXT NOT NULL REFERENCES accounts(account_id),
            entry_type   TEXT NOT NULL CHECK (entry_type IN ('debit','credit')),
            amount_cents BIGINT NOT NULL
        )""",
        f"""CREATE TABLE disputes (
            dispute_id        TEXT PRIMARY KEY,
            rrn               TEXT NOT NULL REFERENCES transactions(rrn),
            raised_by_user_id TEXT NOT NULL REFERENCES users(user_id),
            reason            TEXT NOT NULL,
            status            TEXT NOT NULL,
            opened_at         {ts} NOT NULL,
            -- NULL while the dispute is open. Half the point of this table.
            resolved_at       {ts}
        )""",
        """CREATE TABLE tags (
            tag_id INTEGER PRIMARY KEY,
            name   TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE dispute_tags (
            dispute_id TEXT NOT NULL REFERENCES disputes(dispute_id),
            tag_id     INTEGER NOT NULL REFERENCES tags(tag_id),
            PRIMARY KEY (dispute_id, tag_id)
        )""",
    ]


# Tables whose row tuples do not cover every column.
COLUMNS = {"ledger_entries": " (rrn, account_id, entry_type, amount_cents)"}

INDEXES = [
    "CREATE INDEX idx_entries_account ON ledger_entries(account_id)",
    "CREATE INDEX idx_entries_rrn ON ledger_entries(rrn)",
    "CREATE INDEX idx_txn_created ON transactions(created_at)",
    "CREATE INDEX idx_txn_merchant ON transactions(merchant_id)",
    "CREATE INDEX idx_accounts_user ON accounts(user_id)",
]

REGIONS = {
    "Punjab": ["Lahore", "Faisalabad", "Multan", "Rawalpindi"],
    "Sindh": ["Karachi", "Hyderabad", "Sukkur"],
    "KPK": ["Peshawar", "Abbottabad"],
    "Balochistan": ["Quetta"],
}
CATEGORIES = [
    (1, "Grocery", "5411"), (2, "Fuel", "5541"), (3, "Pharmacy", "5912"),
    (4, "Mobile top-up", "4814"), (5, "Utilities", "4900"),
    (6, "Restaurant", "5812"), (7, "Clothing", "5651"), (8, "Electronics", "5732"),
]
TIERS = [
    (1, "Basic", 2_500_000, 20_000_000),
    (2, "Verified", 10_000_000, 100_000_000),
    (3, "Business", 100_000_000, 2_000_000_000),
]
TAGS = [
    (1, "fraud-suspected"), (2, "duplicate-charge"), (3, "agent-error"),
    (4, "merchant-unresponsive"), (5, "resolved-goodwill"), (6, "chargeback"),
]
FIRST = ["Ayesha", "Bilal", "Fatima", "Hassan", "Imran", "Sana", "Usman", "Zara",
         "Adeel", "Hina", "Kamran", "Nadia", "Omar", "Rabia", "Tariq", "Yasmin"]
LAST = ["Khan", "Ahmed", "Malik", "Sheikh", "Butt", "Qureshi", "Raza", "Iqbal",
        "Hussain", "Javed"]
REASONS = ["unauthorised transaction", "goods not received", "duplicate charge",
           "wrong amount", "agent did not hand over cash"]
PREFIX = ["Al", "New", "City", "Star", "Green"]
SUFFIX = ["Mart", "Store", "Traders", "Centre"]
HOURLY = [1, 1, 1, 1, 1, 2, 4, 6, 8, 9, 9, 8, 7, 7, 8, 9, 9, 8, 7, 5, 4, 3, 2, 1]


def build(rng: random.Random, scale: int) -> dict:
    start = datetime(2025, 8, 1, tzinfo=timezone.utc)
    days = 365

    branches = [("br_head", "Head Office", "Islamabad", "Federal", None, start)]
    for region, cities in REGIONS.items():
        hub = "br_" + region.lower() + "_hub"
        branches.append((hub, region + " Regional Hub", cities[0], region,
                         "br_head", start))
        for city in cities:
            branches.append(("br_" + city.lower(), city + " Branch", city, region,
                             hub, start + timedelta(days=rng.randint(0, 120))))

    local = [b[0] for b in branches if b[4] is not None and "hub" not in b[0]]

    agents = []
    for bid in local:
        for _ in range(rng.randint(1, 4)):
            agents.append(("agt_%04d" % len(agents),
                           rng.choice(FIRST) + " " + rng.choice(LAST), bid,
                           start + timedelta(days=rng.randint(0, 200)),
                           1 if rng.random() > 0.12 else 0))

    users, accounts, cards = [], [], []
    n_users = 400 * scale
    for i in range(n_users):
        uid = "usr_%06d" % i
        created = start + timedelta(days=rng.randint(0, days - 30),
                                    minutes=rng.randint(0, 1439))
        users.append((uid, rng.choice(FIRST) + " " + rng.choice(LAST),
                      "92" + str(rng.randint(3000000000, 3499999999)),
                      rng.choices([1, 2, 3], weights=[60, 35, 5])[0],
                      rng.choice(local), created))
        aid = "acc_%06d" % i
        accounts.append((aid, uid, users[-1][2], "wallet", created))
        cards.append(("4" + str(rng.randint(10 ** 14, 10 ** 15 - 1)), aid,
                      "active" if rng.random() > 0.07 else "blocked", created))

    wallets = list(accounts)
    accounts.append(("acc_system_funding", None, None, "system", start))

    merchants = []
    for i in range(24 * scale):
        aid = "acc_mer_%04d" % i
        accounts.append((aid, None, None, "merchant", start))
        merchants.append(("mer_%04d" % i,
                          "%s %s %d" % (rng.choice(PREFIX), rng.choice(SUFFIX), i),
                          rng.choice(CATEGORIES)[0], aid,
                          rng.choice(sum(REGIONS.values(), []))))

    # Transactions are generated in CHRONOLOGICAL order.
    #
    # The first version picked random dates and evaluated balances in
    # insertion order, so a January purchase could be funded by a December
    # top-up. The books still balanced, but the story did not: 52% of
    # purchases declined because the money had not "arrived" yet in the order
    # the loop happened to run. Sorting the timeline first makes the data
    # temporally coherent and drops declines to a believable rate.
    #
    # A slice of wallets never transacts at all. Newly registered customers
    # who have not used the product exist in every real system, and without
    # them LEFT JOIN and INNER JOIN return the same thing and the difference
    # cannot be taught.
    active = wallets[:int(len(wallets) * 0.92)]

    # Two merchants are onboarded and never trade. Same reason as the dormant
    # wallets: without a zero somewhere, nobody learns why LEFT JOIN exists.
    trading = merchants[:-2]

    schedule = sorted(
        start + timedelta(days=rng.randint(0, days - 1),
                          hours=rng.choices(range(24), weights=HOURLY)[0],
                          minutes=rng.randint(0, 59))
        for _ in range(9000 * scale))

    txns, entries, balances = [], [], {}
    agent_ids = [a[0] for a in agents]
    for i, when in enumerate(schedule):
        rrn = "%012d" % i
        acct = rng.choice(active)
        kind = rng.choices(["topup", "purchase", "transfer"], weights=[30, 55, 15])[0]
        held = balances.get(acct[0], 0)
        agent = merchant = None

        if kind == "topup":
            amount = rng.choice([500, 1000, 2000, 5000, 10000]) * 100
            debit, credit = "acc_system_funding", acct[0]
            agent = rng.choice(agent_ids)
        elif kind == "purchase":
            amount = rng.randint(50, 6000) * 100
            # Mostly people spend what they have. Occasionally they try not
            # to, which is where the declines come from.
            if amount > held and held > 5000 and rng.random() < 0.85:
                amount = rng.randint(1000, max(1001, held))
            m = rng.choice(trading)
            debit, credit, merchant = acct[0], m[3], m[0]
        else:
            amount = rng.randint(100, 3000) * 100
            if amount > held and held > 5000 and rng.random() < 0.85:
                amount = rng.randint(1000, max(1001, held))
            other = rng.choice(active)
            while other[0] == acct[0]:
                other = rng.choice(active)
            debit, credit = acct[0], other[0]

        # Only APPROVED transactions get ledger entries. That is the single
        # most important thing to notice before aggregating this table.
        if kind != "topup" and amount > held:
            status = "declined"
        else:
            status = rng.choices(["approved", "declined", "reversed"],
                                 weights=[95, 3, 2])[0]

        txns.append((rrn, amount, kind, status,
                     rng.choices(["app", "ussd", "agent", "web"],
                                 weights=[55, 20, 20, 5])[0],
                     agent, merchant, when))

        if status == "approved":
            entries.append((rrn, debit, "debit", amount))
            entries.append((rrn, credit, "credit", amount))
            balances[debit] = balances.get(debit, 0) - amount
            balances[credit] = balances.get(credit, 0) + amount

    payer_of = {}
    for rrn, account_id, entry_type, _amount in entries:
        if entry_type == "debit":
            payer_of[rrn] = account_id
    owner_of = {a[0]: a[1] for a in accounts}

    disputes, dispute_tags = [], []
    candidates = [t for t in txns if t[3] == "approved" and t[2] == "purchase"]
    for i, t in enumerate(rng.sample(candidates, min(len(candidates), 120 * scale))):
        uid = owner_of.get(payer_of.get(t[0]))
        if uid is None:
            continue
        opened = t[7] + timedelta(days=rng.randint(1, 20))
        status = rng.choices(["open", "resolved", "rejected"],
                             weights=[25, 55, 20])[0]
        did = "dsp_%05d" % i
        disputes.append((did, t[0], uid, rng.choice(REASONS), status, opened,
                         None if status == "open"
                         else opened + timedelta(days=rng.randint(1, 30))))
        for tag in rng.sample(TAGS, rng.randint(1, 3)):
            dispute_tags.append((did, tag[0]))

    return {
        "branches": branches, "kyc_tiers": TIERS, "users": users, "agents": agents,
        "merchant_categories": CATEGORIES, "accounts": accounts,
        "merchants": merchants, "cards": cards, "transactions": txns,
        "ledger_entries": entries, "disputes": disputes, "tags": TAGS,
        "dispute_tags": dispute_tags,
    }


def write(conn, data: dict, pg: bool) -> None:
    placeholder = "%s" if pg else "?"
    cur = conn.cursor()
    for table in TABLES:
        cur.execute("DROP TABLE IF EXISTS " + table + (" CASCADE" if pg else ""))
    for statement in ddl(pg):
        cur.execute(statement)

    for table in ORDER:
        rows = data[table]
        if not rows:
            continue
        marks = ",".join([placeholder] * len(rows[0]))
        # Named columns for ledger_entries, whose primary key is generated by
        # the database, so the row tuple is one short. Naming them also means
        # a future column added to any table does not silently shift every
        # value one place to the left.
        columns = COLUMNS.get(table, "")
        cur.executemany(
            "INSERT INTO %s%s VALUES (%s)" % (table, columns, marks),
            [tuple(v.isoformat() if isinstance(v, datetime) and not pg else v
                   for v in row) for row in rows])
        print("  %-22s %8d" % (table, len(rows)))

    for statement in INDEXES:
        cur.execute(statement)
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", default="practice.db")
    parser.add_argument("--postgres", help="DSN for a Postgres to load instead")
    parser.add_argument("--scale", type=int, default=1,
                        help="multiply every row count")
    args = parser.parse_args()

    print("generating (seed %d, scale %d)" % (SEED, args.scale))
    data = build(random.Random(SEED), args.scale)

    if args.postgres:
        import psycopg2

        print("\nwriting to Postgres")
        conn = psycopg2.connect(args.postgres)
        write(conn, data, pg=True)
        conn.close()
    else:
        print("\nwriting to " + args.sqlite)
        conn = sqlite3.connect(args.sqlite)
        conn.execute("PRAGMA foreign_keys = ON")
        write(conn, data, pg=False)
        conn.close()

    print("\ndone. Open it in DBeaver, then work through docs/sql-practice.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
