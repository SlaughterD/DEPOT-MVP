"""Transparent anomaly rules. Every rule is plain arithmetic over the ledger that an auditor can
reproduce by hand. A rule opens an investigation case with its evidence; it never concludes
that anyone did anything wrong."""
import json
from datetime import datetime, timedelta, timezone

from .service import Ledger, q3


def _parse(ts):
    return datetime.fromisoformat(ts)


# --------------------------------------------------------------------------- 1. deliveries
def unreconciled_deliveries(L: Ledger, actor=None, grace_hours=12):
    """Dispatched to a van on a trip vs. delivered + returned + documented damage (net of reversals)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=grace_hours)).isoformat()
    alerts = []
    dispatches = L.all(
        """SELECT e.* FROM stock_events e JOIN locations t ON t.id=e.to_location_id
           WHERE e.event_type='TRANSFER' AND e.status='POSTED' AND e.trip_ref IS NOT NULL
             AND t.location_type='VAN' AND e.posted_at<=?""", (cutoff,))
    for d in dispatches:
        van = L.location(d["to_location_id"])
        lines = L.all("SELECT product_id, SUM(quantity_base) q FROM stock_event_lines WHERE event_id=? GROUP BY product_id",
                      (d["id"],))
        for ln in lines:
            accounted = -L.one(
                """SELECT COALESCE(SUM(p.quantity_delta),0) s FROM stock_postings p
                   JOIN stock_event_lines l ON l.id=p.event_line_id JOIN stock_events e ON e.id=l.event_id
                   WHERE e.trip_ref=? AND e.id!=? AND p.location_id=? AND p.product_id=? AND e.status='POSTED'""",
                (d["trip_ref"], d["id"], van["id"], ln["product_id"]))["s"]
            gap = q3(ln["q"] - accounted)
            if gap == 0:
                continue
            prod = L.one("SELECT name, avg_cost FROM products WHERE id=?", (ln["product_id"],))
            amount = round(abs(gap) * prod["avg_cost"], 2)
            evidence = {
                "trip_ref": d["trip_ref"], "dispatch_reference": d["reference"], "dispatch_event_id": d["id"],
                "van": van["name"], "custodian_id": van["custodian_id"], "dispatched": q3(ln["q"]),
                "delivered_returned_or_damaged": q3(accounted), "unaccounted": gap,
                "dispatched_by": d["created_by"],
                "trip_events": [e["reference"] for e in L.all(
                    "SELECT reference FROM stock_events WHERE trip_ref=? AND status='POSTED' ORDER BY posted_at",
                    (d["trip_ref"],))],
            }
            inv = L.open_investigation(
                "UNRECONCILED_DELIVERY", amount,
                f"Trip {d['trip_ref']}: {prod['name']} dispatched {q3(ln['q'])}, accounted {q3(accounted)}, gap {gap:+}",
                evidence, source_key=f"trip:{d['trip_ref']}:{ln['product_id']}", actor=actor)
            alerts.append(inv)
    return alerts


# --------------------------------------------------------------------------- 2. adjustments
def unusual_adjustments(L: Ledger, actor=None, window_days=30, value_threshold=50_000.0, count_threshold=3):
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    alerts, per_actor = [], {}
    for ev in L.all("SELECT * FROM stock_events WHERE event_type IN ('ADJUST_IN','ADJUST_OUT') AND status='POSTED' AND posted_at>=?",
                    (since,)):
        full = L.get_event(ev["id"])
        value = L.event_value(full)
        per_actor.setdefault(ev["created_by"], []).append(ev["reference"])
        if value >= value_threshold:
            alerts.append(L.open_investigation(
                "UNUSUAL_ADJUSTMENT", value,
                f"Adjustment {ev['reference']} of value {value:,.0f} exceeds threshold {value_threshold:,.0f}",
                {"event_id": ev["id"], "reference": ev["reference"], "requested_by": ev["created_by"],
                 "reason": ev["reason"], "approvals": L.all("SELECT approver_id, decision, reason FROM approvals WHERE event_id=?", (ev["id"],))},
                source_key=f"adj:{ev['id']}", actor=actor))
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    for who, refs in per_actor.items():
        if len(refs) >= count_threshold:
            alerts.append(L.open_investigation(
                "UNUSUAL_ADJUSTMENT", 0,
                f"{len(refs)} adjustments requested by one user within {window_days} days (threshold {count_threshold})",
                {"requested_by": who, "references": refs, "window_days": window_days},
                source_key=f"adjfreq:{who}:{month}", actor=actor, severity="MEDIUM"))
    return alerts


# --------------------------------------------------------------------------- 3. sales to cash
def sales_to_cash(L: Ledger, period_start, period_end, actor=None, open_cases=True):
    """Classify every order in the period. Late-but-complete payments are TIMING, not mismatches."""
    rows, totals = [], {"expected_cash": 0.0, "collected_by_period_end": 0.0, "timing_receivable": 0.0,
                        "credit_receivable": 0.0, "mismatch": 0.0, "cancelled": 0.0}
    for o in L.all("SELECT * FROM orders WHERE order_date>=? AND order_date<=? ORDER BY order_date, invoice_no",
                   (period_start, period_end)):
        pays = L.all("SELECT amount, paid_at FROM payments WHERE order_id=?", (o["id"],))
        paid_by_end = round(sum(p["amount"] for p in pays if p["paid_at"][:10] <= period_end), 2)
        paid_ever = round(sum(p["amount"] for p in pays), 2)
        row = {"invoice_no": o["invoice_no"], "order_id": o["id"], "terms": o["payment_terms"], "total": o["total"],
               "paid_by_period_end": paid_by_end, "paid_ever": paid_ever}
        if o["status"] == "CANCELLED":
            row["class"] = "CANCELLED" if paid_ever == 0 else "CANCELLED_WITH_PAYMENT"
            totals["cancelled"] += o["total"]
        elif o["payment_terms"] == "CREDIT":
            row["class"] = "CREDIT_RECEIVABLE"
            totals["credit_receivable"] += round(o["total"] - paid_by_end, 2)
        else:
            totals["expected_cash"] += o["total"]
            totals["collected_by_period_end"] += min(paid_by_end, o["total"])
            if paid_by_end == o["total"]:
                row["class"] = "RECONCILED"
            elif paid_ever == o["total"]:
                row["class"] = "TIMING"
                totals["timing_receivable"] += round(o["total"] - paid_by_end, 2)
            elif paid_ever > o["total"]:
                row["class"] = "OVERPAID"
            else:
                row["class"] = "SHORT_PAID"
        if row["class"] in ("OVERPAID", "SHORT_PAID", "CANCELLED_WITH_PAYMENT"):
            diff = round(abs(paid_ever - (o["total"] if row["class"] != "CANCELLED_WITH_PAYMENT" else 0)), 2)
            totals["mismatch"] += diff
            row["difference"] = diff
            if open_cases:
                L.open_investigation(
                    "SALES_TO_CASH", diff,
                    f"Invoice {o['invoice_no']}: total {o['total']:,.2f}, collected {paid_ever:,.2f} ({row['class']})",
                    {"invoice_no": o["invoice_no"], "order_id": o["id"], "total": o["total"], "paid_ever": paid_ever,
                     "class": row["class"], "created_by": o["created_by"], "period": [period_start, period_end]},
                    source_key=f"cash:{o['id']}:{row['class']}", actor=actor)
        rows.append(row)
    return {"period": [period_start, period_end], "rows": rows, "totals": {k: round(v, 2) for k, v in totals.items()}}


# --------------------------------------------------------------------------- 4. repeated patterns
def repeated_anomalies(L: Ledger, actor=None, min_cases=3, min_activity=5, rate_threshold=0.25, window_days=90):
    """Flag locations/custodians whose unresolved cases are frequent *relative to their activity*.
    A big, busy location with a few cases is not treated like a small one with the same count."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    cases = L.all("SELECT * FROM investigations WHERE status!='CLOSED' AND variance_type!='REPEATED_ANOMALY'")
    by_loc = {}
    for c in cases:
        ev = json.loads(c["evidence"] or "{}")
        loc = ev.get("location_id")
        if not loc and ev.get("van"):
            row = L.one("SELECT id FROM locations WHERE name=?", (ev["van"],))
            loc = row["id"] if row else None
        if loc:
            by_loc.setdefault(loc, []).append(c["id"])
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    alerts = []
    for loc, ids in by_loc.items():
        activity = L.one(
            """SELECT COUNT(DISTINCT e.id) n FROM stock_postings p JOIN stock_event_lines l ON l.id=p.event_line_id
               JOIN stock_events e ON e.id=l.event_id WHERE p.location_id=? AND e.posted_at>=?""", (loc, since))["n"]
        rate = len(ids) / activity if activity else 0
        if len(ids) >= min_cases and activity >= min_activity and rate >= rate_threshold:
            name = L.location(loc)["name"]
            alerts.append(L.open_investigation(
                "REPEATED_ANOMALY", 0,
                f"{name}: {len(ids)} unresolved cases across {activity} stock movements ({rate:.0%}). "
                "Review recommended; this is a pattern, not a finding.",
                {"location_id": loc, "case_ids": ids, "movements": activity, "rate": round(rate, 3),
                 "custodian_id": L.location(loc)["custodian_id"]},
                source_key=f"repeat:{loc}:{month}", actor=actor, severity="MEDIUM"))
    return alerts


def run_all(L: Ledger, actor=None, period=None, grace_hours=12):
    out = {
        "unreconciled_deliveries": unreconciled_deliveries(L, actor, grace_hours=grace_hours),
        "unusual_adjustments": unusual_adjustments(L, actor),
    }
    if period:
        out["sales_to_cash"] = sales_to_cash(L, period[0], period[1], actor)
    out["repeated_anomalies"] = repeated_anomalies(L, actor)
    return out
