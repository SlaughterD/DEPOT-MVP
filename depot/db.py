"""Database layer: SQLite schema with ledger-protection triggers.

The same design is provided for PostgreSQL in sql/schema_postgres.sql.
SQLite is used here so the MVP runs anywhere with zero installs.
"""
import sqlite3
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Africa/Nairobi'
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
    employee_no TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    pin_hash TEXT, pin_salt TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, role_name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id), name TEXT NOT NULL,
    location_type TEXT NOT NULL CHECK (location_type IN
        ('STORE','SHOP','VAN','CUSTODY','IN_TRANSIT','DAMAGED','QUARANTINE')),
    custodian_id TEXT REFERENCES users(id),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL, base_uom TEXT NOT NULL,
    unit_cost_method TEXT NOT NULL DEFAULT 'WEIGHTED_AVERAGE',
    avg_cost NUMERIC NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS uom_conversions (
    product_id TEXT NOT NULL REFERENCES products(id), from_uom TEXT NOT NULL, to_uom TEXT NOT NULL,
    factor NUMERIC NOT NULL CHECK (factor > 0), PRIMARY KEY (product_id, from_uom, to_uom)
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id),
    batch_no TEXT NOT NULL, expiry_date TEXT, UNIQUE (product_id, batch_no)
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, document_type TEXT NOT NULL, reference TEXT NOT NULL, file_uri TEXT,
    event_id TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_events (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id),
    event_type TEXT NOT NULL CHECK (event_type IN
        ('RECEIVE','TRANSFER','SALE','RETURN','DAMAGE','ADJUST_IN','ADJUST_OUT','REVERSAL')),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN
        ('DRAFT','PENDING_APPROVAL','POSTED','REJECTED')),
    reference TEXT NOT NULL,
    trip_ref TEXT,
    reason TEXT,
    from_location_id TEXT REFERENCES locations(id),
    to_location_id TEXT REFERENCES locations(id),
    created_by TEXT NOT NULL REFERENCES users(id),
    custodian_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    posted_at TEXT,
    correction_of TEXT REFERENCES stock_events(id),
    UNIQUE (org_id, event_type, reference)
);
CREATE TABLE IF NOT EXISTS stock_event_lines (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES stock_events(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    quantity_base NUMERIC NOT NULL CHECK (quantity_base > 0),
    batch_id TEXT REFERENCES batches(id),
    unit_cost NUMERIC
);
CREATE TABLE IF NOT EXISTS stock_postings (
    id TEXT PRIMARY KEY,
    event_line_id TEXT NOT NULL REFERENCES stock_event_lines(id),
    location_id TEXT NOT NULL REFERENCES locations(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    quantity_delta NUMERIC NOT NULL CHECK (quantity_delta <> 0),
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_postings_loc_prod ON stock_postings(location_id, product_id);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES stock_events(id),
    approver_id TEXT NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
    reason TEXT, decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id),
    invoice_no TEXT NOT NULL UNIQUE, order_date TEXT NOT NULL,
    payment_terms TEXT NOT NULL DEFAULT 'CASH' CHECK (payment_terms IN ('CASH','CREDIT')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CANCELLED')),
    total NUMERIC NOT NULL CHECK (total >= 0), created_by TEXT REFERENCES users(id),
    cancelled_by TEXT, cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS order_lines (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    product_id TEXT NOT NULL REFERENCES products(id), quantity NUMERIC NOT NULL, unit_price NUMERIC NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    method TEXT NOT NULL, amount NUMERIC NOT NULL, reference TEXT, paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_transactions (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, account TEXT NOT NULL,
    txn_date TEXT NOT NULL, amount NUMERIC NOT NULL, reference TEXT, imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_counts (
    id TEXT PRIMARY KEY, location_id TEXT NOT NULL REFERENCES locations(id),
    cutoff_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','FINALIZED')),
    opened_by TEXT NOT NULL REFERENCES users(id), finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_count_lines (
    id TEXT PRIMARY KEY, count_id TEXT NOT NULL REFERENCES stock_counts(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    expected_qty NUMERIC NOT NULL,
    counted_qty NUMERIC, counted_by TEXT REFERENCES users(id),
    recount_qty NUMERIC, recounted_by TEXT REFERENCES users(id),
    UNIQUE (count_id, product_id)
);
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY, variance_type TEXT NOT NULL, severity TEXT NOT NULL
        CHECK (severity IN ('LOW','MEDIUM','HIGH')),
    amount NUMERIC NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','IN_REVIEW','ESCALATED','CLOSED')),
    assigned_to TEXT REFERENCES users(id), summary TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}', source_key TEXT UNIQUE,
    resolution TEXT, created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, entity_type TEXT,
    entity_id TEXT, detail TEXT, timestamp TEXT NOT NULL
);

-- ---------------------------------------------------------------- ledger protection
-- Posted events and everything beneath them can never be edited or deleted.
CREATE TRIGGER IF NOT EXISTS trg_events_no_edit_posted
BEFORE UPDATE ON stock_events WHEN OLD.status = 'POSTED'
BEGIN SELECT RAISE(ABORT, 'posted stock event is immutable; create a linked correction'); END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON stock_events
BEGIN SELECT RAISE(ABORT, 'stock events cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_lines_no_edit_after_draft
BEFORE UPDATE ON stock_event_lines
BEGIN SELECT RAISE(ABORT, 'stock event lines are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_lines_no_delete
BEFORE DELETE ON stock_event_lines
BEGIN SELECT RAISE(ABORT, 'stock event lines cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_lines_insert_only_open_event
BEFORE INSERT ON stock_event_lines
WHEN (SELECT status FROM stock_events WHERE id = NEW.event_id) NOT IN ('DRAFT')
BEGIN SELECT RAISE(ABORT, 'lines can only be added to a draft event'); END;

CREATE TRIGGER IF NOT EXISTS trg_postings_no_update
BEFORE UPDATE ON stock_postings
BEGIN SELECT RAISE(ABORT, 'stock postings are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_postings_no_delete
BEFORE DELETE ON stock_postings
BEGIN SELECT RAISE(ABORT, 'stock postings cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_approvals_no_update
BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_approvals_no_delete
BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_count_lines_keep_original
BEFORE UPDATE ON stock_count_lines
WHEN OLD.counted_qty IS NOT NULL AND NEW.counted_qty IS NOT OLD.counted_qty
BEGIN SELECT RAISE(ABORT, 'original count is preserved; record a recount instead'); END;
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False: the HTTP server serialises access with a lock.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
