# Depot Ledger (MVP)

Ledger-based stock control, custody tracking, count reconciliation and anomaly detection for a small
depot (store, shop, vans, employee custody), with CEO/board dashboards.
Python 3.9+ standard library only. No installs.

## Run
```
python3 run.py                  # demo data, http://127.0.0.1:8000, database file depot.db
python3 run.py --empty          # clean database
python3 -W ignore -m unittest discover -s tests -v    # 20 tests incl. the 10 acceptance scenarios
```
Demo sign-in: employee number = PIN (last 4 digits): 1001 storekeeper, 1002 cashier, 1003 driver,
1004/1005 managers, 1006 accountant, 1007 auditor, 1008 CEO, 1009 board, 1010 admin.

## What is in the MVP
| Area | Behaviour |
|---|---|
| Ledger | Stock is summed from immutable `stock_postings`; no editable stock field. Posted events can't be edited or deleted (DB triggers, tested with raw SQL). Corrections are linked REVERSAL events. |
| Atomic and idempotent | A posting succeeds entirely or not at all. Replaying the same event id or reference posts once. Negative stock is refused. |
| Separation of duties | Adjustments, reversals and any event above KSh 250,000 need a second person. Requester and stock custodian can't approve; blocked attempts are audited. Recounts must be by a different person than the counter; the original count is preserved. |
| Receiving | Ledger records what was physically received; supplier-note quantity is evidence and shortages open a case, even when the receipt waits for approval. |
| Reconciliation rules | Unreconciled deliveries (dispatch vs delivered + returned + damaged, by trip), count variances, unusual adjustments, sales-to-cash (late payments are TIMING, not mismatch), repeated patterns relative to activity. Idempotent: re-running never duplicates a case. |
| Investigations | Cases hold evidence, assignment, escalation, written resolution. The system never declares theft. |
| Bank import | Duplicate rows detected by fingerprint and skipped. |
| Dashboards | CEO/auditor/manager: KPIs, value by location, ranked cases. Board: aggregates only. |
| Web app | Responsive, role-aware. Offline queue stores failed submissions on the device with a fixed event id and syncs automatically; shown as pending, never as posted. |

## Layout
`depot/db.py` schema + triggers · `service.py` ledger rules · `rules.py` anomaly rules · `dashboard.py` ·
`server.py` JSON API + static hosting · `seed.py` demo data · `static/index.html` web app ·
`sql/schema_postgres.sql` production schema · `tests/`.

## Known limits (be aware before relying on it)
* SQLite is used so it runs anywhere; the PostgreSQL schema is provided but **not yet run against a live Postgres server**. Moving the service layer to Postgres (psycopg, `SELECT ... FOR UPDATE` or serializable transactions) is the first production task.
* Auth is employee number + PIN with server-side sessions. Before real use add: TLS, MFA for privileged roles, session expiry, rate limiting, PIN reset, and a hardened admin workflow. No customer/supplier masters, vehicles, UoM conversions or batch/expiry screens yet (tables exist).
* A cancelled sale is audited, but its stock reversal is a separate, approved step (Move stock → Reverse).
* The offline queue covers stock movements only, and a queued transaction is validated when it syncs, so it can still be rejected.
* Historical 2023 spreadsheets are **not** imported: formulas, external links, duplicate bank rows and opening balances must be validated first (plan section 7). Run parallel with the spreadsheets until daily balances agree within an approved tolerance.
* Thresholds (approval KSh 250k, severity 20k/100k, adjustment 50k) are placeholders to be set by management.
