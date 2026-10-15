"""Zero-dependency JSON API + static web app (Python standard library only)."""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dashboard, rules
from .service import InsufficientStock, Ledger, LedgerError, PermissionDenied

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
LOCK = threading.Lock()  # one writer at a time; SQLite connection is shared


class Api:
    def __init__(self, L: Ledger):
        self.L = L

    # ---- helpers
    def meta(self):
        L = self.L
        return {
            "locations": L.all("SELECT id,name,location_type,custodian_id FROM locations WHERE active=1 ORDER BY name"),
            "products": L.all("SELECT id,sku,name,base_uom,avg_cost FROM products ORDER BY name"),
            "users": L.all("SELECT id,name FROM users WHERE active=1 ORDER BY name"),
        }

    def events(self, uid, status=None):
        L = self.L
        sql = "SELECT * FROM stock_events"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        out = []
        for e in L.all(sql + " ORDER BY created_at DESC LIMIT 100", args):
            full = L.get_event(e["id"])
            names = {p["id"]: p["name"] for p in L.all("SELECT id,name FROM products")}
            full["lines"] = [{**l, "product": names.get(l["product_id"])} for l in full["lines"]]
            full["value"] = L.event_value(full)
            full["created_by_name"] = (L.one("SELECT name FROM users WHERE id=?", (e["created_by"],)) or {}).get("name")
            out.append(full)
        return out

    def balances(self):
        L = self.L
        names = {p["id"]: p for p in L.all("SELECT * FROM products")}
        out = []
        for loc in L.all("SELECT * FROM locations WHERE active=1 ORDER BY name"):
            items = [{"product_id": pid, "product": names[pid]["name"], "qty": q,
                      "value": round(q * names[pid]["avg_cost"], 2)} for pid, q in L.balances(loc["id"]).items()]
            out.append({**loc, "items": items})
        return out

    # ---- routing
    def handle(self, method, path, query, body, uid):
        L = self.L
        if method == "GET":
            if path == "/api/me":
                return {"user": L.get_user(uid), "roles": sorted(L.roles_of(uid)), "perms": sorted(L.perms_of(uid))}
            if path == "/api/meta":
                return self.meta()
            if path == "/api/balances":
                return self.balances()
            if path == "/api/events":
                return self.events(uid, (query.get("status") or [None])[0])
            if path == "/api/dashboard":
                return dashboard.dashboard(L, uid)
            if path == "/api/investigations":
                L.require(uid, "investigation.manage")
                return [{**i, "evidence": json.loads(i["evidence"])} for i in
                        L.all("SELECT * FROM investigations ORDER BY created_at DESC")]
            if path == "/api/audit":
                L.require(uid, "audit.read")
                return L.all("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 200")
            if path == "/api/orders":
                return L.all("SELECT * FROM orders ORDER BY order_date DESC, invoice_no DESC LIMIT 100")
            if path == "/api/counts":
                return L.all("SELECT * FROM stock_counts ORDER BY cutoff_at DESC LIMIT 30")
            m = re.fullmatch(r"/api/counts/(\w+)", path)
            if m:
                names = {p["id"]: p["name"] for p in L.all("SELECT id,name FROM products")}
                return {"count": L._count(m.group(1), False),
                        "lines": [{**l, "product": names.get(l["product_id"])} for l in L.count_lines(m.group(1))]}
        if method == "POST":
            if path == "/api/events":
                ev = L.create_event(uid, body["event_type"], body["reference"], body["lines"],
                                    from_location=body.get("from_location"), to_location=body.get("to_location"),
                                    trip_ref=body.get("trip_ref") or None, reason=body.get("reason"),
                                    event_id=body.get("event_id"), custodian_id=body.get("custodian_id"))
                return L.submit(uid, ev["id"]) if body.get("submit", True) else ev
            if path == "/api/receive":
                return L.receive_stock(uid, body["reference"], body["to_location"], body["lines"],
                                       supplier_note_ref=body.get("supplier_note_ref"))
            m = re.fullmatch(r"/api/events/(\w+)/(decide|reverse|submit)", path)
            if m:
                eid, act = m.groups()
                if act == "decide":
                    return L.decide(uid, eid, body["decision"], body.get("reason"))
                if act == "reverse":
                    rev = L.reverse(uid, eid, body.get("reason"))
                    return L.submit(uid, rev["id"])
                return L.submit(uid, eid)
            if path == "/api/counts":
                return {"id": L.open_count(uid, body["location_id"])}
            m = re.fullmatch(r"/api/counts/(\w+)/(record|recount|finalize|adjust)", path)
            if m:
                cid, act = m.groups()
                if act == "record":
                    return L.record_count(uid, cid, body["product_id"], body["qty"]) or {"ok": True}
                if act == "recount":
                    return L.record_recount(uid, cid, body["product_id"], body["qty"]) or {"ok": True}
                if act == "finalize":
                    return L.finalize_count(uid, cid, float(body.get("tolerance", 0)))
                return L.propose_count_adjustment(uid, cid, body["product_id"], body["reason"])
            if path == "/api/orders":
                return {"id": L.create_order(uid, body["invoice_no"], body["order_date"], body["lines"],
                                             terms=body.get("terms", "CASH"))}
            m = re.fullmatch(r"/api/orders/(\w+)/(cancel|payments)", path)
            if m:
                oid, act = m.groups()
                if act == "cancel":
                    return L.cancel_order(uid, oid, body.get("reason"), body.get("sale_event_id")) or {"ok": True}
                return {"id": L.record_payment(uid, oid, body["method"], body["amount"], body.get("reference"),
                                               body.get("paid_at"))}
            if path == "/api/bank/import":
                return L.import_bank_rows(uid, body["account"], body["rows"])
            if path == "/api/rules/run":
                L.require(uid, "rules.run")
                period = (body["period_start"], body["period_end"]) if body.get("period_start") else None
                with L.tx():
                    res = rules.run_all(L, uid, period=period, grace_hours=float(body.get("grace_hours", 12)))
                return {k: (v if k == "sales_to_cash" else len(v)) for k, v in res.items()}
            m = re.fullmatch(r"/api/investigations/(\w+)/(assign|escalate|close)", path)
            if m:
                iid, act = m.groups()
                if act == "assign":
                    return L.assign_investigation(uid, iid, body["assignee"]) or {"ok": True}
                if act == "escalate":
                    return L.escalate_investigation(uid, iid, body.get("note", "")) or {"ok": True}
                return L.close_investigation(uid, iid, body.get("resolution"), body.get("explanation_supported", False)) or {"ok": True}
        raise LedgerError(f"no such endpoint: {method} {path}")


