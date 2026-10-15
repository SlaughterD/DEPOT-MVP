"""Demo data: a small depot with a believable week of activity, including a few
deliberate discrepancies so every dashboard panel has something to show.
Demo PIN for every user = their employee number's last 4 digits (see README)."""
from datetime import datetime, timedelta, timezone

from . import rules
from .db import connect, new_id
from .service import Ledger


def build(path=":memory:"):
    conn = connect(path)
    if conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]:
        org = conn.execute("SELECT id FROM organizations").fetchone()[0]
        return Ledger(conn, org)
    org = new_id()
    conn.execute("INSERT INTO organizations(id,name) VALUES (?,?)", (org, "Demo Beverage Depot"))
    L = Ledger(conn, org)
    U = {}
    for no, name, role in [("1001", "Philip (Storekeeper)", "storekeeper"), ("1002", "Carol (Shop Cashier)", "shop_cashier"),
                           ("1003", "Daniel (Driver)", "driver"), ("1004", "Mercy (Depot Manager)", "depot_manager"),
                           ("1005", "Moses (Depot Manager)", "depot_manager"), ("1006", "Anne (Accountant)", "accountant"),
                           ("1007", "Ali (Internal Auditor)", "internal_auditor"), ("1008", "CEO", "ceo"),
                           ("1009", "Board Member", "board"), ("1010", "Sysadmin", "system_admin")]:
        U[role if role not in U else role + "2"] = L.create_user(no, name, [role], pin=no[-4:] if len(no) >= 4 else no)
    store = L.create_location("Main Store", "STORE", U["storekeeper"])
    shop = L.create_location("Shop Counter", "SHOP", U["shop_cashier"])
    van = L.create_location("Van KDA 123X", "VAN", U["driver"])
    dmg = L.create_location("Damaged Goods", "DAMAGED")
    cola = L.create_product("COLA-500", "Cola 500ml (case of 24)", avg_cost=1000)
    water = L.create_product("WATER-1L", "Water 1L (case of 12)", avg_cost=500)
    juice = L.create_product("JUICE-1L", "Juice 1L (case of 12)", avg_cost=1500)

    def mv(actor, t, ref, frm, to, lines, **kw):
        ev = L.create_event(actor, t, ref, [{"product_id": p, "quantity": q} for p, q in lines],
                            from_location=frm, to_location=to, **kw)
        return L.submit(actor, ev["id"])

    k, c, d, m1, m2 = U["storekeeper"], U["shop_cashier"], U["driver"], U["depot_manager"], U["depot_manager2"]
    grn = L.receive_stock(k, "GRN-1001", store, [{"product_id": cola, "quantity": 400, "note_qty": 400, "unit_cost": 1000},
                                          {"product_id": water, "quantity": 300, "note_qty": 300, "unit_cost": 500},
                                          {"product_id": juice, "quantity": 120, "note_qty": 130, "unit_cost": 1500}],
                    supplier_note_ref="SN-8841")
    if grn["status"] == "PENDING_APPROVAL":  # large receipt: a second person confirms it
        L.decide(m1, grn["id"], "APPROVED")
    mv(k, "TRANSFER", "SHOP-LOAD-1", store, shop, [(cola, 60), (water, 40)])
    for i in range(3):
        mv(c, "SALE", f"POS-{i+1}", shop, None, [(cola, 8), (water, 5)])
    mv(k, "TRANSFER", "TRIP-A-LOAD", store, van, [(cola, 100), (juice, 20)], trip_ref="TRIP-A")
    mv(d, "SALE", "TRIP-A-DEL1", van, None, [(cola, 60), (juice, 12)], trip_ref="TRIP-A")
    mv(d, "RETURN", "TRIP-A-RET", van, store, [(cola, 30), (juice, 6)], trip_ref="TRIP-A")  # 10 cola + 2 juice unaccounted
    o = L.create_order(c, "INV-0001", (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
                       [{"product_id": cola, "quantity": 24, "unit_price": 1400}])
    L.record_payment(c, o, "MPESA", 33_000, "MPX1")  # short by 600
    # a count with a variance, and a pending adjustment waiting for a second person
    cid = L.open_count(k, store)
    for pid, qty in L.balances(store).items():
        L.record_count(k, cid, pid, qty - (4 if pid == water else 0))
    L.finalize_count(m1, cid)
    adj = L.create_event(m1, "ADJUST_OUT", "ADJ-DEMO-1", [{"product_id": juice, "quantity": 3}],
                         from_location=store, reason="Cartons crushed in store, photo attached", custodian_id=k)
    L.submit(m1, adj["id"])
    with L.tx():
        today = datetime.now(timezone.utc).date()
        rules.run_all(L, U["accountant"], period=((today - timedelta(days=7)).isoformat(), today.isoformat()), grace_hours=0)
    return L
