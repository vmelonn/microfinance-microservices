# Learning SQL on this platform's data

A practice database on the same domain you already understand: wallets,
top-ups, purchases, agents, merchants, disputes. Thirteen tables, a year of
history, about 9,000 transactions.

Nothing here touches the live ledger.

```bash
python scripts/practice_db.py            # writes practice.db
```

Then in **DBeaver**: *Database → New Connection → SQLite →* pick `practice.db`.
No server, no cluster, works offline.

Against the real engine instead, when the sandbox is up:

```bash
oc port-forward svc/postgres 5432:5432
python scripts/practice_db.py --postgres "postgresql://microfinance:PASSWORD@127.0.0.1:5432/practice"
```

DBeaver → PostgreSQL → `localhost:5432`, database `practice`. The data comes
from a fixed seed, so both hold identical rows: an answer worked out on one
is correct on the other.

---

## The schema

```
branches ──┐ (parent_branch_id -> branches, a hierarchy)
           ├── agents
           └── users ── accounts ── cards
                          │
                          ├── merchants ── merchant_categories
                          └── ledger_entries ── transactions ── disputes
                                                                   │
                                                          dispute_tags ── tags
```

| table | rows | why it is here |
|---|---|---|
| `transactions` | ~9,000 | one row per attempt, `approved` / `declined` / `reversed` |
| `ledger_entries` | ~14,000 | two per **approved** transaction, one debit one credit |
| `accounts` | 425 | wallets, merchant accounts, and `acc_system_funding` |
| `users` | 400 | with a `tier_id` and a home branch |
| `branches` | 15 | head office → regional hubs → local branches |
| `agents` | 26 | do cash-in, belong to a branch, some inactive |
| `merchants` | 24 | have a category, two never trade |
| `disputes` | ~120 | `resolved_at` is NULL while open |
| `dispute_tags` | ~230 | many-to-many with `tags` |

**Three things that will catch you out**, on purpose:

1. `ledger_entries` only exists for **approved** transactions. Aggregating
   `transactions` without filtering on status counts money that never moved.
2. Amounts are **integer cents**. Divide by 100 for display, never store the
   division.
3. 32 wallets and 2 merchants have no activity at all. `INNER JOIN` silently
   drops them; `LEFT JOIN` keeps them. That difference is the whole reason
   both exist.

---

# Tier 1. Reading one table

**1.1** Every merchant category, alphabetically.

**1.2** The 10 largest approved transactions, biggest first, showing the
amount in whole currency units rather than cents.

**1.3** How many transactions were declined?

**1.4** Every wallet account opened in January 2026.

**1.5** Users whose name contains "Khan".

<details><summary>Answers</summary>

```sql
-- 1.1
SELECT name, mcc FROM merchant_categories ORDER BY name;

-- 1.2   integer division truncates, so cast or use a decimal divisor
SELECT rrn, kind, amount_cents / 100.0 AS amount
FROM transactions
WHERE status = 'approved'
ORDER BY amount_cents DESC
LIMIT 10;

-- 1.3
SELECT count(*) FROM transactions WHERE status = 'declined';

-- 1.4   >= and < beats BETWEEN on timestamps: BETWEEN would miss
--       anything after 2026-01-31 00:00:00 on the last day
SELECT account_id, opened_at
FROM accounts
WHERE type = 'wallet'
  AND opened_at >= '2026-01-01' AND opened_at < '2026-02-01';

-- 1.5
SELECT user_id, full_name, msisdn FROM users WHERE full_name LIKE '%Khan%';
```
</details>

---

# Tier 2. Grouping and aggregates

**2.1** Count of transactions by `status`.

**2.2** For each `kind`, the count and total approved value.

**2.3** Which channel is used most, and what is its average approved
transaction size?

**2.4** Categories with more than 400 approved purchases. (`HAVING`, not
`WHERE`.)

**2.5** The single busiest day of the year by transaction count.

<details><summary>Answers</summary>

```sql
-- 2.1
SELECT status, count(*) AS txns
FROM transactions GROUP BY status ORDER BY txns DESC;

-- 2.2   the filter goes in WHERE, before grouping
SELECT kind, count(*) AS txns, sum(amount_cents) / 100.0 AS total
FROM transactions
WHERE status = 'approved'
GROUP BY kind
ORDER BY total DESC;

-- 2.3
SELECT channel, count(*) AS txns, round(avg(amount_cents) / 100.0, 2) AS avg_amount
FROM transactions
WHERE status = 'approved'
GROUP BY channel
ORDER BY txns DESC;

-- 2.4   HAVING filters GROUPS, WHERE filters ROWS. Both here, doing
--       different jobs.
SELECT c.name, count(*) AS purchases
FROM transactions t
JOIN merchants m  ON m.merchant_id = t.merchant_id
JOIN merchant_categories c ON c.category_id = m.category_id
WHERE t.status = 'approved' AND t.kind = 'purchase'
GROUP BY c.name
HAVING count(*) > 400
ORDER BY purchases DESC;

-- 2.5   SQLite: date(). Postgres: created_at::date  or  date_trunc('day', ...)
SELECT date(created_at) AS day, count(*) AS txns
FROM transactions
GROUP BY day ORDER BY txns DESC LIMIT 1;
```
</details>

