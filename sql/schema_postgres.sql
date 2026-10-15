-- Depot Ledger: PostgreSQL production schema (mirrors the SQLite MVP in depot/db.py).
-- NOTE: written alongside the MVP but NOT yet executed against a live PostgreSQL server
-- in this build; run it in a scratch database and fix any dialect issue before relying on it.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE organizations (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Africa/Nairobi');
CREATE TABLE users (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), org_id UUID NOT NULL REFERENCES organizations, employee_no TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, pin_hash TEXT, pin_salt TEXT, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE roles (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), role_name TEXT NOT NULL UNIQUE);
CREATE TABLE user_roles (user_id UUID REFERENCES users, role_id UUID REFERENCES roles, PRIMARY KEY (user_id, role_id));
CREATE TABLE locations (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), org_id UUID NOT NULL REFERENCES organizations, name TEXT NOT NULL,
  location_type TEXT NOT NULL CHECK (location_type IN ('STORE','SHOP','VAN','CUSTODY','IN_TRANSIT','DAMAGED','QUARANTINE')),
  custodian_id UUID REFERENCES users, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE products (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL, base_uom TEXT NOT NULL,
  unit_cost_method TEXT NOT NULL DEFAULT 'WEIGHTED_AVERAGE', avg_cost NUMERIC(18,4) NOT NULL DEFAULT 0);
CREATE TABLE uom_conversions (product_id UUID REFERENCES products, from_uom TEXT, to_uom TEXT, factor NUMERIC(18,6) NOT NULL CHECK (factor > 0),
  PRIMARY KEY (product_id, from_uom, to_uom));
CREATE TABLE batches (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), product_id UUID NOT NULL REFERENCES products, batch_no TEXT NOT NULL,
  expiry_date DATE, UNIQUE (product_id, batch_no));

CREATE TABLE stock_events (
  id UUID PRIMARY KEY, org_id UUID NOT NULL REFERENCES organizations,
  event_type TEXT NOT NULL CHECK (event_type IN ('RECEIVE','TRANSFER','SALE','RETURN','DAMAGE','ADJUST_IN','ADJUST_OUT','REVERSAL')),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','PENDING_APPROVAL','POSTED','REJECTED')),
  reference TEXT NOT NULL, trip_ref TEXT, reason TEXT,
  from_location_id UUID REFERENCES locations, to_location_id UUID REFERENCES locations,
  created_by UUID NOT NULL REFERENCES users, custodian_id UUID REFERENCES users,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), posted_at TIMESTAMPTZ,
  correction_of UUID REFERENCES stock_events,
  UNIQUE (org_id, event_type, reference));
CREATE TABLE stock_event_lines (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), event_id UUID NOT NULL REFERENCES stock_events,
  product_id UUID NOT NULL REFERENCES products, quantity_base NUMERIC(18,3) NOT NULL CHECK (quantity_base > 0),
  batch_id UUID REFERENCES batches, unit_cost NUMERIC(18,4));
CREATE TABLE stock_postings (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), event_line_id UUID NOT NULL REFERENCES stock_event_lines,
  location_id UUID NOT NULL REFERENCES locations, product_id UUID NOT NULL REFERENCES products,
  quantity_delta NUMERIC(18,3) NOT NULL CHECK (quantity_delta <> 0), posted_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX ix_postings_loc_prod ON stock_postings (location_id, product_id);
CREATE TABLE approvals (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), event_id UUID NOT NULL REFERENCES stock_events,
  approver_id UUID NOT NULL REFERENCES users, decision TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')), reason TEXT,
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE documents (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), document_type TEXT NOT NULL, reference TEXT NOT NULL, file_uri TEXT,
  event_id UUID REFERENCES stock_events, created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE customers (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT NOT NULL);
CREATE TABLE orders (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), customer_id UUID REFERENCES customers, invoice_no TEXT NOT NULL UNIQUE,
  order_date DATE NOT NULL, payment_terms TEXT NOT NULL DEFAULT 'CASH' CHECK (payment_terms IN ('CASH','CREDIT')),
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CANCELLED')), total NUMERIC(18,2) NOT NULL CHECK (total >= 0),
  created_by UUID REFERENCES users, cancelled_by UUID, cancel_reason TEXT);
