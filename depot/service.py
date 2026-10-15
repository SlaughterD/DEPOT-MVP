"""Ledger service: stock events, postings, approvals, counts, orders, payments,
investigations and the audit trail.

Design rules enforced here (and, for immutability, again by DB triggers):
  * stock is never stored as a mutable number; balances are summed from postings
  * posted events are never edited; corrections are new linked REVERSAL events
  * a posting is atomic: all postings of an event succeed or none do
  * submitting/posting the same event twice posts it once (idempotent retries)
  * negative stock is refused
  * high-risk events need a second person (separation of duties)
"""
import hashlib
import json
import secrets
from contextlib import contextmanager

from .db import new_id, now_iso


class LedgerError(Exception):
    pass


class PermissionDenied(LedgerError):
    pass


class SeparationOfDuties(PermissionDenied):
    pass


class InsufficientStock(LedgerError):
    pass


# ----------------------------------------------------------------------- permissions
ROLE_PERMS = {
    "storekeeper": {
        "event:RECEIVE", "event:TRANSFER", "event:RETURN", "event:DAMAGE",
        "event:ADJUST_IN", "event:ADJUST_OUT", "event:REVERSAL",
        "count.open", "count.record", "count.recount",
    },
    "shop_cashier": {"event:SALE", "event:REVERSAL", "order.create", "order.cancel", "payment.record"},
    "driver": {"event:SALE", "event:RETURN", "event:DAMAGE"},
    "depot_manager": {
        "event:ADJUST_IN", "event:ADJUST_OUT", "event:REVERSAL", "approve",
        "count.open", "count.record", "count.recount",
        "investigation.manage", "rules.run", "dashboard.full",
    },
    "accountant": {"payment.record", "bank.import", "rules.run", "dashboard.full"},
    "internal_auditor": {"count.recount", "investigation.manage", "audit.read", "rules.run", "dashboard.full"},
    "ceo": {"dashboard.full", "audit.read"},
    "board": {"dashboard.summary"},
    "system_admin": {"user.manage"},
}

# Events that always need a second-person approval before they post.
ALWAYS_APPROVE = {"ADJUST_IN", "ADJUST_OUT", "REVERSAL"}
NEEDS_REASON = {"ADJUST_IN", "ADJUST_OUT", "REVERSAL", "DAMAGE"}


def q3(x) -> float:
    return round(float(x or 0), 3)


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


