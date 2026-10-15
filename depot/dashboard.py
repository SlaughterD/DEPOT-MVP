"""Executive and board dashboard data. Board members get aggregates only: no names, no evidence."""
import json
from datetime import datetime, timezone

from .service import Ledger, PermissionDenied, q3

VARIANCE_TYPES = ("STOCK_DISCREPANCY", "UNRECONCILED_DELIVERY", "SALES_TO_CASH", "SUPPLIER_SHORTAGE")


def _age_days(ts):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 86400


def inventory_value(L: Ledger):
    rows = []
    for loc in L.all("SELECT * FROM locations WHERE active=1 ORDER BY name"):
        value, items = 0.0, 0
        for pid, qty in L.balances(loc["id"]).items():
            value += qty * L.one("SELECT avg_cost FROM products WHERE id=?", (pid,))["avg_cost"]
            items += 1
        rows.append({"location_id": loc["id"], "name": loc["name"], "type": loc["location_type"],
                     "value": round(value, 2), "products": items})
    return rows


def count_accuracy(L: Ledger):
    """Share of counted lines (latest finalized count per location) that matched exactly."""
    total = ok = 0
    for loc in L.all("SELECT DISTINCT location_id FROM stock_counts WHERE status='FINALIZED'"):
        latest = L.one("SELECT id FROM stock_counts WHERE location_id=? AND status='FINALIZED' ORDER BY finalized_at DESC LIMIT 1",
                       (loc["location_id"],))
        for l in L.count_lines(latest["id"]):
            total += 1
            ok += 1 if l["variance"] == 0 else 0
    return None if total == 0 else round(100.0 * ok / total, 1)


def dashboard(L: Ledger, user_id):
    perms = L.perms_of(user_id)
    full = "dashboard.full" in perms
    if not full and "dashboard.summary" not in perms:
        raise PermissionDenied("no dashboard access")
    locs = inventory_value(L)
    open_cases = L.all("SELECT * FROM investigations WHERE status!='CLOSED' ORDER BY created_at")
    by_sev = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    age = {"<7d": 0, "7-30d": 0, ">30d": 0}
    for c in open_cases:
        by_sev[c["severity"]] += 1
        d = _age_days(c["created_at"])
        age["<7d" if d < 7 else "7-30d" if d <= 30 else ">30d"] += 1
    unresolved = round(sum(c["amount"] for c in open_cases if c["variance_type"] in VARIANCE_TYPES), 2)
    closed = L.one("SELECT COUNT(*) n FROM investigations WHERE status='CLOSED'")["n"]
    out = {
        "view": "full" if full else "summary",
        "kpis": {
            "inventory_value": round(sum(l["value"] for l in locs), 2),
            "unresolved_variance": unresolved,
            "count_accuracy_pct": count_accuracy(L),
            "open_investigations": len(open_cases),
        },
        "open_by_severity": by_sev, "open_by_age": age, "closed_investigations": closed,
        "pending_approvals": L.one("SELECT COUNT(*) n FROM stock_events WHERE status='PENDING_APPROVAL'")["n"],
        "currency": "KSh",
    }
    if full:
        out["locations"] = locs
        out["investigations"] = [
            {"id": c["id"], "type": c["variance_type"], "severity": c["severity"], "amount": c["amount"],
             "status": c["status"], "summary": c["summary"], "age_days": round(_age_days(c["created_at"]), 1),
             "assigned_to": c["assigned_to"], "evidence": json.loads(c["evidence"] or "{}")}
            for c in reversed(open_cases)]
    else:  # board: exposure by type and trend only, no people, no free text
        by_type = {}
        for c in open_cases:
            by_type[c["variance_type"]] = round(by_type.get(c["variance_type"], 0) + c["amount"], 2)
        out["exposure_by_type"] = by_type
        out["locations"] = [{"type": l["type"], "value": l["value"]} for l in locs]
    return out