CREATE TABLE order_lines (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), order_id UUID NOT NULL REFERENCES orders,
  product_id UUID NOT NULL REFERENCES products, quantity NUMERIC(18,3) NOT NULL, unit_price NUMERIC(18,2) NOT NULL);
CREATE TABLE payments (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), order_id UUID NOT NULL REFERENCES orders, method TEXT NOT NULL,
  amount NUMERIC(18,2) NOT NULL, reference TEXT, paid_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE bank_transactions (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), fingerprint TEXT NOT NULL UNIQUE, account TEXT NOT NULL,
  txn_date DATE NOT NULL, amount NUMERIC(18,2) NOT NULL, reference TEXT, imported_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE stock_counts (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), location_id UUID NOT NULL REFERENCES locations,
  cutoff_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','FINALIZED')),
  opened_by UUID NOT NULL REFERENCES users, finalized_at TIMESTAMPTZ);
CREATE TABLE stock_count_lines (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), count_id UUID NOT NULL REFERENCES stock_counts,
  product_id UUID NOT NULL REFERENCES products, expected_qty NUMERIC(18,3) NOT NULL, counted_qty NUMERIC(18,3),
  counted_by UUID REFERENCES users, recount_qty NUMERIC(18,3), recounted_by UUID REFERENCES users, UNIQUE (count_id, product_id),
  CHECK (recounted_by IS NULL OR recounted_by IS DISTINCT FROM counted_by));   -- recount by a different person
CREATE TABLE investigations (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), variance_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH')), amount NUMERIC(18,2) NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','IN_REVIEW','ESCALATED','CLOSED')),
  assigned_to UUID REFERENCES users, summary TEXT NOT NULL, evidence JSONB NOT NULL DEFAULT '{}', source_key TEXT UNIQUE,
  resolution TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_at TIMESTAMPTZ);
CREATE TABLE audit_log (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), actor_id UUID, action TEXT NOT NULL, entity_type TEXT,
  entity_id TEXT, detail JSONB, "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now());

-- ---- ledger protection (applies to every role, including application administrators)
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% on % is not allowed: the ledger is append-only; post a linked correction', TG_OP, TG_TABLE_NAME; END $$;
CREATE TRIGGER t_postings_ro  BEFORE UPDATE OR DELETE ON stock_postings    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER t_lines_ro     BEFORE UPDATE OR DELETE ON stock_event_lines FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER t_approvals_ro BEFORE UPDATE OR DELETE ON approvals         FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER t_audit_ro     BEFORE UPDATE OR DELETE ON audit_log         FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER t_events_nodel BEFORE DELETE ON stock_events                FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE FUNCTION events_no_edit_posted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF OLD.status = 'POSTED' THEN RAISE EXCEPTION 'posted stock event is immutable; create a linked correction'; END IF; RETURN NEW; END $$;
CREATE TRIGGER t_events_posted_ro BEFORE UPDATE ON stock_events FOR EACH ROW EXECUTE FUNCTION events_no_edit_posted();
CREATE FUNCTION count_keep_original() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF OLD.counted_qty IS NOT NULL AND NEW.counted_qty IS DISTINCT FROM OLD.counted_qty THEN
  RAISE EXCEPTION 'original count is preserved; record a recount instead'; END IF; RETURN NEW; END $$;
CREATE TRIGGER t_count_orig BEFORE UPDATE ON stock_count_lines FOR EACH ROW EXECUTE FUNCTION count_keep_original();

-- Balance is always derived from the ledger. Never store a mutable current_stock.
CREATE VIEW stock_balances AS
  SELECT location_id, product_id, SUM(quantity_delta) AS quantity FROM stock_postings GROUP BY location_id, product_id;
-- Recommended hardening: REVOKE UPDATE, DELETE, TRUNCATE on the tables above from the application role.