class Ledger:
    def __init__(self, conn, org_id, approval_value_threshold=250_000.0,
                 severity_high=100_000.0, severity_medium=20_000.0):
        self.c = conn
        self.org_id = org_id
        self.approval_value_threshold = approval_value_threshold
        self.severity_high = severity_high
        self.severity_medium = severity_medium
        self._tx_depth = 0

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def tx(self):
        """All-or-nothing transaction; nested use joins the outer one."""
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self.c.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        finally:
            self._tx_depth = 0

    def one(self, sql, args=()):
        r = self.c.execute(sql, args).fetchone()
        return dict(r) if r else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def audit(self, actor_id, action, entity_type=None, entity_id=None, detail=None):
        self.c.execute(
            "INSERT INTO audit_log(id,actor_id,action,entity_type,entity_id,detail,timestamp) VALUES (?,?,?,?,?,?,?)",
            (new_id(), actor_id, action, entity_type, entity_id,
             json.dumps(detail, default=str) if detail is not None else None, now_iso()),
        )

    # ------------------------------------------------------------------ users & auth
    def create_role(self, role_name):
        r = self.one("SELECT id FROM roles WHERE role_name=?", (role_name,))
        if r:
            return r["id"]
        rid = new_id()
        self.c.execute("INSERT INTO roles(id,role_name) VALUES (?,?)", (rid, role_name))
        return rid

    def create_user(self, employee_no, name, roles=(), pin=None, actor=None):
        uid = new_id()
        salt = secrets.token_hex(8)
        with self.tx():
            self.c.execute(
                "INSERT INTO users(id,org_id,employee_no,name,pin_hash,pin_salt) VALUES (?,?,?,?,?,?)",
                (uid, self.org_id, employee_no, name, hash_pin(pin, salt) if pin else None, salt),
            )
            for r in roles:
                self.c.execute("INSERT INTO user_roles(user_id,role_id) VALUES (?,?)", (uid, self.create_role(r)))
            self.audit(actor, "user.create", "user", uid, {"employee_no": employee_no, "roles": list(roles)})
        return uid

    def roles_of(self, user_id):
        return {r["role_name"] for r in self.all(
            "SELECT r.role_name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?", (user_id,))}

    def perms_of(self, user_id):
        perms = set()
        for r in self.roles_of(user_id):
            perms |= ROLE_PERMS.get(r, set())
        return perms

    def get_user(self, user_id):
        u = self.one("SELECT id,employee_no,name,active FROM users WHERE id=?", (user_id,))
        if not u or not u["active"]:
            raise PermissionDenied("unknown or inactive user")
        return u

    def require(self, user_id, perm):
        self.get_user(user_id)
        if perm not in self.perms_of(user_id):
            self.audit(user_id, "permission.denied", detail={"perm": perm})
            raise PermissionDenied(f"user lacks permission '{perm}'")

    def login(self, employee_no, pin):
        u = self.one("SELECT * FROM users WHERE employee_no=? AND active=1", (employee_no,))
        if not u or not u["pin_hash"] or hash_pin(pin, u["pin_salt"]) != u["pin_hash"]:
            self.audit(None, "login.failed", detail={"employee_no": employee_no})
            raise PermissionDenied("invalid credentials")
        token = secrets.token_urlsafe(24)
        self.c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES (?,?,?)", (token, u["id"], now_iso()))
        self.audit(u["id"], "login")
        return token, u["id"]

    def user_for_token(self, token):
        r = self.one("SELECT user_id FROM sessions WHERE token=?", (token,))
        return r["user_id"] if r else None

    # ------------------------------------------------------------------ master data
    def create_location(self, name, location_type, custodian_id=None):
        lid = new_id()
        self.c.execute("INSERT INTO locations(id,org_id,name,location_type,custodian_id) VALUES (?,?,?,?,?)",
                       (lid, self.org_id, name, location_type, custodian_id))
        return lid

    def create_product(self, sku, name, base_uom="CASE", avg_cost=0.0):
        pid = new_id()
        self.c.execute("INSERT INTO products(id,sku,name,base_uom,avg_cost) VALUES (?,?,?,?,?)",
                       (pid, sku, name, base_uom, avg_cost))
        return pid

    def location(self, location_id):
        loc = self.one("SELECT * FROM locations WHERE id=?", (location_id,))
        if not loc or not loc["active"]:
            raise LedgerError("unknown or inactive location")
        return loc

    # ------------------------------------------------------------------ balances
    def balance(self, location_id, product_id, at=None):
        sql = "SELECT COALESCE(SUM(quantity_delta),0) s FROM stock_postings WHERE location_id=? AND product_id=?"
        args = [location_id, product_id]
        if at:
            sql += " AND posted_at<=?"
            args.append(at)
        return q3(self.one(sql, args)["s"])

    def balances(self, location_id, at=None):
        sql = "SELECT product_id, SUM(quantity_delta) s FROM stock_postings WHERE location_id=?"
        args = [location_id]
        if at:
            sql += " AND posted_at<=?"
            args.append(at)
        sql += " GROUP BY product_id"
        return {r["product_id"]: q3(r["s"]) for r in self.all(sql, args) if q3(r["s"]) != 0}

    def total_on_hand(self, product_id):
        return q3(self.one("SELECT COALESCE(SUM(quantity_delta),0) s FROM stock_postings WHERE product_id=?",
                           (product_id,))["s"])

    # ------------------------------------------------------------------ stock events
    def get_event(self, event_id):
        ev = self.one("SELECT * FROM stock_events WHERE id=?", (event_id,))
        if not ev:
            raise LedgerError("event not found")
        ev["lines"] = self.all("SELECT * FROM stock_event_lines WHERE event_id=? ORDER BY rowid", (event_id,))
        return ev

    def event_value(self, ev):
        total = 0.0
        for ln in ev["lines"]:
            cost = ln["unit_cost"]
            if cost is None:
                cost = self.one("SELECT avg_cost FROM products WHERE id=?", (ln["product_id"],))["avg_cost"]
            total += ln["quantity_base"] * cost
        return round(total, 2)

    def create_event(self, actor, event_type, reference, lines, from_location=None, to_location=None,
                     trip_ref=None, reason=None, event_id=None, custodian_id=None, correction_of=None):
        """Create a DRAFT event. Safe to retry: same id or same (type, reference) returns the original."""
        self.require(actor, f"event:{event_type}")
        with self.tx():
            if event_id:
                ex = self.one("SELECT id,reference,event_type FROM stock_events WHERE id=?", (event_id,))
                if ex:
                    if (ex["reference"], ex["event_type"]) != (reference, event_type):
                        raise LedgerError("event id already used for a different transaction")
                    return self.get_event(event_id)
            ex = self.one("SELECT id,created_by FROM stock_events WHERE org_id=? AND event_type=? AND reference=?",
                          (self.org_id, event_type, reference))
            if ex:
                if ex["created_by"] != actor:
                    raise LedgerError(f"reference '{reference}' already used for {event_type}")
                return self.get_event(ex["id"])
            if event_type in NEEDS_REASON and not (reason and reason.strip()):
                raise LedgerError(f"{event_type} requires a reason")
            if not lines:
                raise LedgerError("an event needs at least one line")
            self._validate_shape(event_type, from_location, to_location)
            eid = event_id or new_id()
            self.c.execute(
                """INSERT INTO stock_events(id,org_id,event_type,status,reference,trip_ref,reason,from_location_id,
                   to_location_id,created_by,custodian_id,created_at,correction_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, self.org_id, event_type, "DRAFT", reference, trip_ref, reason, from_location, to_location,
                 actor, custodian_id, now_iso(), correction_of))
            for ln in lines:
                qty = float(ln["quantity"])
                if qty <= 0:
                    raise LedgerError("line quantity must be positive")
                self.c.execute(
                    "INSERT INTO stock_event_lines(id,event_id,product_id,quantity_base,batch_id,unit_cost) VALUES (?,?,?,?,?,?)",
                    (new_id(), eid, ln["product_id"], qty, ln.get("batch_id"), ln.get("unit_cost")))
            self.audit(actor, "event.create", "stock_event", eid, {"type": event_type, "reference": reference})
            return self.get_event(eid)

    def _validate_shape(self, t, frm, to):
        need_from = t in {"TRANSFER", "SALE", "RETURN", "DAMAGE", "ADJUST_OUT"}
        need_to = t in {"RECEIVE", "TRANSFER", "RETURN", "DAMAGE", "ADJUST_IN"}
        if t == "REVERSAL":
            return
        if need_from and not frm:
            raise LedgerError(f"{t} needs a from-location")
        if need_to and not to:
            raise LedgerError(f"{t} needs a to-location")
        if frm:
            self.location(frm)
        if to:
            self.location(to)
        if frm and to and frm == to:
            raise LedgerError("from and to locations must differ")
        if t == "DAMAGE" and self.location(to)["location_type"] not in ("DAMAGED", "QUARANTINE"):
            raise LedgerError("damage must be moved to a DAMAGED or QUARANTINE location")

    def requires_approval(self, ev):
        return ev["event_type"] in ALWAYS_APPROVE or self.event_value(ev) >= self.approval_value_threshold

    def submit(self, actor, event_id):
        """Creator submits. Posts immediately, or parks the event for a second person."""
        with self.tx():
            ev = self.get_event(event_id)
            if ev["status"] in ("POSTED", "PENDING_APPROVAL", "REJECTED"):
                return ev  # idempotent retry after a network failure
            if ev["created_by"] != actor:
                raise PermissionDenied("only the creator can submit a draft")
            self.require(actor, f"event:{ev['event_type']}")
            if self.requires_approval(ev):
                self.c.execute("UPDATE stock_events SET status='PENDING_APPROVAL' WHERE id=?", (event_id,))
                self.audit(actor, "event.submit_for_approval", "stock_event", event_id,
                           {"value": self.event_value(ev)})
                return self.get_event(event_id)
            self._post(ev, actor)
            return self.get_event(event_id)

    def decide(self, approver, event_id, decision, reason=None):
        try:
            return self._decide(approver, event_id, decision, reason)
        except SeparationOfDuties:
            # recorded outside the rolled-back transaction so the blocked attempt stays in the audit trail
            self.audit(approver, "approval.blocked_self", "stock_event", event_id)
            raise

    def _decide(self, approver, event_id, decision, reason=None):
        self.require(approver, "approve")
        if decision not in ("APPROVED", "REJECTED"):
            raise LedgerError("decision must be APPROVED or REJECTED")
        with self.tx():
            ev = self.get_event(event_id)
            if ev["status"] == "POSTED" and decision == "APPROVED":
                return ev
            if ev["status"] != "PENDING_APPROVAL":
                raise LedgerError(f"event is {ev['status']}, not awaiting approval")
            if approver in (ev["created_by"], ev["custodian_id"]):
                raise SeparationOfDuties("approver cannot be the requester or the custodian of the stock")
            if decision == "REJECTED" and not (reason and reason.strip()):
                raise LedgerError("a rejection needs a reason")
            self.c.execute("INSERT INTO approvals(id,event_id,approver_id,decision,reason,decided_at) VALUES (?,?,?,?,?,?)",
                           (new_id(), event_id, approver, decision, reason, now_iso()))
            if decision == "REJECTED":
                self.c.execute("UPDATE stock_events SET status='REJECTED' WHERE id=?", (event_id,))
                self.audit(approver, "event.rejected", "stock_event", event_id, {"reason": reason})
            else:
                self._post(ev, approver)
            return self.get_event(event_id)

    def reverse(self, actor, event_id, reason):
        """Correction = a new linked event that undoes the original. Original stays untouched."""
        orig = self.get_event(event_id)
        if orig["status"] != "POSTED":
            raise LedgerError("only posted events can be reversed")
        if orig["event_type"] == "REVERSAL":
            raise LedgerError("a reversal cannot itself be reversed; post a new correcting event")
        with self.tx():
            ex = self.one("SELECT id FROM stock_events WHERE correction_of=? AND event_type='REVERSAL' AND status!='REJECTED'",
                          (event_id,))
            if ex:
                return self.get_event(ex["id"])
            lines = [{"product_id": l["product_id"], "quantity": l["quantity_base"], "unit_cost": l["unit_cost"]}
                     for l in orig["lines"]]
            return self.create_event(actor, "REVERSAL", f"REV-{orig['reference']}", lines, reason=reason,
                                     correction_of=event_id, trip_ref=orig["trip_ref"])

    # ------------------------------------------------------------------ posting
    def _postings_for(self, ev):
        """Return [(line_id, location_id, product_id, delta)]."""
        t, out = ev["event_type"], []
        if t == "REVERSAL":
            orig = self.get_event(ev["correction_of"])
            if orig["status"] != "POSTED":
                raise LedgerError("original event is not posted")
            for new_ln, old_ln in zip(ev["lines"], orig["lines"]):
                for p in self.all("SELECT * FROM stock_postings WHERE event_line_id=?", (old_ln["id"],)):
                    out.append((new_ln["id"], p["location_id"], p["product_id"], -p["quantity_delta"]))
            return out
        for ln in ev["lines"]:
            q = ln["quantity_base"]
            if t in ("TRANSFER", "RETURN", "DAMAGE"):
                out += [(ln["id"], ev["from_location_id"], ln["product_id"], -q),
                        (ln["id"], ev["to_location_id"], ln["product_id"], q)]
            elif t in ("SALE", "ADJUST_OUT"):
                out.append((ln["id"], ev["from_location_id"], ln["product_id"], -q))
            elif t in ("RECEIVE", "ADJUST_IN"):
                out.append((ln["id"], ev["to_location_id"], ln["product_id"], q))
        return out

    def _post(self, ev, posted_by):
        with self.tx():
            fresh = self.get_event(ev["id"])
            if fresh["status"] == "POSTED":
                return
            postings = self._postings_for(fresh)
            net = {}
            for _, loc, prod, d in postings:
                net[(loc, prod)] = net.get((loc, prod), 0) + d
            for (loc, prod), d in net.items():
                if d < 0 and self.balance(loc, prod) + d < -1e-9:
                    name = self.location(loc)["name"]
                    raise InsufficientStock(
                        f"posting would take {name} below zero ({self.balance(loc, prod)} on hand, {-d} needed)")
            if fresh["event_type"] == "RECEIVE":
                self._update_avg_cost(fresh)
            ts = now_iso()
            for line_id, loc, prod, d in postings:
                self.c.execute(
                    "INSERT INTO stock_postings(id,event_line_id,location_id,product_id,quantity_delta,posted_at) VALUES (?,?,?,?,?,?)",
                    (new_id(), line_id, loc, prod, d, ts))
            self.c.execute("UPDATE stock_events SET status='POSTED', posted_at=? WHERE id=?", (ts, ev["id"]))
            self.audit(posted_by, "event.post", "stock_event", ev["id"],
                       {"type": fresh["event_type"], "reference": fresh["reference"], "value": self.event_value(fresh)})

    def _update_avg_cost(self, ev):
        for ln in ev["lines"]:
            if ln["unit_cost"] is None:
                continue
            p = self.one("SELECT avg_cost FROM products WHERE id=?", (ln["product_id"],))
            on_hand = self.total_on_hand(ln["product_id"])
            new_total = on_hand + ln["quantity_base"]
            avg = (on_hand * p["avg_cost"] + ln["quantity_base"] * ln["unit_cost"]) / new_total
            self.c.execute("UPDATE products SET avg_cost=? WHERE id=?", (round(avg, 4), ln["product_id"]))

    # ------------------------------------------------------------------ receiving vs supplier note
    def receive_stock(self, actor, reference, to_location, lines, supplier_note_ref=None, file_uri=None):
        """Receive what was physically counted in. Each line may carry the supplier-note quantity;
        any difference is kept as evidence and opens a SUPPLIER_SHORTAGE case. The ledger always
        records the physical quantity, never the paper quantity."""
        ev = self.create_event(actor, "RECEIVE", reference,
                               [{"product_id": l["product_id"], "quantity": l["quantity"], "unit_cost": l.get("unit_cost")}
                                for l in lines], to_location=to_location)
        ev = self.submit(actor, ev["id"])
        with self.tx():
            if supplier_note_ref and not self.one("SELECT id FROM documents WHERE event_id=? AND reference=?",
                                                  (ev["id"], supplier_note_ref)):
                self.c.execute("INSERT INTO documents(id,document_type,reference,file_uri,event_id,created_at) VALUES (?,?,?,?,?,?)",
                               (new_id(), "SUPPLIER_NOTE", supplier_note_ref, file_uri, ev["id"], now_iso()))
            for l in lines:
                if l.get("note_qty") is None:
                    continue
                gap = q3(float(l["note_qty"]) - float(l["quantity"]))
                if gap == 0:
                    continue
                prod = self.one("SELECT name, avg_cost FROM products WHERE id=?", (l["product_id"],))
                self.open_investigation(
                    "SUPPLIER_SHORTAGE", round(abs(gap) * (l.get("unit_cost") or prod["avg_cost"]), 2),
                    f"{reference}: {prod['name']} note says {l['note_qty']}, received {l['quantity']} ({-gap:+})",
                    {"event_id": ev["id"], "reference": reference, "supplier_note": supplier_note_ref,
                     "product_id": l["product_id"], "note_qty": l["note_qty"], "received_qty": l["quantity"],
                     "location_id": to_location, "received_by": actor, "receipt_status": ev["status"]},
                    source_key=f"rcv:{ev['id']}:{l['product_id']}", actor=actor)
        return ev

    # ------------------------------------------------------------------ stock counts
    def open_count(self, actor, location_id):
        self.require(actor, "count.open")
        self.location(location_id)
        with self.tx():
            cid, cutoff = new_id(), now_iso()
            self.c.execute("INSERT INTO stock_counts(id,location_id,cutoff_at,opened_by) VALUES (?,?,?,?)",
                           (cid, location_id, cutoff, actor))
            for prod, qty in self.balances(location_id, at=cutoff).items():
                self.c.execute("INSERT INTO stock_count_lines(id,count_id,product_id,expected_qty) VALUES (?,?,?,?)",
                               (new_id(), cid, prod, qty))
            self.audit(actor, "count.open", "stock_count", cid, {"location": location_id})
            return cid

    def _count(self, count_id, must_be_open=True):
        c = self.one("SELECT * FROM stock_counts WHERE id=?", (count_id,))
        if not c:
            raise LedgerError("count not found")
        if must_be_open and c["status"] != "OPEN":
            raise LedgerError("count is already finalized")
        return c

    def record_count(self, actor, count_id, product_id, counted_qty):
        self.require(actor, "count.record")
        with self.tx():
            self._count(count_id)
            line = self.one("SELECT * FROM stock_count_lines WHERE count_id=? AND product_id=?", (count_id, product_id))
            if not line:  # product found physically that the ledger says is not here
                lid = new_id()
                self.c.execute("INSERT INTO stock_count_lines(id,count_id,product_id,expected_qty) VALUES (?,?,?,0)",
                               (lid, count_id, product_id))
                line = self.one("SELECT * FROM stock_count_lines WHERE id=?", (lid,))
            if line["counted_qty"] is not None:
                raise LedgerError("already counted; the original count is preserved - record a recount instead")
            self.c.execute("UPDATE stock_count_lines SET counted_qty=?, counted_by=? WHERE id=?",
                           (float(counted_qty), actor, line["id"]))
            self.audit(actor, "count.record", "stock_count", count_id, {"product": product_id, "qty": counted_qty})

    def record_recount(self, actor, count_id, product_id, recount_qty):
        self.require(actor, "count.recount")
        with self.tx():
            self._count(count_id)
            line = self.one("SELECT * FROM stock_count_lines WHERE count_id=? AND product_id=?", (count_id, product_id))
            if not line or line["counted_qty"] is None:
                raise LedgerError("nothing to recount: the line has not been counted")
            if line["counted_by"] == actor:
                raise SeparationOfDuties("a recount must be done by someone other than the original counter")
            self.c.execute("UPDATE stock_count_lines SET recount_qty=?, recounted_by=? WHERE id=?",
                           (float(recount_qty), actor, line["id"]))
            self.audit(actor, "count.recount", "stock_count", count_id, {"product": product_id, "qty": recount_qty})

    def count_lines(self, count_id):
        out = []
        for l in self.all("SELECT * FROM stock_count_lines WHERE count_id=?", (count_id,)):
            eff = l["recount_qty"] if l["recount_qty"] is not None else l["counted_qty"]
            l["effective_qty"] = eff
            l["variance"] = None if eff is None else q3(eff - l["expected_qty"])
            out.append(l)
        return out

    def finalize_count(self, actor, count_id, tolerance_qty=0.0):
        """Close the count; every line outside tolerance becomes an investigation case."""
        self.require(actor, "count.open")
        with self.tx():
            cnt = self._count(count_id)
            lines = self.count_lines(count_id)
            if any(l["effective_qty"] is None for l in lines):
                raise LedgerError("every expected line must be counted before finalizing")
            cases = []
            for l in lines:
                if abs(l["variance"]) > tolerance_qty:
                    cost = self.one("SELECT avg_cost, name FROM products WHERE id=?", (l["product_id"],))
                    amount = round(abs(l["variance"]) * cost["avg_cost"], 2)
                    inv = self.open_investigation(
                        "STOCK_DISCREPANCY", amount,
                        f"{cost['name']}: counted {l['effective_qty']} vs expected {l['expected_qty']} "
                        f"({l['variance']:+}) at {self.location(cnt['location_id'])['name']}",
                        {"count_id": count_id, "product_id": l["product_id"], "location_id": cnt["location_id"],
                         "expected": l["expected_qty"], "counted": l["counted_qty"], "recount": l["recount_qty"],
                         "counted_by": l["counted_by"], "recounted_by": l["recounted_by"],
                         "variance_qty": l["variance"], "custodian_id": self.location(cnt["location_id"])["custodian_id"]},
                        source_key=f"count:{count_id}:{l['product_id']}", actor=actor)
                    cases.append(inv["id"])
            self.c.execute("UPDATE stock_counts SET status='FINALIZED', finalized_at=? WHERE id=?", (now_iso(), count_id))
            ok = sum(1 for l in lines if abs(l["variance"]) <= tolerance_qty)
            self.audit(actor, "count.finalize", "stock_count", count_id,
                       {"lines": len(lines), "within_tolerance": ok, "cases": cases})
            return {"count_id": count_id, "lines": len(lines), "within_tolerance": ok, "investigations": cases}

    def propose_count_adjustment(self, actor, count_id, product_id, reason):
        """Turn a count variance into an ADJUST event. It still needs a second person to approve."""
        cnt = self._count(count_id, must_be_open=False)
        line = next((l for l in self.count_lines(count_id) if l["product_id"] == product_id), None)
        if not line or not line["variance"]:
            raise LedgerError("no variance to adjust")
        t = "ADJUST_IN" if line["variance"] > 0 else "ADJUST_OUT"
        kw = {"to_location": cnt["location_id"]} if t == "ADJUST_IN" else {"from_location": cnt["location_id"]}
        ev = self.create_event(actor, t, f"CNT-{count_id[:8]}-{product_id[:6]}",
                               [{"product_id": product_id, "quantity": abs(line["variance"])}], reason=reason,
                               custodian_id=self.location(cnt["location_id"])["custodian_id"], **kw)
        return self.submit(actor, ev["id"])

    # ------------------------------------------------------------------ investigations
    def severity_for(self, amount):
        return "HIGH" if amount >= self.severity_high else "MEDIUM" if amount >= self.severity_medium else "LOW"

    def open_investigation(self, variance_type, amount, summary, evidence, source_key=None, actor=None, severity=None):
        if source_key:
            ex = self.one("SELECT * FROM investigations WHERE source_key=?", (source_key,))
            if ex:
                return ex
        iid = new_id()
        self.c.execute(
            "INSERT INTO investigations(id,variance_type,severity,amount,summary,evidence,source_key,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, variance_type, severity or self.severity_for(amount), amount, summary,
             json.dumps(evidence, default=str), source_key, now_iso()))
        self.audit(actor, "investigation.open", "investigation", iid, {"type": variance_type, "amount": amount})
        return self.one("SELECT * FROM investigations WHERE id=?", (iid,))

    def assign_investigation(self, actor, inv_id, assignee):
        self.require(actor, "investigation.manage")
        self.get_user(assignee)
        self.c.execute("UPDATE investigations SET assigned_to=?, status='IN_REVIEW' WHERE id=? AND status!='CLOSED'",
                       (assignee, inv_id))
        self.audit(actor, "investigation.assign", "investigation", inv_id, {"assignee": assignee})

    def escalate_investigation(self, actor, inv_id, note):
        self.require(actor, "investigation.manage")
        self.c.execute("UPDATE investigations SET status='ESCALATED' WHERE id=? AND status!='CLOSED'", (inv_id,))
        self.audit(actor, "investigation.escalate", "investigation", inv_id, {"note": note})

    def close_investigation(self, actor, inv_id, resolution, explanation_supported):
        """A conclusion is recorded by a person with evidence; the system never declares theft."""
        self.require(actor, "investigation.manage")
        if not (resolution and resolution.strip()):
            raise LedgerError("closing a case needs a written resolution")
        self.c.execute("UPDATE investigations SET status='CLOSED', resolution=?, closed_at=? WHERE id=?",
                       (resolution, now_iso(), inv_id))
        self.audit(actor, "investigation.close", "investigation", inv_id,
                   {"explanation_supported": bool(explanation_supported), "resolution": resolution})

    # ------------------------------------------------------------------ orders, payments, bank
    def create_order(self, actor, invoice_no, order_date, lines, customer_id=None, terms="CASH"):
        self.require(actor, "order.create")
        with self.tx():
            total = round(sum(float(l["quantity"]) * float(l["unit_price"]) for l in lines), 2)
            oid = new_id()
            self.c.execute(
                "INSERT INTO orders(id,customer_id,invoice_no,order_date,payment_terms,total,created_by) VALUES (?,?,?,?,?,?,?)",
                (oid, customer_id, invoice_no, order_date, terms, total, actor))
            for l in lines:
                self.c.execute("INSERT INTO order_lines(id,order_id,product_id,quantity,unit_price) VALUES (?,?,?,?,?)",
                               (new_id(), oid, l["product_id"], l["quantity"], l["unit_price"]))
            self.audit(actor, "order.create", "order", oid, {"invoice_no": invoice_no, "total": total})
            return oid

    def cancel_order(self, actor, order_id, reason, sale_event_id=None):
        """Cancelling is explicit, reasoned and audited. The stock side is a reversal that a manager must approve."""
        self.require(actor, "order.cancel")
        if not (reason and reason.strip()):
            raise LedgerError("cancelling a sale needs a reason")
        with self.tx():
            o = self.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not o:
                raise LedgerError("order not found")
            if o["status"] == "CANCELLED":
                return None
            self.c.execute("UPDATE orders SET status='CANCELLED', cancelled_by=?, cancel_reason=? WHERE id=?",
                           (actor, reason, order_id))
            self.audit(actor, "order.cancel", "order", order_id, {"before": o["status"], "reason": reason})
            if sale_event_id:
                rev = self.reverse(actor, sale_event_id, reason)
                return self.submit(actor, rev["id"])

    def record_payment(self, actor, order_id, method, amount, reference=None, paid_at=None):
        self.require(actor, "payment.record")
        pid = new_id()
        self.c.execute("INSERT INTO payments(id,order_id,method,amount,reference,paid_at) VALUES (?,?,?,?,?,?)",
                       (pid, order_id, method, float(amount), reference, paid_at or now_iso()))
        self.audit(actor, "payment.record", "payment", pid, {"order": order_id, "amount": amount, "method": method})
        return pid

    def import_bank_rows(self, actor, account, rows):
        """Idempotent import: a row seen before (same account/date/amount/reference) is reported, not re-added."""
        self.require(actor, "bank.import")
        added, dupes = 0, []
        with self.tx():
            for r in rows:
                fp = hashlib.sha256(
                    f"{account}|{r['date']}|{float(r['amount']):.2f}|{(r.get('reference') or '').strip().upper()}".encode()
                ).hexdigest()
                cur = self.c.execute(
                    "INSERT OR IGNORE INTO bank_transactions(id,fingerprint,account,txn_date,amount,reference,imported_at) VALUES (?,?,?,?,?,?,?)",
                    (new_id(), fp, account, r["date"], float(r["amount"]), r.get("reference"), now_iso()))
                if cur.rowcount:
                    added += 1
                else:
                    dupes.append(r)
            self.audit(actor, "bank.import", "bank", account, {"added": added, "duplicates": len(dupes)})
        return {"added": added, "duplicates": dupes}