def make_handler(api: Api):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path):
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, name))
            if not full.startswith(os.path.normpath(STATIC)) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript", "css": "text/css",
                     "json": "application/json", "svg": "image/svg+xml"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
            self._send(200, open(full, "rb").read(), ctype)

        def _api(self, method):
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                return self._static(u.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                with LOCK:
                    if u.path == "/api/login" and method == "POST":
                        token, uid = api.L.login(body.get("employee_no", ""), body.get("pin", ""))
                        return self._send(200, {"token": token})
                    tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                    uid = api.L.user_for_token(tok)
                    if not uid:
                        return self._send(401, {"error": "sign in required"})
                    res = api.handle(method, u.path, parse_qs(u.query), body, uid)
                return self._send(200, res if res is not None else {"ok": True})
            except InsufficientStock as e:
                self._send(409, {"error": str(e), "kind": "insufficient_stock"})
            except PermissionDenied as e:
                self._send(403, {"error": str(e), "kind": "denied"})
            except (LedgerError, KeyError, ValueError, TypeError) as e:
                self._send(400, {"error": f"{type(e).__name__}: {e}" if not isinstance(e, LedgerError) else str(e)})
            except Exception as e:  # database rule violations etc.
                self._send(400, {"error": str(e)})

        def do_GET(self):
            self._api("GET")

        def do_POST(self):
            self._api("POST")

    return H


def serve(L: Ledger, host="127.0.0.1", port=8000):
    srv = ThreadingHTTPServer((host, port), make_handler(Api(L)))
    print(f"Depot Ledger running on http://{host}:{port}")
    srv.serve_forever()