---

# Tier 3. Joins

**3.1** Every merchant with its category name and city.

**3.2** Total approved spend per merchant, highest first.

**3.3** **Every** merchant and its sales count, *including the two that have
never traded.* They must show `0`, not vanish.

**3.4** Each user with their KYC tier name and home branch city.

**3.5** Each branch with its parent branch's name. (A table joined to
itself.)

<details><summary>Answers</summary>

```sql
-- 3.1
SELECT m.name, c.name AS category, m.city
FROM merchants m
JOIN merchant_categories c ON c.category_id = m.category_id
ORDER BY c.name, m.name;

-- 3.2
SELECT m.name, count(*) AS sales, sum(t.amount_cents) / 100.0 AS revenue
FROM merchants m
JOIN transactions t ON t.merchant_id = m.merchant_id
WHERE t.status = 'approved'
GROUP BY m.name
ORDER BY revenue DESC;

-- 3.3   TWO traps in one query.
--
--   LEFT JOIN keeps merchants with no rows on the right.
--   The status filter must move into the ON clause: in WHERE it is applied
--   AFTER the join, the non-matching rows are all NULL, NULL = 'approved' is
--   not true, and the LEFT JOIN quietly becomes an INNER one.
--
--   count(t.rrn) not count(*): count(*) counts the NULL row as 1.
SELECT m.name,
       count(t.rrn) AS sales,
       COALESCE(sum(t.amount_cents), 0) / 100.0 AS revenue
FROM merchants m
LEFT JOIN transactions t
       ON t.merchant_id = m.merchant_id AND t.status = 'approved'
GROUP BY m.name
ORDER BY sales ASC;

-- 3.4
SELECT u.full_name, k.name AS tier, b.city
FROM users u
JOIN kyc_tiers k ON k.tier_id = u.tier_id
LEFT JOIN branches b ON b.branch_id = u.branch_id
ORDER BY u.full_name;

-- 3.5   the same table twice, with aliases. Head office has no parent, so
--       LEFT JOIN keeps it.
SELECT b.name AS branch, p.name AS reports_to
FROM branches b
LEFT JOIN branches p ON p.branch_id = b.parent_branch_id
ORDER BY p.name NULLS FIRST, b.name;   -- SQLite: just ORDER BY p.name, b.name
```
</details>

---

# Tier 4. Subqueries, EXISTS, NULL

**4.1** Users who have never had a dispute raised against their name.

**4.2** Wallets whose balance is above the average wallet balance.

**4.3** Disputes still open, with how many days they have been open.

**4.4** Transactions above the average amount **for their own kind**.

<details><summary>Answers</summary>

```sql
-- 4.1   NOT EXISTS, not NOT IN.
--       If the subquery ever returns a NULL, NOT IN returns no rows at all
--       and gives you a confidently empty answer.
SELECT u.user_id, u.full_name
FROM users u
WHERE NOT EXISTS (SELECT 1 FROM disputes d WHERE d.raised_by_user_id = u.user_id);

-- 4.2
WITH balances AS (
    SELECT a.account_id,
           sum(CASE WHEN e.entry_type = 'credit' THEN e.amount_cents
                    ELSE -e.amount_cents END) AS balance_cents
    FROM accounts a
    JOIN ledger_entries e ON e.account_id = a.account_id
    WHERE a.type = 'wallet'
    GROUP BY a.account_id
)
SELECT account_id, balance_cents / 100.0 AS balance
FROM balances
WHERE balance_cents > (SELECT avg(balance_cents) FROM balances)
ORDER BY balance_cents DESC;

-- 4.3   resolved_at IS NULL. Never "= NULL": that is neither true nor
--       false, it is unknown, and the row is dropped.
SELECT dispute_id, reason,
       CAST(julianday('now') - julianday(opened_at) AS INT) AS days_open
FROM disputes
WHERE resolved_at IS NULL
ORDER BY days_open DESC;
-- Postgres:  (now() - opened_at) AS open_for

-- 4.4   a correlated subquery: it runs per row, referencing the outer one
SELECT t.rrn, t.kind, t.amount_cents / 100.0 AS amount
FROM transactions t
WHERE t.status = 'approved'
  AND t.amount_cents > (SELECT avg(amount_cents) FROM transactions t2
                        WHERE t2.kind = t.kind AND t2.status = 'approved')
ORDER BY t.kind, amount DESC;
```
</details>

