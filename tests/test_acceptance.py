"""The ten acceptance scenarios from the MVP plan, plus core ledger safety checks.
Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from depot import dashboard, rules
from depot.db import connect, new_id
from depot.service import (InsufficientStock, Ledger, LedgerError, PermissionDenied, SeparationOfDuties)


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        org = new_id()
        self.conn.execute("INSERT INTO organizations(id,name) VALUES (?,?)", (org, "Test Depot"))
        self.L = L = Ledger(self.conn, org)
        self.store_k = L.create_user("E1", "Philip Store", ["storekeeper"], pin="1111")
        self.cashier = L.create_user("E2", "Carol Cashier", ["shop_cashier"], pin="2222")
        self.driver = L.create_user("E3", "Dan Driver", ["driver"], pin="3333")
        self.manager = L.create_user("E4", "Mary Manager", ["depot_manager"], pin="4444")
        self.manager2 = L.create_user("E5", "Mo Manager", ["depot_manager"], pin="5555")
        self.accountant = L.create_user("E6", "Ann Accountant", ["accountant"], pin="6666")
        self.auditor = L.create_user("E7", "Ali Auditor", ["internal_auditor"], pin="7777")
        self.ceo = L.create_user("E8", "Chief", ["ceo"], pin="8888")
        self.board = L.create_user("E9", "Board", ["board"], pin="9999")
        self.admin = L.create_user("E10", "Admin", ["system_admin"], pin="0000")
        self.store = L.create_location("Main Store", "STORE", self.store_k)
        self.shop = L.create_location("Shop", "SHOP", self.cashier)
        self.van = L.create_location("Van 1", "VAN", self.driver)
        self.damaged = L.create_location("Damaged", "DAMAGED")
        self.cola = L.create_product("COLA-500", "Cola 500ml (case)", avg_cost=1000)
        self.water = L.create_product("WATER-1L", "Water 1L (case)", avg_cost=500)

    def stock(self, qty=200, ref="GRN-1", product=None):
        return self.L.receive_stock(self.store_k, ref, self.store,
                                    [{"product_id": product or self.cola, "quantity": qty, "unit_cost": 1000}])

    def move(self, actor, etype, ref, frm, to, qty, trip=None, product=None, **kw):
        ev = self.L.create_event(actor, etype, ref, [{"product_id": product or self.cola, "quantity": qty}],
                                 from_location=frm, to_location=to, trip_ref=trip, **kw)
        return self.L.submit(actor, ev["id"])

    def bal(self, loc, product=None):
        return self.L.balance(loc, product or self.cola)


class TestAcceptance(Base):
    # 1
    def test_01_delivery_10_cases_short_of_supplier_note(self):
        ev = self.L.receive_stock(self.store_k, "GRN-9", self.store,
                                  [{"product_id": self.cola, "quantity": 90, "note_qty": 100, "unit_cost": 1000}],
                                  supplier_note_ref="SN-555")
        self.assertEqual(ev["status"], "POSTED")
        self.assertEqual(self.bal(self.store), 90)  # ledger holds what was physically received
        cases = self.L.all("SELECT * FROM investigations WHERE variance_type='SUPPLIER_SHORTAGE'")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["amount"], 10_000)
        self.assertEqual(json.loads(cases[0]["evidence"])["supplier_note"], "SN-555")
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM documents")["n"], 1)

    def test_01b_large_receipt_shortage_survives_approval_wait(self):
        ev = self.L.receive_stock(self.store_k, "GRN-BIG", self.store,
                                  [{"product_id": self.cola, "quantity": 290, "note_qty": 300, "unit_cost": 1000}])
        self.assertEqual(ev["status"], "PENDING_APPROVAL")  # 290,000 is over the approval threshold
        self.assertEqual(self.bal(self.store), 0)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM investigations WHERE variance_type='SUPPLIER_SHORTAGE'")["n"], 1)
        self.L.decide(self.manager, ev["id"], "APPROVED")
        self.assertEqual(self.bal(self.store), 290)

    # 2
    def test_02_store_issues_100_van_returns_90(self):
        self.stock(200)
        self.move(self.store_k, "TRANSFER", "TRIP1-LOAD", self.store, self.van, 100, trip="TRIP1")
        self.move(self.driver, "SALE", "TRIP1-S1", self.van, None, 60, trip="TRIP1")
        self.move(self.driver, "RETURN", "TRIP1-R", self.van, self.store, 30, trip="TRIP1")
        alerts = rules.unreconciled_deliveries(self.L, grace_hours=0)
        self.assertEqual(len(alerts), 1)
        ev = json.loads(alerts[0]["evidence"])
        self.assertEqual(ev["unaccounted"], 10)
        self.assertEqual(alerts[0]["amount"], 10_000)
        self.assertEqual(self.bal(self.van), 10)
        # re-running does not duplicate the case
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM investigations")["n"], 1)
        rules.unreconciled_deliveries(self.L, grace_hours=0)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM investigations")["n"], 1)

    # 3
    def test_03_customer_rejects_5_cases_is_fully_reconciled(self):
        self.stock(200)
        self.move(self.store_k, "TRANSFER", "T2-LOAD", self.store, self.van, 100, trip="T2")
        self.move(self.driver, "SALE", "T2-S", self.van, None, 95, trip="T2")
        self.move(self.driver, "RETURN", "T2-R", self.van, self.store, 5, trip="T2")
        self.assertEqual(rules.unreconciled_deliveries(self.L, grace_hours=0), [])
        self.assertEqual(self.bal(self.van), 0)
        self.assertEqual(self.bal(self.store), 105)

    # 4
    def test_04_cashier_cancels_posted_sale(self):
        self.stock(50)
        self.move(self.store_k, "TRANSFER", "SH-LOAD", self.store, self.shop, 20)
        sale = self.move(self.cashier, "SALE", "INV-1-S", self.shop, None, 5)
        oid = self.L.create_order(self.cashier, "INV-1", "2026-10-14", [{"product_id": self.cola, "quantity": 5, "unit_price": 1500}])
        self.assertEqual(self.bal(self.shop), 15)
        rev = self.L.cancel_order(self.cashier, oid, "customer changed mind", sale_event_id=sale["id"])
        self.assertEqual(rev["status"], "PENDING_APPROVAL")  # cashier cannot silently undo a posted sale
        self.assertEqual(self.bal(self.shop), 15)
        self.L.decide(self.manager, rev["id"], "APPROVED")
        self.assertEqual(self.bal(self.shop), 20)
        orig = self.L.get_event(sale["id"])
        self.assertEqual(orig["status"], "POSTED")  # original preserved
        self.assertEqual(self.L.get_event(rev["id"])["correction_of"], sale["id"])
        self.assertEqual(self.L.one("SELECT status FROM orders WHERE id=?", (oid,))["status"], "CANCELLED")

    # 5
    def test_05_supervisor_cannot_approve_own_adjustment(self):
        self.stock(50)
        ev = self.L.create_event(self.manager, "ADJUST_OUT", "ADJ-1", [{"product_id": self.cola, "quantity": 3}],
                                 from_location=self.store, reason="breakage found")
        ev = self.L.submit(self.manager, ev["id"])
        self.assertEqual(ev["status"], "PENDING_APPROVAL")
        with self.assertRaises(SeparationOfDuties):
            self.L.decide(self.manager, ev["id"], "APPROVED")
        self.assertEqual(self.bal(self.store), 50)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM audit_log WHERE action='approval.blocked_self'")["n"], 1)
        # the custodian of the stock cannot approve either, and non-approvers cannot approve at all
        with self.assertRaises(PermissionDenied):
            self.L.decide(self.store_k, ev["id"], "APPROVED")
        self.L.decide(self.manager2, ev["id"], "APPROVED")
        self.assertEqual(self.bal(self.store), 47)

    # 6
    def test_06_network_failure_retry_does_not_double_post(self):
        self.stock(100)
        evt_id = new_id()
        mk = lambda: self.L.create_event(self.store_k, "TRANSFER", "OFFLINE-1", [{"product_id": self.cola, "quantity": 10}],
                                         from_location=self.store, to_location=self.shop, event_id=evt_id)
        a = mk()
        self.L.submit(self.store_k, a["id"])
        # client never saw the response and replays the same queued transaction
        b = mk()
        self.L.submit(self.store_k, b["id"])
        self.L.submit(self.store_k, b["id"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.bal(self.shop), 10)
        self.assertEqual(self.bal(self.store), 90)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM stock_events WHERE reference='OFFLINE-1'")["n"], 1)

    def test_06b_failed_posting_is_atomic(self):
        self.stock(10)
        ev = self.L.create_event(self.store_k, "TRANSFER", "BIG", [
            {"product_id": self.cola, "quantity": 5}, {"product_id": self.water, "quantity": 5}],
            from_location=self.store, to_location=self.shop)
        with self.assertRaises(InsufficientStock):
            self.L.submit(self.store_k, ev["id"])
        self.assertEqual(self.bal(self.store), 10)  # first line did not leak through
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM stock_postings WHERE location_id=?", (self.shop,))["n"], 0)
        self.assertEqual(self.L.get_event(ev["id"])["status"], "DRAFT")

    # 7
    def test_07_count_corrected_after_independent_recount(self):
        self.stock(100)
        cid = self.L.open_count(self.store_k, self.store)
        self.L.record_count(self.store_k, cid, self.cola, 95)
        with self.assertRaises(SeparationOfDuties):
            self.L.record_recount(self.store_k, cid, self.cola, 100)  # counter cannot recount own line
        with self.assertRaises(LedgerError):
            self.L.record_count(self.store_k, cid, self.cola, 100)  # cannot silently overwrite the first count
        self.L.record_recount(self.manager, cid, self.cola, 100)
        line = self.L.count_lines(cid)[0]
        self.assertEqual((line["counted_qty"], line["recount_qty"], line["variance"]), (95, 100, 0))
        res = self.L.finalize_count(self.manager, cid)
        self.assertEqual(res["investigations"], [])  # recount resolved the variance, history kept
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE stock_count_lines SET counted_qty=100")

    def test_07b_unresolved_count_variance_opens_case_and_needs_approved_adjustment(self):
        self.stock(100)
        cid = self.L.open_count(self.store_k, self.store)
        self.L.record_count(self.store_k, cid, self.cola, 92)
        res = self.L.finalize_count(self.manager, cid)
        self.assertEqual(len(res["investigations"]), 1)
        adj = self.L.propose_count_adjustment(self.manager, cid, self.cola, "confirmed after recount")
        self.assertEqual(adj["status"], "PENDING_APPROVAL")
        self.L.decide(self.manager2, adj["id"], "APPROVED")
        self.assertEqual(self.bal(self.store), 92)

    # 8
    def test_08_payment_after_period_is_timing_not_mismatch(self):
        oid = self.L.create_order(self.cashier, "INV-8", "2026-09-30", [{"product_id": self.cola, "quantity": 10, "unit_price": 1500}])
        self.L.record_payment(self.cashier, oid, "MPESA", 15_000, "QX1", paid_at="2026-10-03T08:00:00+00:00")
        res = rules.sales_to_cash(self.L, "2026-09-01", "2026-09-30")
        self.assertEqual(res["rows"][0]["class"], "TIMING")
        self.assertEqual(res["totals"]["timing_receivable"], 15_000)
        self.assertEqual(res["totals"]["mismatch"], 0)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM investigations")["n"], 0)

    def test_08b_short_payment_opens_case(self):
        oid = self.L.create_order(self.cashier, "INV-8B", "2026-09-30", [{"product_id": self.cola, "quantity": 10, "unit_price": 1500}])
        self.L.record_payment(self.cashier, oid, "CASH", 14_000, paid_at="2026-09-30T10:00:00+00:00")
        res = rules.sales_to_cash(self.L, "2026-09-01", "2026-09-30")
        self.assertEqual(res["rows"][0]["class"], "SHORT_PAID")
        self.assertEqual(res["totals"]["mismatch"], 1000)

    # 9
    def test_09_duplicate_bank_transaction_is_not_imported_twice(self):
        rows = [{"date": "2026-10-01", "amount": 15000, "reference": "mp-abc"},
                {"date": "2026-10-02", "amount": 7000, "reference": "MP-DEF"}]
        r1 = self.L.import_bank_rows(self.accountant, "KCB-1", rows)
        r2 = self.L.import_bank_rows(self.accountant, "KCB-1", rows + [{"date": "2026-10-01", "amount": 15000, "reference": "MP-ABC "}])
        self.assertEqual(r1["added"], 2)
        self.assertEqual(r2["added"], 0)
        self.assertEqual(len(r2["duplicates"]), 3)
        self.assertEqual(self.L.one("SELECT COUNT(*) n FROM bank_transactions")["n"], 2)

    # 10
    def test_10_administrator_cannot_alter_posted_history(self):
        ev = self.stock(100)
        adj = self.L.create_event(self.manager, "ADJUST_OUT", "ADJ-10", [{"product_id": self.cola, "quantity": 1}],
                                  from_location=self.store, reason="breakage")
        self.L.submit(self.manager, adj["id"])
        self.L.decide(self.manager2, adj["id"], "APPROVED")  # creates an approval row to try to tamper with
        for sql in ("UPDATE stock_events SET reference='X' WHERE id=?", "DELETE FROM stock_events WHERE id=?"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql, (ev["id"],))
        for sql in ("UPDATE stock_postings SET quantity_delta=999", "DELETE FROM stock_postings",
                    "UPDATE stock_event_lines SET quantity_base=1", "DELETE FROM stock_event_lines",
                    "UPDATE audit_log SET action='x'", "DELETE FROM audit_log", "DELETE FROM approvals"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(sql)
        self.assertEqual(self.bal(self.store), 99)
        # app level: admin has no stock or approval rights
        with self.assertRaises(PermissionDenied):
            self.L.create_event(self.admin, "ADJUST_IN", "A", [{"product_id": self.cola, "quantity": 1}],
                                to_location=self.store, reason="x")
        with self.assertRaises(PermissionDenied):
            self.L.decide(self.admin, ev["id"], "APPROVED")


class TestSafety(Base):
    def test_negative_stock_refused(self):
        self.stock(5)
        ev = self.L.create_event(self.store_k, "TRANSFER", "N1", [{"product_id": self.cola, "quantity": 6}],
                                 from_location=self.store, to_location=self.shop)
        with self.assertRaises(InsufficientStock):
            self.L.submit(self.store_k, ev["id"])

    def test_reversal_is_linked_and_idempotent(self):
        ev = self.stock(100)
        r1 = self.L.reverse(self.store_k, ev["id"], "entered twice")
        r2 = self.L.reverse(self.store_k, ev["id"], "entered twice")
        self.assertEqual(r1["id"], r2["id"])
        self.L.submit(self.store_k, r1["id"])
        self.L.decide(self.manager, r1["id"], "APPROVED")
        self.assertEqual(self.bal(self.store), 0)

    def test_weighted_average_cost(self):
        self.stock(100, "G1")
        self.L.receive_stock(self.store_k, "G2", self.store, [{"product_id": self.cola, "quantity": 100, "unit_cost": 1200}])
        self.assertEqual(self.L.one("SELECT avg_cost FROM products WHERE id=?", (self.cola,))["avg_cost"], 1100)

    def test_login_and_permissions(self):
        tok, uid = self.L.login("E1", "1111")
        self.assertEqual(self.L.user_for_token(tok), uid)
        with self.assertRaises(PermissionDenied):
            self.L.login("E1", "bad")
        with self.assertRaises(PermissionDenied):
            self.L.create_order(self.driver, "I", "2026-10-14", [{"product_id": self.cola, "quantity": 1, "unit_price": 1}])

    def test_board_view_has_no_people_or_evidence(self):
        self.stock(100)
        cid = self.L.open_count(self.store_k, self.store)
        self.L.record_count(self.store_k, cid, self.cola, 80)
        self.L.finalize_count(self.manager, cid)
        b = dashboard.dashboard(self.L, self.board)
        self.assertEqual(b["view"], "summary")
        self.assertNotIn("investigations", b)
        self.assertNotIn("Philip", json.dumps(b))
        full = dashboard.dashboard(self.L, self.ceo)
        self.assertEqual(full["kpis"]["unresolved_variance"], 20_000)
        self.assertEqual(full["kpis"]["open_investigations"], 1)
        with self.assertRaises(PermissionDenied):
            dashboard.dashboard(self.L, self.cashier)

    def test_close_case_needs_resolution(self):
        self.stock(100)
        cid = self.L.open_count(self.store_k, self.store)
        self.L.record_count(self.store_k, cid, self.cola, 90)
        inv = self.L.finalize_count(self.manager, cid)["investigations"][0]
        with self.assertRaises(LedgerError):
            self.L.close_investigation(self.auditor, inv, " ", True)
        self.L.close_investigation(self.auditor, inv, "Recount and delivery note reviewed", True)
        self.assertEqual(self.L.one("SELECT status FROM investigations WHERE id=?", (inv,))["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