---

# Tier 5. CTEs and window functions

Where SQL stops being a filter and starts being an analysis language.

**5.1** Monthly approved volume, with each month's change from the previous
one.

**5.2** The top 3 merchants **per category** by revenue.

**5.3** A running total of platform volume through the year.

**5.4** Rank users by total spend, showing their percentile.

**5.5** For each account, the gap in days between consecutive transactions.

<details><summary>Answers</summary>

```sql
-- 5.1   LAG reaches back one row in the ordered window
WITH monthly AS (
    SELECT strftime('%Y-%m', created_at) AS month,      -- PG: to_char(created_at,'YYYY-MM')
           sum(amount_cents) AS total_cents
    FROM transactions WHERE status = 'approved'
    GROUP BY month
)
SELECT month,
       total_cents / 100.0 AS total,
       (total_cents - LAG(total_cents) OVER (ORDER BY month)) / 100.0 AS change
FROM monthly
ORDER BY month;

-- 5.2   rank WITHIN each category, then filter. The window function cannot
--       go in WHERE, because WHERE runs before the window is computed, so it
--       needs the CTE.
WITH revenue AS (
    SELECT c.name AS category, m.name AS merchant,
           sum(t.amount_cents) AS total_cents,
           ROW_NUMBER() OVER (PARTITION BY c.name
                              ORDER BY sum(t.amount_cents) DESC) AS rank_in_category
    FROM transactions t
    JOIN merchants m ON m.merchant_id = t.merchant_id
    JOIN merchant_categories c ON c.category_id = m.category_id
    WHERE t.status = 'approved'
    GROUP BY c.name, m.name
)
SELECT category, merchant, total_cents / 100.0 AS revenue
FROM revenue WHERE rank_in_category <= 3
ORDER BY category, rank_in_category;

-- 5.3   a window with a frame: everything from the start up to this row
SELECT date(created_at) AS day,
       sum(amount_cents) / 100.0 AS daily,
       sum(sum(amount_cents)) OVER (ORDER BY date(created_at)) / 100.0 AS running
FROM transactions WHERE status = 'approved'
GROUP BY day ORDER BY day;

-- 5.4   RANK leaves gaps after ties, DENSE_RANK does not, ROW_NUMBER never
--       ties. Picking the wrong one is a classic silent bug.
WITH spend AS (
    SELECT a.user_id, sum(e.amount_cents) AS spent
    FROM ledger_entries e
    JOIN accounts a ON a.account_id = e.account_id
    WHERE e.entry_type = 'debit' AND a.type = 'wallet'
    GROUP BY a.user_id
)
SELECT u.full_name, s.spent / 100.0 AS spent,
       RANK() OVER (ORDER BY s.spent DESC) AS rank,
       round(PERCENT_RANK() OVER (ORDER BY s.spent) * 100, 1) AS percentile
FROM spend s JOIN users u ON u.user_id = s.user_id
ORDER BY s.spent DESC LIMIT 20;

-- 5.5   LAG partitioned per account
WITH events AS (
    SELECT e.account_id, t.created_at,
           LAG(t.created_at) OVER (PARTITION BY e.account_id
                                   ORDER BY t.created_at) AS previous
    FROM ledger_entries e
    JOIN transactions t ON t.rrn = e.rrn
    WHERE e.entry_type = 'debit'
)
SELECT account_id, created_at, previous,
       CAST(julianday(created_at) - julianday(previous) AS INT) AS days_since
FROM events WHERE previous IS NOT NULL
ORDER BY days_since DESC LIMIT 20;
```
</details>

---

# Tier 6. The hard ones

**6.1** The full branch hierarchy with each branch's depth, using a
**recursive CTE**.

**6.2** Every tag with how many disputes carry it, and how many of those were
resolved. (Many-to-many.)

**6.3** A cohort table: for users grouped by their signup month, how many
were still transacting three months later?

**6.4** Reconcile the ledger. Prove total debits equal total credits, and
find any transaction whose entries do not balance.

**6.5** Each account's balance **as at 31 March 2026**, not today.

<details><summary>Answers</summary>

```sql
-- 6.1   the anchor is the root, then it joins to itself until nothing new
--       comes back. Postgres needs RECURSIVE; SQLite accepts it too.
WITH RECURSIVE tree AS (
    SELECT branch_id, name, parent_branch_id, 0 AS depth
    FROM branches WHERE parent_branch_id IS NULL
    UNION ALL
    SELECT b.branch_id, b.name, b.parent_branch_id, tree.depth + 1
    FROM branches b JOIN tree ON b.parent_branch_id = tree.branch_id
)
SELECT depth, name FROM tree ORDER BY depth, name;

-- 6.2   the join table is what makes it many-to-many
SELECT g.name AS tag,
       count(*) AS disputes,
       sum(CASE WHEN d.status = 'resolved' THEN 1 ELSE 0 END) AS resolved
FROM tags g
JOIN dispute_tags dt ON dt.tag_id = g.tag_id
JOIN disputes d      ON d.dispute_id = dt.dispute_id
GROUP BY g.name
ORDER BY disputes DESC;

-- 6.3   cohort analysis. Two derived tables and a date comparison.
WITH cohort AS (
    SELECT user_id, strftime('%Y-%m', created_at) AS signup_month FROM users
),
activity AS (
    SELECT DISTINCT a.user_id, strftime('%Y-%m', t.created_at) AS active_month
    FROM transactions t
    JOIN ledger_entries e ON e.rrn = t.rrn
    JOIN accounts a ON a.account_id = e.account_id
    WHERE t.status = 'approved' AND a.user_id IS NOT NULL
)
SELECT c.signup_month,
       count(DISTINCT c.user_id) AS signed_up,
       count(DISTINCT CASE WHEN act.active_month IS NOT NULL
                           THEN c.user_id END) AS still_active_m3
FROM cohort c
LEFT JOIN activity act
       ON act.user_id = c.user_id
      AND act.active_month = strftime('%Y-%m', date(c.signup_month || '-01', '+3 months'))
GROUP BY c.signup_month
ORDER BY c.signup_month;

-- 6.4   two answers. The total, and the per-transaction check that finds
--       WHICH one is wrong when the total is not zero.
SELECT sum(CASE WHEN entry_type = 'debit' THEN -amount_cents
                ELSE amount_cents END) AS should_be_zero
FROM ledger_entries;

SELECT rrn,
       sum(CASE WHEN entry_type = 'debit' THEN amount_cents ELSE 0 END) AS debits,
       sum(CASE WHEN entry_type = 'credit' THEN amount_cents ELSE 0 END) AS credits
FROM ledger_entries
GROUP BY rrn
HAVING debits <> credits;      -- expect zero rows

-- 6.5   a balance is not stored, it is the sum of everything up to a
--       moment. Change the date and you have time travel.
SELECT a.account_id, a.type,
       COALESCE(sum(CASE WHEN e.entry_type = 'credit' THEN e.amount_cents
                         ELSE -e.amount_cents END), 0) / 100.0 AS balance
FROM accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.account_id
LEFT JOIN transactions t   ON t.rrn = e.rrn AND t.created_at < '2026-04-01'
WHERE a.type = 'wallet'
GROUP BY a.account_id, a.type
ORDER BY balance DESC LIMIT 20;
```
</details>

---

## Habits worth forming

- **Filter before you aggregate.** `WHERE status = 'approved'` belongs in
  almost every query against `transactions`. Forgetting it is the single most
  common wrong answer here.
- **`LEFT JOIN` conditions go in `ON`, not `WHERE`.** In `WHERE` they run
  after the join and turn it back into an inner join. 3.3 is that trap.
- **`IS NULL`, never `= NULL`.** Comparison with NULL is unknown, and unknown
  rows are dropped.
- **`NOT EXISTS` over `NOT IN`** when the subquery could contain a NULL.
- **Read the plan when something is slow.** `EXPLAIN QUERY PLAN <query>` in
  SQLite, `EXPLAIN ANALYZE <query>` in Postgres. Watch for a sequential scan
  where you expected an index.

## Where the dialects differ

You will hit these moving between the SQLite file and cluster Postgres:

| | SQLite | Postgres |
|---|---|---|
| current time | `datetime('now')` | `now()` |
| date part | `strftime('%Y-%m', c)` | `to_char(c, 'YYYY-MM')` |
| truncate to day | `date(c)` | `date_trunc('day', c)` or `c::date` |
| date difference | `julianday(a) - julianday(b)` | `a - b` (an interval) |
| concatenate | `a || b` | `a || b` |
| cast | `CAST(x AS INT)` | `x::int` |
| NULL ordering | NULLs first by default | NULLs last by default |

The last row bites quietly: the same `ORDER BY` puts NULLs at opposite ends
in the two engines. Say what you mean with `NULLS FIRST` or `NULLS LAST` in
Postgres.
