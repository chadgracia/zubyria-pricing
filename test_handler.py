"""Handler acceptance tests — all 23 (10 prior + 6 tape-chart + 7 new)."""
import sys
sys.path.insert(0, ".")
import lambda_function
from datetime import date
from pricing_engine import FALLBACK_RULES

ADMIN_SECRET = lambda_function.ADMIN_SECRET
_passed = 0
_failed = 0

# --- abort guard -----------------------------------------------------------
# This file is a flat script: an exception part-way through skips every later
# assertion. Python does exit non-zero on an uncaught exception, but a silent
# partial run still LOOKS like a pass in logs, so the run is only ever trusted
# when the final "SUITE COMPLETE" line is present. The deploy gate greps for it.
import atexit
import os as _os

_suite_completed = False


def _abort_guard():
    if not _suite_completed:
        print("\nSUITE ABORTED before completion — "
              "assertions after the failure point never ran.")
        sys.stdout.flush()
        _os._exit(1)


atexit.register(_abort_guard)


class FakeStore:
    def __init__(self, blocks=None):
        self._blocks = [dict(b) for b in (blocks or [])]
        self._next = 1000

    def list_blocks(self):
        return [dict(b) for b in self._blocks]

    def availability(self, checkin, checkout, exclude_id=None):
        result = {}
        ci = checkin if isinstance(checkin, date) else date.fromisoformat(str(checkin))
        co = checkout if isinstance(checkout, date) else date.fromisoformat(str(checkout))
        for key in ["tseglina", "modryna", "zharyna"]:
            active = [
                b for b in self._blocks
                if b.get("status") == "active"
                and (not exclude_id or b.get("sk") != exclude_id)
                and key in b.get("houses", [])
                and date.fromisoformat(b["checkin"]) < co
                and date.fromisoformat(b["checkout"]) > ci
            ]
            if active:
                result[key] = {"available": False, "blocked_by": active[0].get("label", "?")}
            else:
                result[key] = {"available": True, "blocked_by": None}
        return result

    def add_block(self, houses, checkin, checkout, label, created_by="admin",
                  exclude_id=None, snapshot=None, quote_params=None, btype=None,
                  notes=None, deposit=None, override_subtotal=None, price_note=None,
                  payments=None):
        ci_d = checkin if isinstance(checkin, date) else date.fromisoformat(str(checkin))
        co_d = checkout if isinstance(checkout, date) else date.fromisoformat(str(checkout))
        conflicts = []
        for b in self._blocks:
            if b.get("status") != "active":
                continue
            if exclude_id and b.get("sk") == exclude_id:
                continue
            bc = date.fromisoformat(b["checkin"])
            bo = date.fromisoformat(b["checkout"])
            if ci_d >= bo or co_d <= bc:
                continue
            for h in houses:
                if h in b.get("houses", []) and b.get("label", "?") not in conflicts:
                    conflicts.append(b.get("label", "?"))
        if conflicts:
            return {"ok": False, "conflicts": conflicts}
        sk = f"block#{self._next}"
        self._next += 1
        item = {
            "pk": "blocks", "sk": sk,
            "houses": houses,
            "checkin": checkin.isoformat() if hasattr(checkin, "isoformat") else str(checkin),
            "checkout": checkout.isoformat() if hasattr(checkout, "isoformat") else str(checkout),
            "label": label,
            "created_by": created_by,
            "status": "active",
        }
        if snapshot:
            item["snapshot"] = snapshot
        if quote_params:
            item["quote_params"] = quote_params
        if btype:
            item["btype"] = btype
        if notes:
            item["notes"] = notes
        if deposit:
            item["deposit"] = deposit
        if payments is not None:
            item["payments"] = payments
        if override_subtotal:
            item["override_subtotal"] = override_subtotal
        if price_note:
            item["price_note"] = price_note
        self._blocks.append(item)
        return {"ok": True}

    def set_payments(self, block_id, payments):
        for b in self._blocks:
            if b.get("sk") == block_id:
                b["payments"] = payments

    def set_price_override(self, block_id, snapshot, override_subtotal, price_note):
        for b in self._blocks:
            if b.get("sk") == block_id:
                b["snapshot"] = snapshot
                if override_subtotal is None:
                    b.pop("override_subtotal", None)
                    b.pop("price_note", None)
                else:
                    b["override_subtotal"] = override_subtotal
                    b["price_note"] = price_note or ""

    def cancel_block(self, block_id):
        for b in self._blocks:
            if b.get("sk") == block_id:
                b["status"] = "cancelled"


def ev(path="/", qs=None, cookies=None):
    return {"rawPath": path, "queryStringParameters": qs or {}, "cookies": cookies or []}

def admin_cookie():
    return [f"zadmin={ADMIN_SECRET}"]

def t(name, cond):
    global _passed, _failed
    if cond:
        print(f"  PASS  {name}")
        _passed += 1
    else:
        print(f"  FAIL  {name}")
        _failed += 1


# ── Prior tests 1–10 ────────────────────────────────────────────────────────

print("=== Prior tests ===")

# 1 – Public page renders the calculator form
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/"), None)
t("1: public page 200", r["statusCode"] == 200)
t("1: public page has form", "<form" in r["body"])

# 2 – Non-admin /admin is denied
r = lambda_function.lambda_handler(ev("/admin"), None)
t("2: non-admin denied", "Access denied" in r["body"])

# 3 – Admin with key param can access /admin
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET}), None)
t("3: admin with key gets admin page", "Admin" in r["body"] and "Access denied" not in r["body"])

# 4 – /availability.json without params returns 400
r = lambda_function.lambda_handler(ev("/availability.json"), None)
t("4: availability.json needs params", r["statusCode"] == 400)

# 5 – add_block works
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-09-10 to 2026-09-15",
    "h_tseglina": "on", "label": "Test Guest",
}), None)
t("5: add_block success", "Block added" in r["body"])

# 6 – cancel_block works
_store6 = FakeStore([{
    "pk": "blocks", "sk": "block#c1",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-15",
    "label": "Cancel Me", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store6
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "cancel_block", "block_id": "block#c1",
}), None)
t("6: cancel_block success msg", "cancelled" in r["body"].lower())
t("6: block marked cancelled", _store6._blocks[0]["status"] == "cancelled")

# 7 – Admin PRICE tab shows Anya bonus and tab nav
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "price",
    "dates": "2026-12-30 to 2027-01-02",
    "h_tseglina": "on", "g_tseglina": "5",
    "h_zharyna": "on", "g_zharyna": "5",
    "jacuzzi": "2", "btype": "airbnb",
}), None)
t("7: admin price tab shows Anya", "Anya" in r["body"])
t("7: admin price tab has BLOCK|PRICE nav", "Block" in r["body"] and "Price" in r["body"])

# 8 – Public page form action is "/"
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/"), None)
t('8: public form action="/"', 'action="/"' in r["body"])

# 9 – Admin form action is "/admin"
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "price"}), None)
t('9: admin form action="/admin"', 'action="/admin"' in r["body"])

# 10 – Public quote result has no Anya
r = lambda_function.lambda_handler(ev("/", {
    "dates": "2026-12-30 to 2027-01-02",
    "h_tseglina": "on", "g_tseglina": "5",
    "jacuzzi": "0", "btype": "cash",
}), None)
t("10: public page no Anya", "Anya" not in r["body"])

# ── Tape-chart tests 1–6 ────────────────────────────────────────────────────

print()
print("=== Tape-chart tests ===")

# Tape 1 – Admin BLOCK tab contains all three house names and a bar for the seeded block
_store_t1 = FakeStore([{
    "pk": "blocks", "sk": "block#tape1",
    "houses": ["tseglina", "modryna", "zharyna"],
    "checkin": "2026-08-10", "checkout": "2026-08-15",
    "label": "Family Vacation", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_t1
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body = r["body"]
t("T1: all three house names present", "Tseglina" in body and "Modryna" in body and "Zharyna" in body)
t("T1: block label in body", "Family Vacation" in body)
t("T1: tape-bar class present", "tape-bar" in body)

# Tape 2 – Block 09-10→09-13 occupies 3 day-cells (colspan=3), checkout day NOT included
_store_t2 = FakeStore([{
    "pk": "blocks", "sk": "block#t2",
    "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Three Night", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_t2
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-09"}), None)
body = r["body"]
t("T2: data-checkin present", 'data-checkin="2026-09-10"' in body)
t("T2: data-checkout present", 'data-checkout="2026-09-13"' in body)
t("T2: bar has half-start (mid check-in cell)", 'tape-half-start' in body)
t("T2: bar has half-end (mid check-out cell)", 'tape-half-end' in body)
t("T2: bar slanted at both ends", 'tape-slant-both' in body)
t("T2: 3-night bar spans 3 cells (102px)", 'width:102px' in body)
t("T2: bar starts at mid-cell offset (17px)", 'left:17px' in body)
t("T2: three occupied night cells", body.count('tape-occ"') == 3)
t("T2: checkout day not in block bar data", 'data-checkin="2026-09-13"' not in body)

# Tape 3 – Back-to-back blocks both render without overlap
_store_t3 = FakeStore([
    {
        "pk": "blocks", "sk": "block#b2b1",
        "houses": ["tseglina"],
        "checkin": "2026-09-10", "checkout": "2026-09-13",
        "label": "First Guest", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#b2b2",
        "houses": ["tseglina"],
        "checkin": "2026-09-13", "checkout": "2026-09-16",
        "label": "Second Guest", "created_by": "admin", "status": "active",
    },
])
lambda_function._store = _store_t3
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-09"}), None)
body = r["body"]
t("T3: first block label", "First Guest" in body)
t("T3: second block label", "Second Guest" in body)
t("T3: block1 data-checkin", 'data-checkin="2026-09-10"' in body)
t("T3: block2 data-checkin", 'data-checkin="2026-09-13"' in body)
t("T3: no overlap (block1 data-checkout visible)", 'data-checkout="2026-09-13"' in body)

# Tape 4 – month=2026-12 window renders New Year holiday marker
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-12"}), None)
body = r["body"]
t("T4: New Year holiday marker in Dec window", "New Year" in body)

# Tape 5 – details flow: bar link shows Details panel; cancel two-step; after cancel bar gone
_store_t5 = FakeStore([{
    "pk": "blocks", "sk": "block#cc1",
    "houses": ["modryna"],
    "checkin": "2026-09-05", "checkout": "2026-09-08",
    "label": "Confirm Cancel Test", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_t5
# Step A: details param shows Details panel with label and controls
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#cc1", "month": "2026-09",
}), None)
body = r["body"]
t("T5: details panel shows label", "Confirm Cancel Test" in body)
t("T5: details panel shows 'Details' title", "Details" in body)
t("T5: details panel shows Edit control", "Edit" in body)
# Step B: after actual cancel, bar gone
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "cancel_block",
    "block_id": "block#cc1", "month": "2026-09",
}), None)
body = r["body"]
t("T5: after cancel label gone from chart", "Confirm Cancel Test" not in body)

# Tape 6 – Non-admin still sees nothing of /admin
lambda_function._store = FakeStore([{
    "pk": "blocks", "sk": "block#secret",
    "houses": ["tseglina"], "checkin": "2026-09-01", "checkout": "2026-09-05",
    "label": "Secret Block", "created_by": "admin", "status": "active",
}])
r = lambda_function.lambda_handler(ev("/admin"), None)
body = r["body"]
t("T6: non-admin denied", "Access denied" in body)
t("T6: non-admin sees no block labels", "Secret Block" not in body)
t("T6: non-admin sees no tape chart", 'data-house=' not in body)

# ── Acceptance tests 1–7 (new features) ────────────────────────────────────

print()
print("=== Acceptance tests (new features) ===")

# AT1 – Edit block: no self-conflict; edit into other block rejects + names it
_store_at1 = FakeStore([{
    "pk": "blocks", "sk": "block#A",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Original Guest", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_at1
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#A",
    "dates": "2026-09-11 to 2026-09-14",
    "h_tseglina": "on", "label": "Original Guest",
}), None)
t("AT1: edit success (no self-conflict)", "Block updated" in r["body"] or "Block added" in r["body"])
t("AT1: original block cancelled", _store_at1._blocks[0]["status"] == "cancelled")
t("AT1: new active block for new dates",
  any(b["status"] == "active" and b.get("checkin") == "2026-09-11" for b in _store_at1._blocks))

_store_at1b = FakeStore([
    {
        "pk": "blocks", "sk": "block#A",
        "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
        "label": "Block A", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#B",
        "houses": ["tseglina"], "checkin": "2026-09-12", "checkout": "2026-09-15",
        "label": "Block B", "created_by": "admin", "status": "active",
    },
])
lambda_function._store = _store_at1b
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#A",
    "dates": "2026-09-11 to 2026-09-14",
    "h_tseglina": "on", "label": "Block A",
}), None)
t("AT1: conflict names Block B", "Block B" in r["body"])
t("AT1: Block A unchanged on conflict", _store_at1b._blocks[0]["status"] == "active")

# AT2 – Snapshot: block via price flow with btype stores bonus; plain block has no snapshot
_store_at2 = FakeStore()
lambda_function._store = _store_at2
lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-08-10 to 2026-08-13",
    "h_tseglina": "on", "label": "Priced Guest",
    "btype": "airbnb", "g_tseglina": "4", "jacuzzi": "0", "pets": "0",
}), None)
priced_block = _store_at2._blocks[0] if _store_at2._blocks else {}
t("AT2: snapshot stored on priced block", "snapshot" in priced_block)
t("AT2: bonus in snapshot > 0", (priced_block.get("snapshot") or {}).get("bonus", 0) > 0)
t("AT2: btype stored", priced_block.get("btype") == "airbnb")

_store_at2b = FakeStore()
lambda_function._store = _store_at2b
lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-08-14 to 2026-08-17",
    "h_tseglina": "on", "label": "Maintenance",
}), None)
plain_block = _store_at2b._blocks[0] if _store_at2b._blocks else {}
t("AT2: no snapshot on plain block", "snapshot" not in plain_block)

# AT3 – Re-price on edit: snapshot reflects new dates (holiday night raises bonus)
_store_at3 = FakeStore([{
    "pk": "blocks", "sk": "block#rep",
    "houses": ["tseglina"], "checkin": "2026-08-10", "checkout": "2026-08-13",
    "label": "Reprice Test", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_at3
lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#rep",
    "dates": "2026-08-23 to 2026-08-26",   # includes Independence Day Aug 24
    "h_tseglina": "on", "label": "Reprice Test",
    "btype": "cash", "g_tseglina": "2",
}), None)
t("AT3: original cancelled on edit", _store_at3._blocks[0]["status"] == "cancelled")
new_b3 = next((b for b in _store_at3._blocks if b.get("status") == "active"), None)
t("AT3: new active block has snapshot", new_b3 is not None and "snapshot" in (new_b3 or {}))
# Aug 23 = Sun (before holiday Mon Aug 24): weekend $200; Aug 24 = IndepDay x2 on weekday = $200; Aug 25 = weekday $100
# rental = 500, cleaning = 100, gross_profit = 500 (cash), bonus = 100.0
t("AT3: snapshot bonus reflects holiday night", (new_b3 or {}).get("snapshot", {}).get("bonus") == 100.0)

# AT4 – BONUS tab: priced blocks show with total; unpriced listed separately; cancelled absent
_store_at4 = FakeStore([
    {
        "pk": "blocks", "sk": "block#bon1",
        "houses": ["tseglina"], "checkin": "2026-08-05", "checkout": "2026-08-08",
        "label": "Revenue Block", "created_by": "admin", "status": "active",
        "snapshot": {"subtotal": 600.0, "gross_profit": 500.0, "bonus": 100.0},
        "btype": "cash",
    },
    {
        "pk": "blocks", "sk": "block#bon2",
        "houses": ["modryna"], "checkin": "2026-08-12", "checkout": "2026-08-14",
        "label": "Maintenance Block", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#bon3",
        "houses": ["zharyna"], "checkin": "2026-08-20", "checkout": "2026-08-22",
        "label": "Cancelled Revenue", "created_by": "admin", "status": "cancelled",
        "snapshot": {"subtotal": 300.0, "gross_profit": 250.0, "bonus": 50.0},
    },
    {
        "pk": "blocks", "sk": "block#bon4",
        "houses": ["tseglina"], "checkin": "2026-09-01", "checkout": "2026-09-03",
        "label": "Different Month Block", "created_by": "admin", "status": "active",
        "snapshot": {"subtotal": 300.0, "gross_profit": 250.0, "bonus": 50.0},
    },
])
# Cash basis: give each block a payment so it lands in a quarter.
for _b, _pd in (("block#bon1", "2026-08-05"), ("block#bon2", "2026-08-12"),
                ("block#bon3", "2026-08-20"), ("block#bon4", "2026-11-01")):
    for _blk in _store_at4._blocks:
        if _blk["sk"] == _b:
            _amt = (_blk.get("snapshot") or {}).get("subtotal", 120.0)
            _blk["payments"] = [{"id": _b, "date": _pd, "amount": _amt, "note": ""}]
lambda_function._store = _store_at4
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "3", "year": "2026"}), None)
body = r["body"]
t("AT4: BONUS tab renders 200", r["statusCode"] == 200)
t("AT4: revenue block shown", "Revenue Block" in body)
t("AT4: bonus due 100.00 for the quarter", "100.00" in body)
t("AT4: unpriced block listed", "Maintenance Block" in body)
t("AT4: unpriced flagged as not computable", "bonus not computable" in body)
t("AT4: cancelled block's payment still counted", "Cancelled Revenue" in body)
t("AT4: cancelled block tagged as such", "cancelled</span>" in body)
t("AT4: out-of-quarter block absent", "Different Month Block" not in body)
t("AT4: Q4 shows only the out-of-quarter block",
  "Different Month Block" in lambda_function.lambda_handler(ev("/admin", {
      "key": ADMIN_SECRET, "tab": "bonus", "q": "4", "year": "2026"}), None)["body"])

# AT5 – Events: 500 + 10x30 = 800 added to rental_price; event_min_days enforcement
from pricing_engine import quote as pe_quote
_q_ev = pe_quote(
    date(2026, 8, 10), date(2026, 8, 12),   # 2 weekday nights (Mon-Tue), no holidays
    {"tseglina": 2},
    event=True, event_guests=10,
    booking_type="cash", rules=FALLBACK_RULES,
)
# nightly = 2 * 100 = 200; event = 500 + 10*30 = 800; rental_price = 1000
t("AT5a: event adds 800 to rental_price", not _q_ev.errors and _q_ev.rental_price == 1000.0)
t("AT5a: event in bonus base (gross_profit)", not _q_ev.errors and _q_ev.gross_profit == 1000.0)

_rules_em = {**FALLBACK_RULES, "modifiers": {**FALLBACK_RULES["modifiers"], "default_min_stay": 1, "event_min_days": 2}}
_q_ev_short = pe_quote(
    date(2026, 8, 10), date(2026, 8, 11),   # 1 night only
    {"tseglina": 2},
    event=True, event_guests=5,
    booking_type="cash", rules=_rules_em,
)
t("AT5b: 1-night event errors with event_min_days=2", bool(_q_ev_short.errors))

# AT6 – UI: event fields present on public page and admin PRICE tab; public result has no Anya
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/"), None)
t("AT6: public page has event checkbox", 'name="event"' in r["body"])
t("AT6: public page has event_guests input", 'name="event_guests"' in r["body"])

r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "price"}), None)
t("AT6: admin PRICE tab has event checkbox", 'name="event"' in r["body"])
t("AT6: admin PRICE tab has event_guests input", 'name="event_guests"' in r["body"])

r = lambda_function.lambda_handler(ev("/", {
    "dates": "2026-08-10 to 2026-08-12",
    "h_tseglina": "on", "g_tseglina": "2",
    "event": "on", "event_guests": "10", "btype": "cash",
}), None)
t("AT6: public quote result has no Anya", "Anya" not in r["body"])
t("AT6: public quote result has no Gross profit", "Gross profit" not in r["body"])

# AT7 – All prior tests still unaffected (spot-check New Year FALLBACK pricing)
lambda_function._store = FakeStore()
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "price",
    "dates": "2026-12-30 to 2027-01-02",
    "h_tseglina": "on", "g_tseglina": "5",
    "jacuzzi": "0", "btype": "cash",
}), None)
t("AT7: New Year price tab renders without event param", r["statusCode"] == 200 and "Anya" in r["body"])
r_pub = lambda_function.lambda_handler(ev("/", {
    "dates": "2026-12-30 to 2027-01-02",
    "h_tseglina": "on", "g_tseglina": "5",
    "jacuzzi": "0", "btype": "cash",
}), None)
t("AT7: public New Year quote still no Anya", "Anya" not in r_pub["body"])

# ── AT_det: Unified Details popup tests ──────────────────────────────────────

print("\n=== Details popup tests ===")

_snap_block = {
    "pk": "blocks", "sk": "block#det1",
    "houses": ["tseglina", "zharyna"],
    "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Details Test Guest", "created_by": "admin",
    "created_at": "2026-08-01T10:00:00",
    "status": "active",
    "btype": "monobank",
    "snapshot": {"subtotal": 500.0, "gross_profit": 450.0, "bonus": 90.0},
    "quote_params": {"houses": {"tseglina": 3, "zharyna": 2}, "jacuzzi": 0, "pets": 0,
                     "btype": "monobank", "event": False, "event_guests": 0},
}
_plain_block = {
    "pk": "blocks", "sk": "block#det2",
    "houses": ["modryna"],
    "checkin": "2026-09-15", "checkout": "2026-09-17",
    "label": "Plain Maintenance", "created_by": "admin",
    "created_at": "2026-08-01T11:00:00",
    "status": "active",
}
_store_det = FakeStore([_snap_block, _plain_block])
lambda_function._store = _store_det

# AT_det1 – Snapshot block: Details panel contains all required content and 3 controls
r_det1 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#det1", "month": "2026-09",
}), None)
body_det1 = r_det1["body"]
t("AT_det1: Details panel title 'Details'", "<h2>Details</h2>" in body_det1)
t("AT_det1: label present", "Details Test Guest" in body_det1)
t("AT_det1: house names present", "Tseglina" in body_det1 and "Zharyna" in body_det1)
t("AT_det1: night count present", "3 nights" in body_det1)
t("AT_det1: bonus value present", "90.00" in body_det1)
t("AT_det1: Anya bonus label present", "Anya" in body_det1)
t("AT_det1: guest pays present", "500.00" in body_det1)
t("AT_det1: gross profit present", "450.00" in body_det1)
t("AT_det1: Edit control present", ">Edit<" in body_det1)
t("AT_det1: Cancel control present", ">Cancel<" in body_det1)
t("AT_det1: Done control present", ">Done<" in body_det1)
t("AT_det1: priced-for summary present", "Tseglina: 3 guests" in body_det1)

# AT_det2 – Non-snapshot block: "No pricing recorded" shown, bonus not present
r_det2 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#det2", "month": "2026-09",
}), None)
body_det2 = r_det2["body"]
t("AT_det2: non-snapshot block shows label", "Plain Maintenance" in body_det2)
t("AT_det2: 'No pricing recorded' shown", "No pricing recorded" in body_det2)
t("AT_det2: no bonus line", "Anya" not in body_det2)

# AT_det3 – Cancel two-step: first request (details only) does NOT cancel; block still active
lambda_function._store = FakeStore([dict(_snap_block)])
r_det3a = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#det1", "month": "2026-09",
}), None)
t("AT_det3: first click — block still active", any(
    b.get("sk") == "block#det1" and b.get("status") == "active"
    for b in lambda_function._store._blocks
))
# confirm_cancel=1 shows "Confirm cancel" button but does NOT cancel
r_det3b = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#det1",
    "confirm_cancel": "1", "month": "2026-09",
}), None)
t("AT_det3: confirm_cancel=1 shows Confirm cancel", "Confirm cancel" in r_det3b["body"])
t("AT_det3: confirm_cancel=1 does NOT cancel block", any(
    b.get("sk") == "block#det1" and b.get("status") == "active"
    for b in lambda_function._store._blocks
))

# AT_det4 – action=cancel_block (Confirm cancel link) actually cancels
r_det4 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "cancel_block",
    "block_id": "block#det1",
}), None)
t("AT_det4: action=cancel_block cancels block", all(
    b.get("status") == "cancelled"
    for b in lambda_function._store._blocks
    if b.get("sk") == "block#det1"
))

# AT_det5 – Done link points back to BLOCK tab chart
lambda_function._store = FakeStore([dict(_snap_block)])
r_det5 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#det1", "month": "2026-09",
}), None)
t("AT_det5: Done href targets block tab", 'tab=block' in r_det5["body"] and '>Done<' in r_det5["body"])

# AT_det6 – Non-admin cannot see the details panel
r_det6 = lambda_function.lambda_handler(ev("/admin", {
    "tab": "block", "details": "block#det1",
}), None)
t("AT_det6: non-admin cannot see Details panel", "Details Test Guest" not in r_det6["body"])
t("AT_det6: non-admin sees access denied", "Access denied" in r_det6["body"])

# ── AT_D: Decimal-safe DynamoDB & error-handler tests ────────────────────────

print("\n=== Decimal / error-handler tests ===")

from decimal import Decimal
from reservations import Store


class StrictDynamoTable:
    """Mock DynamoDB table that raises TypeError on float values (like real DynamoDB)."""
    def __init__(self):
        self._items = []

    def _check_no_floats(self, obj, path="root"):
        if isinstance(obj, float):
            raise TypeError(f"Float types are not supported at '{path}'. Use Decimal instead.")
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._check_no_floats(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._check_no_floats(v, f"{path}[{i}]")

    def put_item(self, Item):
        self._check_no_floats(Item)
        self._items.append(dict(Item))

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues=None):
        self._check_no_floats(ExpressionAttributeValues or {})
        vals = ExpressionAttributeValues or {}
        set_part, _, remove_part = UpdateExpression.partition("REMOVE")
        set_part = set_part.replace("SET", "", 1)
        for item in self._items:
            if item.get("pk") != Key["pk"] or item.get("sk") != Key["sk"]:
                continue
            for assign in set_part.split(","):
                if "=" not in assign:
                    continue
                lhs, rhs = (x.strip() for x in assign.split("=", 1))
                item[ExpressionAttributeNames.get(lhs, lhs)] = vals[rhs]
            for nm in remove_part.split(","):
                nm = nm.strip()
                if nm:
                    item.pop(ExpressionAttributeNames.get(nm, nm), None)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **kw):
        pk_val = ExpressionAttributeValues[":pk"]
        return {"Items": [dict(i) for i in self._items if i.get("pk") == pk_val]}


# AT_D1 – add_block with float snapshot passes through StrictDynamoTable without raising
_strict_tbl = StrictDynamoTable()
_strict_store = Store(_strict_tbl)
try:
    result = _strict_store.add_block(
        houses=["tseglina"],
        checkin=date(2026, 11, 1),
        checkout=date(2026, 11, 3),
        label="Decimal test",
        snapshot={"subtotal": 300.0, "gross_profit": 250.0, "bonus": 50.0},
    )
    t("AT_D1: add_block with float snapshot doesn't raise", result.get("ok") is True)
except TypeError as e:
    t("AT_D1: add_block with float snapshot doesn't raise", False)
    print(f"  >> TypeError: {e}")

# AT_D2 – list_blocks returns Python floats/ints (not Decimal) after round-trip
_items_d2 = _strict_store.list_blocks()
t("AT_D2: list_blocks returned items", len(_items_d2) == 1)
snap_d2 = _items_d2[0].get("snapshot", {})
t("AT_D2: subtotal is native numeric not Decimal", isinstance(snap_d2.get("subtotal"), (int, float)) and not isinstance(snap_d2.get("subtotal"), Decimal))
t("AT_D2: gross_profit is native numeric not Decimal", isinstance(snap_d2.get("gross_profit"), (int, float)) and not isinstance(snap_d2.get("gross_profit"), Decimal))

# AT_D3 – handler wraps unhandled exceptions → 500 HTML for non-JSON routes
class BrokenAddStore(FakeStore):
    def add_block(self, *a, **kw):
        raise RuntimeError("simulated DynamoDB failure")

lambda_function._store = BrokenAddStore()
r_d3 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-11-05 to 2026-11-08",
    "h_tseglina": "on", "label": "Crash test",
}), None)
t("AT_D3: unhandled exception → 500", r_d3["statusCode"] == 500)
t("AT_D3: 500 body is HTML error page", "internal error" in r_d3["body"].lower())

# AT_D4 – handler wraps unhandled exceptions → 500 JSON for /availability.json
class BrokenAvailStore(FakeStore):
    def availability(self, checkin, checkout, exclude_id=None):
        raise RuntimeError("simulated availability failure")

lambda_function._store = BrokenAvailStore()
r_d4 = lambda_function.lambda_handler(ev("/availability.json", {
    "checkin": "2026-11-05",
    "checkout": "2026-11-08",
}), None)
t("AT_D4: json route unhandled exception → 500", r_d4["statusCode"] == 500)
import json as _json
_body_d4 = _json.loads(r_d4["body"])
t("AT_D4: 500 JSON has 'error' key", "error" in _body_d4)

# ── AT_hd: Half-day tape chart bars ──────────────────────────────────────────

print("\n=== Half-day tape chart bar tests ===")

# AT_hd1 – checkin=right-half, checkout=left-half, middle days=full
_store_hd1 = FakeStore([{
    "pk": "blocks", "sk": "block#hd1",
    "houses": ["tseglina"],
    "checkin": "2026-08-10", "checkout": "2026-08-15",
    "label": "Half-day Block", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_hd1
r_hd1 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd1 = r_hd1["body"]
t("AT_hd1: bar has half-start at check-in", 'tape-half-start' in body_hd1)
t("AT_hd1: bar has half-end at check-out", 'tape-half-end' in body_hd1)
t("AT_hd1: bar slanted at both ends", 'tape-slant-both' in body_hd1)
t("AT_hd1: 5-night bar is 170px wide", 'width:170px' in body_hd1)
t("AT_hd1: five occupied night cells", body_hd1.count('tape-occ"') == 5)
t("AT_hd1: data-checkin present on bar", 'data-checkin="2026-08-10"' in body_hd1)
t("AT_hd1: data-checkout present on bar", 'data-checkout="2026-08-15"' in body_hd1)
t("AT_hd1: legend text present", "check-in 4pm" in body_hd1)

# AT_hd2 – same-day turnover: block A checkout and block B checkin on same day
_store_hd2 = FakeStore([
    {
        "pk": "blocks", "sk": "block#hd2a",
        "houses": ["tseglina"],
        "checkin": "2026-08-10", "checkout": "2026-08-15",
        "label": "Guest A", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#hd2b",
        "houses": ["tseglina"],
        "checkin": "2026-08-15", "checkout": "2026-08-18",
        "label": "Guest B", "created_by": "admin", "status": "active",
    },
])
lambda_function._store = _store_hd2
r_hd2 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd2 = r_hd2["body"]
t("AT_hd2: both bars slanted at both ends", body_hd2.count('class="tape-bar tape-slant-both') == 2)
t("AT_hd2: departing bar ends mid-cell", 'tape-half-end' in body_hd2)
t("AT_hd2: arriving bar starts mid-cell", 'tape-half-start' in body_hd2)
t("AT_hd2: turnover night counted once (5+3 nights)", body_hd2.count('tape-occ"') == 8)
t("AT_hd2: block A checkout visible on turnover day", 'data-checkout="2026-08-15"' in body_hd2)
t("AT_hd2: block B checkin visible on turnover day", 'data-checkin="2026-08-15"' in body_hd2)
# Conflict check: same house, overlapping dates → Conflict
lambda_function._store = _store_hd2
r_hd2_conflict = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-08-12 to 2026-08-16",
    "h_tseglina": "on", "label": "Conflict test",
}), None)
t("AT_hd2: conflict detection still works", "Conflict" in r_hd2_conflict["body"])

# AT_hd3 – clipped left edge: block started before window → no right-half at window start
# Using trailing-quote suffix (tape-seg-r") to match HTML elements only, not CSS rule text
_store_hd3 = FakeStore([{
    "pk": "blocks", "sk": "block#hd3",
    "houses": ["tseglina"],
    "checkin": "2026-07-25", "checkout": "2026-08-05",
    "label": "Clipped Start", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_hd3
r_hd3 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd3 = r_hd3["body"]
t("AT_hd3: clipped-start has flat leading edge (no half-start)", 'tape-half-start' not in body_hd3)
t("AT_hd3: clipped-start bar begins at cell edge", 'left:0px' in body_hd3)
t("AT_hd3: clipped-start slants only at the trailing end", 'tape-slant-end' in body_hd3)
t("AT_hd3: clipped-start keeps half-end at check-out", 'tape-half-end' in body_hd3)

# AT_hd4 – clipped right edge: block ends after window → no left-half beyond window
# Window for month=2026-08 spans Aug 1 – Sep 30; use checkout=2026-10-05 to clip right edge
# Using trailing-quote suffix to match HTML elements only, not CSS rule text
_store_hd4 = FakeStore([{
    "pk": "blocks", "sk": "block#hd4",
    "houses": ["tseglina"],
    "checkin": "2026-09-25", "checkout": "2026-10-05",
    "label": "Clipped End", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_hd4
r_hd4 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd4 = r_hd4["body"]
t("AT_hd4: clipped-end keeps half-start at check-in", 'tape-half-start' in body_hd4)
t("AT_hd4: clipped-end has flat trailing edge (no half-end)", 'tape-half-end' not in body_hd4)
t("AT_hd4: clipped-end slants only at the leading edge", 'tape-slant-start' in body_hd4)

# AT_hd5 – compact column width preserved in the rendered CSS
t("AT_hd5: day cells keep 34px min-width", ".tape-dc{min-width:34px" in body_hd1)
t("AT_hd5: day cells capped at 34px", "max-width:34px" in body_hd1)
t("AT_hd5: day headers keep 34px min-width", ".tape-dh{min-width:34px" in body_hd1)
t("AT_hd5: chart stays horizontally scrollable", "overflow-x:auto" in body_hd1)
t("AT_hd5: house column stays sticky", "position:sticky" in body_hd1)
t("AT_hd5: bars are absolutely positioned (cannot widen cells)",
  ".tape-bar{position:absolute" in body_hd1)
t("AT_hd5: diagonal edges use clip-path", "clip-path:polygon" in body_hd1)
t("AT_hd5: cell width constant matches CSS", lambda_function._TAPE_CELL_W == 34)

# AT_hd6 – Fri→Sun: Fri right-portion start, Sat full night, Sun left-portion end
# 2026-08-14 is a Friday; checkout 2026-08-16 is a Sunday.
_store_hd6 = FakeStore([{
    "pk": "blocks", "sk": "block#hd6",
    "houses": ["tseglina"],
    "checkin": "2026-08-14", "checkout": "2026-08-16",
    "label": "Weekend", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_hd6
r_hd6 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd6 = r_hd6["body"]
t("AT_hd6: Fri check-in starts mid-cell", 'tape-half-start' in body_hd6)
t("AT_hd6: Sun check-out ends mid-cell", 'tape-half-end' in body_hd6)
t("AT_hd6: bar anchored at Fri offset 17px", 'left:17px' in body_hd6)
t("AT_hd6: 2 nights → 68px (mid-Fri to mid-Sun)", 'width:68px' in body_hd6)
t("AT_hd6: exactly 2 occupied nights (Fri, Sat)", body_hd6.count('tape-occ"') == 2)
t("AT_hd6: short bar hides inline label", 'tape-bar-nolabel' in body_hd6)
t("AT_hd6: hidden label still available as tooltip", 'title="Weekend"' in body_hd6)

# AT_hd7 – per-bar inline geometry, one element per block-house, both clip edges
import re as _re

CELL = lambda_function._TAPE_CELL_W          # 34
HALF = CELL // 2                             # 0.5-cell shift at the check-in edge
SLANT = lambda_function._TAPE_SLANT          # 9

def _bars(body):
    """Every rendered bar as (classes, inline style, title)."""
    return [
        (m.group(1), m.group(2), m.group(3))
        for m in _re.finditer(
            r'<a class="(tape-bar[^"]*)"[^>]*style="[^;]*;([^"]*)"[^>]*title="([^"]*)"',
            body)
    ]

# 2-night block over two houses + 5-night block over one house, same window
_store_hd7 = FakeStore([
    {
        "pk": "blocks", "sk": "block#hd7a",
        "houses": ["modryna", "zharyna"],
        "checkin": "2026-08-21", "checkout": "2026-08-23",   # Fri → Sun, 2 nights
        "label": "TwoNight", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#hd7b",
        "houses": ["tseglina"],
        "checkin": "2026-08-10", "checkout": "2026-08-15",   # 5 nights
        "label": "FiveNight", "created_by": "admin", "status": "active",
    },
])
lambda_function._store = _store_hd7
r_hd7 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)
body_hd7 = r_hd7["body"]
bars_hd7 = _bars(body_hd7)

two = [b for b in bars_hd7 if b[2] == "TwoNight"]
five = [b for b in bars_hd7 if b[2] == "FiveNight"]

# Exactly one bar element per block per house lane — never per-cell fragments
t("AT_hd7: 2-night block renders one bar per house (2 houses)", len(two) == 2)
t("AT_hd7: 5-night block renders exactly one bar", len(five) == 1)
t("AT_hd7: no extra bar elements on the chart", len(bars_hd7) == 3)

# Inline geometry: left carries the 0.5-cell shift, width = nights x cell width
t("AT_hd7: 2-night left offset is the 0.5-cell shift",
  all(f"left:{HALF}px" in b[1] for b in two))
t("AT_hd7: 2-night width = 2 x cell width", all(f"width:{2 * CELL}px" in b[1] for b in two))
t("AT_hd7: 5-night left offset is the 0.5-cell shift", f"left:{HALF}px" in five[0][1])
t("AT_hd7: 5-night width = 5 x cell width", f"width:{5 * CELL}px" in five[0][1])

# Both lead and trail slants on every bar that is not window-clipped
_both = f"clip-path:polygon({SLANT}px 0,100% 0,calc(100% - {SLANT}px) 100%,0 100%)"
t("AT_hd7: every unclipped bar has lead+trail clip-path",
  all(_both in b[1] for b in bars_hd7))
t("AT_hd7: every unclipped bar carries both slant markers",
  all("tape-half-start" in b[0] and "tape-half-end" in b[0] for b in bars_hd7))
t("AT_hd7: slant is fixed px, never a percentage",
  all("%" not in b[1].split("polygon(")[1].split(",")[0] for b in bars_hd7))

# Short bars keep a visible body: slant is clamped so it can never eat the whole bar
t("AT_hd7: 2-night body survives both slants", 2 * CELL - 2 * SLANT > 0)
_store_hd7c = FakeStore([{
    "pk": "blocks", "sk": "block#hd7c",
    "houses": ["tseglina"],
    "checkin": "2026-08-22", "checkout": "2026-08-23",   # 1 night
    "label": "OneNight", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_hd7c
body_hd7c = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)["body"]
one = _bars(body_hd7c)
t("AT_hd7: 1-night block renders exactly one bar", len(one) == 1)
t("AT_hd7: 1-night width = 1 x cell width", f"width:{CELL}px" in one[0][1])
_slant_1n = int(_re.search(r"polygon\((\d+)px", one[0][1]).group(1))
t("AT_hd7: 1-night slant clamped below half the bar", _slant_1n * 2 < CELL)
t("AT_hd7: 1-night body still visible", CELL - 2 * _slant_1n >= 16)

# Bars must paint above day-cell backgrounds (weekend/holiday cells shade the row);
# without this a bar crossing a shaded cell is truncated at the cell edge.
t("AT_hd7: bars sit above the cell layer", "text-decoration:none;box-sizing:border-box;z-index:2" in body_hd7)
t("AT_hd7: sticky label column stays above bars", "left:0;z-index:5" in body_hd7)

# ── AT_nd: short-bar labels, notes, downpayment ──────────────────────────────

print("\n=== Labels / notes / downpayment tests ===")

# AT_nd1 – a 2-night block keeps its inline label; a 1-night block is tooltip-only
_store_nd1 = FakeStore([
    {
        "pk": "blocks", "sk": "block#nd1a",
        "houses": ["tseglina"],
        "checkin": "2026-08-21", "checkout": "2026-08-23",   # 2 nights
        "label": "wedding", "created_by": "admin", "status": "active",
    },
    {
        "pk": "blocks", "sk": "block#nd1b",
        "houses": ["modryna"],
        "checkin": "2026-08-21", "checkout": "2026-08-22",   # 1 night
        "label": "solo", "created_by": "admin", "status": "active",
    },
])
lambda_function._store = _store_nd1
body_nd1 = lambda_function.lambda_handler(
    ev("/admin", {"key": ADMIN_SECRET, "tab": "block", "month": "2026-08"}), None)["body"]
_nd1_bars = {t_[2]: t_ for t_ in _bars(body_nd1)}
# the label must sit inside the bar element, not just in the tooltip
t("AT_nd1: 2-night bar body contains its label text",
  _re.search(r'<a class="tape-bar[^"]*"[^>]*title="wedding"[^>]*>wedding</a>', body_nd1) is not None)
t("AT_nd1: 2-night bar is not marked nolabel",
  "tape-bar-nolabel" not in _nd1_bars["wedding"][0])
t("AT_nd1: 1-night bar is tooltip-only", "tape-bar-nolabel" in _nd1_bars["solo"][0])
t("AT_nd1: 1-night bar renders no inline text",
  _re.search(r'title="solo"[^>]*></a>', body_nd1) is not None)
t("AT_nd1: label truncation is clipped, never widening the bar",
  "overflow:hidden" in body_nd1 and "text-overflow:ellipsis" in body_nd1)

# AT_nd2 – notes round-trip: add → details → edit, and HTML in notes is escaped
_store_nd2 = FakeStore()
lambda_function._store = _store_nd2
r_nd2 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-09-10 to 2026-09-13",
    "h_tseglina": "on", "label": "Notes Guest",
    "notes": "Late arrival <script>alert(1)</script> & pets",
}), None)
t("AT_nd2: add_block with notes succeeds", "Block added" in r_nd2["body"])
_nd2_item = [b for b in _store_nd2._blocks if b.get("label") == "Notes Guest"][0]
t("AT_nd2: notes stored on the item",
  _nd2_item.get("notes") == "Late arrival <script>alert(1)</script> & pets")

r_nd2d = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": _nd2_item["sk"],
}), None)
body_nd2d = r_nd2d["body"]
t("AT_nd2: details shows the Notes heading", ">Notes</h3>" in body_nd2d)
t("AT_nd2: details shows the note text", "Late arrival" in body_nd2d)
t("AT_nd2: raw script tag is escaped in details",
  "<script>alert(1)</script>" not in body_nd2d and "&lt;script&gt;" in body_nd2d)
t("AT_nd2: ampersand escaped in details", "&amp; pets" in body_nd2d)
t("AT_nd2: edit link is minimal (no data threaded through the URL)",
  f'edit_id={_nd2_item["sk"]}' in body_nd2d.replace("%23", "#")
  and "notes=Late%20arrival" not in body_nd2d)
_nd2_edit = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "edit_id": _nd2_item["sk"]}), None)["body"]
t("AT_nd2: edit screen pre-fills notes from the block, escaped",
  "Late arrival &lt;script&gt;" in _nd2_edit)

# Edit view pre-fills the textarea with the stored notes, escaped
r_nd2e = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "edit_id": _nd2_item["sk"],
    "dates": "2026-09-10 to 2026-09-13", "h_tseglina": "on",
    "label": "Notes Guest", "notes": "Late arrival <b>x</b>",
}), None)
t("AT_nd2: edit form has a notes textarea", 'name="notes"' in r_nd2e["body"])
t("AT_nd2: edit textarea pre-filled and escaped",
  "Late arrival &lt;b&gt;x&lt;/b&gt;</textarea>" in r_nd2e["body"])

# Blocks without notes omit the section entirely
_store_nd2b = FakeStore([{
    "pk": "blocks", "sk": "block#nd2b",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "No Notes", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_nd2b
body_nd2b = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#nd2b"}), None)["body"]
t("AT_nd2: block without notes omits the Notes section", ">Notes</h3>" not in body_nd2b)

# AT_nd3 – deposit 500 against a 3600 subtotal → Remaining 3100 in details and BONUS
_snap_nd3 = {"subtotal": 3600.0, "gross_profit": 3000.0, "bonus": 600.0}
_store_nd3 = FakeStore([{
    "pk": "blocks", "sk": "block#nd3",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Deposit Guest", "created_by": "admin", "status": "active",
    "btype": "cash", "snapshot": _snap_nd3, "deposit": 500.0,
}])
lambda_function._store = _store_nd3
body_nd3 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#nd3"}), None)["body"]
t("AT_nd3: details shows money received", "Received: $500.00" in body_nd3)
t("AT_nd3: legacy deposit migrated into the ledger",
  "downpayment (migrated)" in body_nd3)
t("AT_nd3: details shows outstanding 3100", "Outstanding: $3,100.00" in body_nd3)
body_nd3b = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "3", "year": "2026"}), None)["body"]
t("AT_nd3: migrated deposit appears as a payment in its quarter",
  "Deposit Guest" in body_nd3b)
t("AT_nd3: BONUS tab reports money received", "Received:" in body_nd3b)
t("AT_nd3: bonus accrues pro-rata on the 500 received",
  f"${round(500.0 * (600.0 / 3600.0), 2):,.2f}" in body_nd3b)

# AT_nd4 – deposit with no snapshot → unpriced dash, no fabricated remaining
_store_nd4 = FakeStore([{
    "pk": "blocks", "sk": "block#nd4",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Unpriced Deposit", "created_by": "admin", "status": "active",
    "deposit": 250.0,
}])
lambda_function._store = _store_nd4
body_nd4 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#nd4"}), None)["body"]
t("AT_nd4: unpriced block still shows money received", "Received: $250.00" in body_nd4)
t("AT_nd4: unpriced block shows the outstanding dash", "— (unpriced)" in body_nd4)
t("AT_nd4: unpriced block still says no pricing recorded", "No pricing recorded" in body_nd4)

# AT_nd5 – deposit greater than subtotal is flagged as an overpayment
_store_nd5 = FakeStore([{
    "pk": "blocks", "sk": "block#nd5",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Overpaid Guest", "created_by": "admin", "status": "active",
    "btype": "cash", "deposit": 900.0,
    "snapshot": {"subtotal": 700.0, "gross_profit": 600.0, "bonus": 120.0},
}])
lambda_function._store = _store_nd5
body_nd5 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#nd5"}), None)["body"]
t("AT_nd5: overpayment flagged", "overpaid by $200.00" in body_nd5)
t("AT_nd5: overpayment uses the --err colour", "var(--err)" in body_nd5)
t("AT_nd5: outstanding clamped at zero", "Outstanding: $0.00" in body_nd5)

# AT_nd6 – cash basis: bonus accrues as money arrives; the booking's full bonus
# is unchanged by how it was paid (bonus is on gross profit, not on cash).
_paid = {
    "pk": "blocks", "sk": "block#nd6a",
    "houses": ["tseglina"], "checkin": "2026-09-10", "checkout": "2026-09-13",
    "label": "Fully Paid", "created_by": "admin", "status": "active",
    "btype": "cash", "snapshot": {"subtotal": 1000.0, "gross_profit": 800.0, "bonus": 160.0},
}
lambda_function._store = FakeStore([dict(_paid, payments=[
    {"id": "p1", "date": "2026-09-15", "amount": 400.0, "note": ""}])])
_part = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "3", "year": "2026"}), None)["body"]
lambda_function._store = FakeStore([dict(_paid, payments=[
    {"id": "p1", "date": "2026-09-15", "amount": 1000.0, "note": ""}])])
_full = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "3", "year": "2026"}), None)["body"]
t("AT_nd6: part payment accrues a proportional bonus (400 -> 64.00)",
  "$64.00" in _part)
t("AT_nd6: full payment accrues the whole bonus (1000 -> 160.00)",
  "$160.00" in _full)
t("AT_nd6: bonus due tracks cash, not the booking total",
  "BONUS DUE" in _part and "$160.00" not in _part.split("BONUS DUE")[1])
t("AT_nd6: full amount shown as received", "Received: <b>$1,000.00</b>" in _full)

# AT_nd7 – chart marks bars with a balance still owing; paid/unpriced stay unmarked
_store_nd7 = FakeStore([
    dict(_paid, sk="block#nd7a", label="Owing", checkin="2026-09-04",
         checkout="2026-09-09", deposit=200.0),
    dict(_paid, sk="block#nd7b", label="Settled", checkin="2026-09-14",
         checkout="2026-09-19", deposit=1000.0, houses=["modryna"]),
    {"pk": "blocks", "sk": "block#nd7c", "houses": ["zharyna"],
     "checkin": "2026-09-04", "checkout": "2026-09-09", "label": "NoPrice",
     "created_by": "admin", "status": "active"},
])
lambda_function._store = _store_nd7
body_nd7 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "month": "2026-09"}), None)["body"]
_nd7 = {t_[2].split(" ·")[0]: t_ for t_ in _bars(body_nd7)}
t("AT_nd7: bar with a balance owing is marked", "tape-unpaid" in _nd7["Owing"][0])
t("AT_nd7: fully paid bar is unmarked", "tape-unpaid" not in _nd7["Settled"][0])
t("AT_nd7: unpriced bar is unmarked", "tape-unpaid" not in _nd7["NoPrice"][0])
t("AT_nd7: remaining shown in the tooltip", "Owing · $800.00 remaining" in body_nd7)
t("AT_nd7: legend mentions the marker", "balance still owing" in body_nd7)

# AT_nd8 – notes hint appears in the bar tooltip, never as bar text
_store_nd8 = FakeStore([{
    "pk": "blocks", "sk": "block#nd8", "houses": ["tseglina"],
    "checkin": "2026-09-04", "checkout": "2026-09-09", "label": "Tooltip Guest",
    "created_by": "admin", "status": "active", "notes": "call ahead",
}])
lambda_function._store = _store_nd8
body_nd8 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "month": "2026-09"}), None)["body"]
t("AT_nd8: tooltip carries the has-notes hint",
  'title="Tooltip Guest · has notes"' in body_nd8)
t("AT_nd8: note text never rendered on the bar", "call ahead" not in body_nd8)
t("AT_nd8: bar text stays the plain label", ">Tooltip Guest</a>" in body_nd8)

# AT_nd9 – deposit is written through a table mock that rejects floats
_strict_nd9 = StrictDynamoTable()
_store_nd9 = Store(_strict_nd9)
_r_nd9 = _store_nd9.add_block(
    houses=["tseglina"], checkin=date(2026, 11, 1), checkout=date(2026, 11, 3),
    label="Deposit decimal", notes="fine", deposit=500.5,
    snapshot={"subtotal": 3600.0, "gross_profit": 3000.0, "bonus": 600.0},
)
t("AT_nd9: deposit survives a float-rejecting table", _r_nd9.get("ok") is True)
_back_nd9 = _store_nd9.list_blocks()[0]
t("AT_nd9: deposit reads back as a native float", _back_nd9.get("deposit") == 500.5)
t("AT_nd9: deposit is not a Decimal", not isinstance(_back_nd9.get("deposit"), Decimal))
t("AT_nd9: notes round-trip through the store", _back_nd9.get("notes") == "fine")

# AT_nd10 – non-admin sees none of the new block data
lambda_function._store = _store_nd3
r_nd10 = lambda_function.lambda_handler(ev("/admin", {"tab": "block", "details": "block#nd3"}), None)
t("AT_nd10: non-admin sees no deposit", "Downpayment" not in r_nd10["body"])
t("AT_nd10: non-admin sees no notes section", ">Notes</h3>" not in r_nd10["body"])
t("AT_nd10: non-admin denied", "Access denied" in r_nd10["body"])

# ── AT_ov: manual price override ─────────────────────────────────────────────

print("\n=== Price override tests ===")

# Airbnb block: computed subtotal 3600, cleaning 200 → override to 3000
def _ov_block(**kw):
    b = {
        "pk": "blocks", "sk": "block#ov", "houses": ["tseglina"],
        "checkin": "2026-09-10", "checkout": "2026-09-13",
        "label": "Override Guest", "created_by": "admin", "status": "active",
        "btype": "airbnb",
        "snapshot": {
            "subtotal": 3600.0, "gross_profit": 2842.0, "bonus": 568.40,
            "cleaning_total": 200.0, "rental_price": 3400.0,
        },
    }
    b.update(kw)
    return b

def _set_price(store, price, note="friend discount", block_id="block#ov"):
    lambda_function._store = store
    return lambda_function.lambda_handler(ev("/admin", {
        "key": ADMIN_SECRET, "tab": "block", "action": "set_price",
        "block_id": block_id, "price": str(price), "price_note": note,
    }), None)

# AT_ov1 – the recompute itself matches the specified arithmetic
_calc = lambda_function._recompute_from_subtotal(3000.0, 200.0, "airbnb")
t("AT_ov1: airbnb fee on override is 465", _calc["fee"] == 465.0)
t("AT_ov1: rental effective is override minus cleaning",
  _calc["rental_price_effective"] == 2800.0)
t("AT_ov1: gross profit is 2335", _calc["gross_profit"] == 2335.0)
t("AT_ov1: bonus is 467.00", _calc["bonus"] == 467.0)
t("AT_ov1: airbnb listing price is 3550.30", _calc["listing_price"] == 3550.30)
_calc_card = lambda_function._recompute_from_subtotal(3000.0, 200.0, "stripe")
t("AT_ov1: card fee is a pct of the override", _calc_card["fee"] == round(3000 * 0.055, 2))
t("AT_ov1: card type has no listing price", _calc_card["listing_price"] is None)

# AT_ov2 – saving an override stores computed_* and recomputed live fields
_store_ov2 = FakeStore([_ov_block()])
_set_price(_store_ov2, 3000)
_snap_ov2 = _store_ov2._blocks[0]["snapshot"]
t("AT_ov2: override amount stored", _snap_ov2["override_subtotal"] == 3000.0)
t("AT_ov2: live subtotal is the override", _snap_ov2["subtotal"] == 3000.0)
t("AT_ov2: original subtotal preserved", _snap_ov2["computed_subtotal"] == 3600.0)
t("AT_ov2: original bonus preserved", _snap_ov2["computed_bonus"] == 568.40)
t("AT_ov2: gross profit recomputed to 2335", _snap_ov2["gross_profit"] == 2335.0)
t("AT_ov2: bonus recomputed to 467.00", _snap_ov2["bonus"] == 467.0)
t("AT_ov2: listing price recomputed to 3550.30", _snap_ov2["listing_price"] == 3550.30)
t("AT_ov2: reason stored as price_note",
  _store_ov2._blocks[0]["price_note"] == "friend discount")
t("AT_ov2: cleaning untouched by the discount", _snap_ov2["cleaning_total"] == 200.0)

# AT_ov3 – details view shows both prices and the recomputed figures
body_ov3 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#ov"}), None)["body"]
t("AT_ov3: final price shown", "Final price: <b>$3,000.00</b>" in body_ov3)
t("AT_ov3: custom marker with the reason", "(custom — friend discount)" in body_ov3)
t("AT_ov3: original price struck through",
  "line-through" in body_ov3 and "$3,600.00" in body_ov3)
t("AT_ov3: recomputed commission shown", "Airbnb commission: $465.00" in body_ov3)
t("AT_ov3: recomputed gross profit shown", "Gross profit: $2,335.00" in body_ov3)
t("AT_ov3: recomputed bonus shown", "$467.00" in body_ov3)
t("AT_ov3: airbnb listing price shown", "List at: $3,550.30" in body_ov3)
t("AT_ov3: details is read-only — price form lives on the Edit screen",
  ">Edit price</summary>" not in body_ov3 and 'name="final_price"' not in body_ov3)
t("AT_ov3: details links to the Edit screen", "edit_id=" in body_ov3)
_ov3_edit = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "edit_id": "block#ov"}), None)["body"]
t("AT_ov3: edit screen exposes the final-price field", 'name="final_price"' in _ov3_edit)
t("AT_ov3: edit screen prefills the override amount", 'value="3000.00"' in _ov3_edit)
t("AT_ov3: revert control on the Edit screen", "Revert to calculated" in _ov3_edit)

# AT_ov4 – revert restores the computed numbers and drops override-only fields
lambda_function._store = _store_ov2
r_ov4 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "revert_price",
    "block_id": "block#ov"}), None)
_snap_ov4 = _store_ov2._blocks[0]["snapshot"]
t("AT_ov4: revert reports success", "reverted" in r_ov4["body"].lower())
t("AT_ov4: subtotal back to computed", _snap_ov4["subtotal"] == 3600.0)
t("AT_ov4: override field removed", "override_subtotal" not in _snap_ov4)
t("AT_ov4: computed_* fields removed", "computed_subtotal" not in _snap_ov4)
t("AT_ov4: price_note cleared from item",
  "price_note" not in _store_ov2._blocks[0])
t("AT_ov4: details no longer shows a custom marker",
  "(custom" not in lambda_function.lambda_handler(ev("/admin", {
      "key": ADMIN_SECRET, "tab": "block", "details": "block#ov"}), None)["body"])

# AT_ov5 – an override below the cleaning cost is rejected, nothing is written
_store_ov5 = FakeStore([_ov_block()])
r_ov5 = _set_price(_store_ov5, 150)
t("AT_ov5: sub-cleaning override rejected", "at least the cleaning cost" in r_ov5["body"])
t("AT_ov5: rejection names the cleaning figure", "$200.00" in r_ov5["body"])
t("AT_ov5: snapshot untouched after rejection",
  _store_ov5._blocks[0]["snapshot"]["subtotal"] == 3600.0)
t("AT_ov5: no override written after rejection",
  "override_subtotal" not in _store_ov5._blocks[0]["snapshot"])
# exactly at the cleaning cost is allowed
_store_ov5b = FakeStore([_ov_block()])
_set_price(_store_ov5b, 200)
t("AT_ov5: override equal to cleaning is accepted",
  _store_ov5b._blocks[0]["snapshot"]["override_subtotal"] == 200.0)

# AT_ov6 – an override changes the bonus ratio, so cash accrues at the new rate
_store_ov6 = FakeStore([_ov_block()])
_set_price(_store_ov6, 3000)
_store_ov6.set_payments("block#ov", [
    {"id": "p1", "date": "2026-09-15", "amount": 3000.0, "note": ""}])
lambda_function._store = _store_ov6
body_ov6 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "3", "year": "2026"}), None)["body"]
t("AT_ov6: BONUS row shows the payment", "$3,000.00" in body_ov6)
t("AT_ov6: BONUS row carries a custom marker", "custom</span>" in body_ov6)
t("AT_ov6: paying the overridden total accrues the overridden bonus",
  "$467.00" in body_ov6)
t("AT_ov6: pre-override bonus not used", "$568.40" not in body_ov6)

# AT_ov7 – Remaining is measured against the override, not the computed price
_store_ov7 = FakeStore([_ov_block(deposit=500.0)])
_set_price(_store_ov7, 3000)
lambda_function._store = _store_ov7
body_ov7 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#ov"}), None)["body"]
t("AT_ov7: outstanding uses the override (3000-500)", "Outstanding: $2,500.00" in body_ov7)
t("AT_ov7: outstanding does not use the computed price", "Outstanding: $3,100.00" not in body_ov7)

# AT_ov8 – editing dates keeps the override amount, re-derives it, and warns
_store_ov8 = FakeStore([_ov_block()])
_set_price(_store_ov8, 3000)
lambda_function._store = _store_ov8
r_ov8 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#ov",
    "dates": "2026-09-20 to 2026-09-24",          # different dates
    "h_tseglina": "on", "label": "Override Guest",
    "btype": "airbnb", "g_tseglina": "4",
    "override_subtotal": "3000.0", "price_note": "friend discount",
}), None)
t("AT_ov8: edit succeeds", "Block updated" in r_ov8["body"])
t("AT_ov8: warning shown that the custom price predates the change",
  "kept from before this change" in r_ov8["body"])
_new_ov8 = [b for b in _store_ov8._blocks
            if b.get("status") == "active" and b.get("checkin") == "2026-09-20"][0]
t("AT_ov8: override amount preserved verbatim",
  _new_ov8["snapshot"]["override_subtotal"] == 3000.0)
t("AT_ov8: live subtotal still the override", _new_ov8["snapshot"]["subtotal"] == 3000.0)
t("AT_ov8: price_note carried across the edit",
  _new_ov8.get("price_note") == "friend discount")
t("AT_ov8: computed_* reflect the NEW dates, not the old snapshot",
  _new_ov8["snapshot"]["computed_subtotal"] != 3600.0)
t("AT_ov8: bonus re-derived from the override and new cleaning",
  _new_ov8["snapshot"]["bonus"] == lambda_function._recompute_from_subtotal(
      3000.0, _new_ov8["snapshot"]["cleaning_total"], "airbnb")["bonus"])

# AT_ov9 – a block with no snapshot cannot be overridden
_store_ov9 = FakeStore([{
    "pk": "blocks", "sk": "block#ov9", "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Unpriced",
    "created_by": "admin", "status": "active",
}])
r_ov9 = _set_price(_store_ov9, 1000, block_id="block#ov9")
t("AT_ov9: override refused without a price snapshot",
  "no price to override" in r_ov9["body"])
t("AT_ov9: unpriced block has no edit-price form",
  ">Edit price</summary>" not in lambda_function.lambda_handler(ev("/admin", {
      "key": ADMIN_SECRET, "tab": "block", "details": "block#ov9"}), None)["body"])

# AT_ov10 – override survives a float-rejecting table, and non-admin sees none of it
_strict_ov = StrictDynamoTable()
_store_ov10 = Store(_strict_ov)
_store_ov10.add_block(
    houses=["tseglina"], checkin=date(2026, 11, 1), checkout=date(2026, 11, 4),
    label="Strict override", btype="airbnb",
    snapshot={"subtotal": 3600.0, "gross_profit": 2842.0, "bonus": 568.40,
              "cleaning_total": 200.0, "rental_price": 3400.0},
)
_sk_ov10 = _store_ov10.list_blocks()[0]["sk"]
_store_ov10.set_price_override(
    _sk_ov10,
    lambda_function._apply_override(
        _store_ov10.list_blocks()[0]["snapshot"], "airbnb", 3000.0),
    3000.0, "friend discount")
_read_ov10 = _store_ov10.list_blocks()[0]
t("AT_ov10: override written through a float-rejecting table",
  _read_ov10["snapshot"]["override_subtotal"] == 3000.0)
t("AT_ov10: recomputed bonus survives the round-trip",
  _read_ov10["snapshot"]["bonus"] == 467.0)
t("AT_ov10: no Decimal leaks back out",
  not isinstance(_read_ov10["snapshot"]["bonus"], Decimal))
t("AT_ov10: price_note round-trips", _read_ov10["price_note"] == "friend discount")
_store_ov10.set_price_override(
    _sk_ov10, lambda_function._revert_override(_read_ov10["snapshot"]), None, None)
_rev_ov10 = _store_ov10.list_blocks()[0]
t("AT_ov10: REMOVE clears the override on revert",
  "override_subtotal" not in _rev_ov10 and _rev_ov10["snapshot"]["subtotal"] == 3600.0)

lambda_function._store = _store_ov7
r_ov10n = lambda_function.lambda_handler(ev("/admin", {
    "tab": "block", "details": "block#ov"}), None)
t("AT_ov10: non-admin sees no price override UI",
  "Edit price" not in r_ov10n["body"] and "Access denied" in r_ov10n["body"])
r_ov10s = lambda_function.lambda_handler(ev("/admin", {
    "tab": "block", "action": "set_price", "block_id": "block#ov", "price": "1"}), None)
t("AT_ov10: non-admin cannot set a price",
  "Access denied" in r_ov10s["body"]
  and _store_ov7._blocks[0]["snapshot"]["subtotal"] == 3000.0)

# ── AT_pl: payment ledger + quarterly cash-basis bonus ───────────────────────

print("\n=== Payment ledger / quarterly bonus tests ===")

# 3625 subtotal, 568.40 bonus → ratio 0.15680; 1000 → 156.80, 2625 → 411.60
_PL_SNAP = {"subtotal": 3625.0, "gross_profit": 2842.0, "bonus": 568.40,
            "cleaning_total": 200.0, "rental_price": 3425.0}

def _pl_block(payments, **kw):
    b = {
        "pk": "blocks", "sk": "block#pl", "houses": ["tseglina"],
        "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Ledger Guest",
        "created_by": "admin", "created_at": "2026-08-01T10:00:00",
        "status": "active", "btype": "cash", "snapshot": dict(_PL_SNAP),
        "payments": payments,
    }
    b.update(kw)
    return b

def _q(store, q, year=2026):
    lambda_function._store = store
    return lambda_function.lambda_handler(ev("/admin", {
        "key": ADMIN_SECRET, "tab": "bonus", "q": str(q), "year": str(year)}), None)["body"]

# AT_pl1 – two payments in Q4 2026 → received 3625, bonus due 568.40
_two = [{"id": "a", "date": "2026-10-05", "amount": 1000.0, "note": "deposit"},
        {"id": "b", "date": "2026-11-20", "amount": 2625.0, "note": "balance"}]
_store_pl1 = FakeStore([_pl_block(_two)])
body_pl1 = _q(_store_pl1, 4)
t("AT_pl1: Q4 received totals 3625", "Received: <b>$3,625.00</b>" in body_pl1)
t("AT_pl1: Q4 bonus due totals 568.40", "BONUS DUE: <b>$568.40</b>" in body_pl1)
t("AT_pl1: both payments listed", "2026-10-05" in body_pl1 and "2026-11-20" in body_pl1)
t("AT_pl1: payments grouped under one booking", body_pl1.count("Ledger Guest") == 1)
t("AT_pl1: first payment accrues 156.80", "$156.80" in body_pl1)
t("AT_pl1: second payment accrues 411.60", "$411.60" in body_pl1)

# AT_pl2 – a single 1000 payment accrues 156.80 and leaves a balance
_store_pl2 = FakeStore([_pl_block([_two[0]])])
body_pl2 = _q(_store_pl2, 4)
t("AT_pl2: single payment accrues 156.80", "$156.80" in body_pl2)
t("AT_pl2: bonus due is only what was received", "BONUS DUE: <b>$156.80</b>" in body_pl2)
_rec, _out, _over, _pr = lambda_function._payment_state(_pl_block([_two[0]]))
t("AT_pl2: received reads from the ledger", _rec == 1000.0)
t("AT_pl2: outstanding is subtotal minus received", _out == 2625.0)

# AT_pl3 – a payment dated Q1 2027 appears only in that quarter
_store_pl3 = FakeStore([_pl_block(
    _two + [{"id": "c", "date": "2027-02-10", "amount": 500.0, "note": "late"}])])
t("AT_pl3: Q1-2027 payment absent from Q4-2026", "2027-02-10" not in _q(_store_pl3, 4))
_body_q1 = _q(_store_pl3, 1, 2027)
t("AT_pl3: Q1-2027 payment present in its own quarter", "2027-02-10" in _body_q1)
t("AT_pl3: Q1-2027 shows only that payment", "2026-10-05" not in _body_q1)
t("AT_pl3: Q1-2027 totals only that payment", "Received: <b>$500.00</b>" in _body_q1)

# AT_pl4 – legacy deposit migrates to payment #1, totals intact
_legacy = {
    "pk": "blocks", "sk": "block#leg", "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Legacy Guest",
    "created_by": "admin", "created_at": "2026-10-02T09:00:00", "status": "active",
    "btype": "cash", "snapshot": dict(_PL_SNAP), "deposit": 1000.0,
}
_mig = lambda_function._payments_of(_legacy)
t("AT_pl4: legacy deposit becomes exactly one payment", len(_mig) == 1)
t("AT_pl4: migrated amount preserved", _mig[0]["amount"] == 1000.0)
t("AT_pl4: migrated date comes from created_at", _mig[0]["date"] == "2026-10-02")
t("AT_pl4: migrated payment is labelled", _mig[0]["note"] == "downpayment (migrated)")
t("AT_pl4: received matches the legacy deposit",
  lambda_function._payment_state(_legacy)[0] == 1000.0)
t("AT_pl4: migrated payment lands in its quarter",
  "$156.80" in _q(FakeStore([_legacy]), 4))
t("AT_pl4: a real ledger makes deposit ignored",
  lambda_function._payment_state(dict(_legacy, payments=[]))[0] == 0.0)

# AT_pl5 – a stay edit carries both payments onto the RECREATED item
_store_pl5 = FakeStore([_pl_block(_two)])
lambda_function._store = _store_pl5
r_pl5 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#pl", "dates": "2026-09-20 to 2026-09-24",
    "h_tseglina": "on", "label": "Ledger Guest",
}), None)
t("AT_pl5: edit succeeds", "Block updated" in r_pl5["body"])
_new_pl5 = [b for b in _store_pl5._blocks
            if b.get("status") == "active" and b.get("checkin") == "2026-09-20"][0]
t("AT_pl5: both payments carried onto the new item",
  len(_new_pl5.get("payments", [])) == 2)
t("AT_pl5: carried amounts intact",
  sorted(p["amount"] for p in _new_pl5["payments"]) == [1000.0, 2625.0])
t("AT_pl5: carried notes intact",
  {p["note"] for p in _new_pl5["payments"]} == {"deposit", "balance"})
t("AT_pl5: ledger not threaded through the edit URL",
  "payments=" not in r_pl5["body"] and "amount=" not in r_pl5["body"])

# AT_pl6 – a conflicting edit carrying a new payment writes NOTHING
_store_pl6 = FakeStore([
    _pl_block(_two),
    {"pk": "blocks", "sk": "block#other", "houses": ["tseglina"],
     "checkin": "2026-09-20", "checkout": "2026-09-25", "label": "Blocker",
     "created_by": "admin", "status": "active"},
])
_before = len(_store_pl6._blocks)
lambda_function._store = _store_pl6
r_pl6 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "edit_id": "block#pl", "dates": "2026-09-21 to 2026-09-24",
    "h_tseglina": "on", "label": "Ledger Guest", "deposit": "750",
}), None)
t("AT_pl6: conflicting edit rejected naming the blocker",
  "Conflict" in r_pl6["body"] and "Blocker" in r_pl6["body"])
t("AT_pl6: no new block written", len(_store_pl6._blocks) == _before)
_orig_pl6 = [b for b in _store_pl6._blocks if b["sk"] == "block#pl"][0]
t("AT_pl6: original block still active", _orig_pl6["status"] == "active")
t("AT_pl6: no payment written on conflict", len(_orig_pl6["payments"]) == 2)
t("AT_pl6: the submitted 750 was not recorded",
  all(p["amount"] != 750.0 for p in _orig_pl6["payments"]))

# AT_pl7 – a negative payment (refund) subtracts from both totals
_store_pl7 = FakeStore([_pl_block(
    _two + [{"id": "r", "date": "2026-12-01", "amount": -625.0, "note": "refund"}])])
body_pl7 = _q(_store_pl7, 4)
t("AT_pl7: refund accepted in the ledger", "refund" in body_pl7)
t("AT_pl7: refund subtracts from received", "Received: <b>$3,000.00</b>" in body_pl7)
t("AT_pl7: refund subtracts bonus too", "BONUS DUE: <b>$470.40</b>" in body_pl7)
t("AT_pl7: refund shown as a negative amount", "-$625.00" in body_pl7)

# AT_pl8 – an override changes the ratio, so the same cash accrues differently
_store_pl8 = FakeStore([_pl_block([_two[0]])])
_pre = _q(_store_pl8, 4)
_store_pl8.set_price_override(
    "block#pl",
    lambda_function._apply_override(dict(_PL_SNAP), "cash", 2000.0),
    2000.0, "discount")
_post = _q(_store_pl8, 4)
t("AT_pl8: pre-override 1000 accrues 156.80", "$156.80" in _pre)
_r8 = lambda_function._bonus_ratio(
    dict(_pl_block([_two[0]]),
         snapshot=lambda_function._apply_override(dict(_PL_SNAP), "cash", 2000.0)))
t("AT_pl8: override raises the bonus ratio", _r8 > 568.40 / 3625)
t("AT_pl8: same cash now accrues at the new rate",
  f"${round(1000.0 * _r8, 2):,.2f}" in _post)
t("AT_pl8: old accrual no longer shown", "$156.80" not in _post)

# AT_pl9 – payments against an unpriced block are listed, not silently dropped
_store_pl9 = FakeStore([_pl_block(
    [{"id": "u", "date": "2026-10-08", "amount": 400.0, "note": "holding"}],
    snapshot=None, sk="block#unp", label="Unpriced Guest")])
_store_pl9._blocks[0].pop("snapshot", None)
body_pl9 = _q(_store_pl9, 4)
t("AT_pl9: unpriced block with payments is listed", "Unpriced Guest" in body_pl9)
t("AT_pl9: flagged bonus not computable", "bonus not computable" in body_pl9)
t("AT_pl9: its amount is shown", "$400.00" in body_pl9)
t("AT_pl9: it contributes no bonus", "BONUS DUE: <b>$0.00</b>" in body_pl9)
t("AT_pl9: but does count as received", "Received: <b>$400.00</b>" in body_pl9)

# AT_pl10 – add / remove payment from Details, two-step remove, admin only
_store_pl10 = FakeStore([_pl_block([_two[0]])])
lambda_function._store = _store_pl10
r_add = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_payment",
    "block_id": "block#pl", "amount": "2625", "pay_date": "2026-11-20",
    "pay_note": "balance"}), None)
t("AT_pl10: add payment reports success", "Payment of $2,625.00 recorded" in r_add["body"])
t("AT_pl10: ledger now has two payments", len(_store_pl10._blocks[0]["payments"]) == 2)
t("AT_pl10: details lists the Payments section", ">Payments</h3>" in r_add["body"])
t("AT_pl10: details shows bonus earned to date", "Bonus earned to date" in r_add["body"])
_pid = [p["id"] for p in _store_pl10._blocks[0]["payments"] if p["amount"] == 2625.0][0]
r_rm1 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": "block#pl", "confirm_rm": _pid}), None)
t("AT_pl10: remove needs a confirmation step", "confirm remove" in r_rm1["body"])
t("AT_pl10: viewing the confirm step removes nothing",
  len(_store_pl10._blocks[0]["payments"]) == 2)
r_rm2 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "remove_payment",
    "block_id": "block#pl", "payment_id": _pid}), None)
t("AT_pl10: confirmed remove deletes the payment",
  len(_store_pl10._blocks[0]["payments"]) == 1)
t("AT_pl10: the right payment was removed",
  _store_pl10._blocks[0]["payments"][0]["amount"] == 1000.0)
r_pl10n = lambda_function.lambda_handler(ev("/admin", {
    "tab": "block", "action": "add_payment", "block_id": "block#pl", "amount": "99"}), None)
t("AT_pl10: non-admin cannot add a payment",
  "Access denied" in r_pl10n["body"]
  and len(_store_pl10._blocks[0]["payments"]) == 1)

# AT_pl11 – downpayment on the add form becomes payment #1, and is Decimal-safe
_store_pl11 = FakeStore()
lambda_function._store = _store_pl11
lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "add_block",
    "dates": "2026-09-10 to 2026-09-13", "h_tseglina": "on",
    "label": "New Guest", "deposit": "500"}), None)
_new11 = _store_pl11._blocks[-1]
t("AT_pl11: downpayment creates payment #1", len(_new11.get("payments", [])) == 1)
t("AT_pl11: payment #1 carries the amount", _new11["payments"][0]["amount"] == 500.0)
t("AT_pl11: payment #1 is labelled downpayment",
  _new11["payments"][0]["note"] == "downpayment")

_strict_pl = StrictDynamoTable()
_store_pl12 = Store(_strict_pl)
_store_pl12.add_block(
    houses=["tseglina"], checkin=date(2026, 11, 1), checkout=date(2026, 11, 4),
    label="Ledger decimal", snapshot=dict(_PL_SNAP),
    payments=[{"id": "x", "date": "2026-11-01", "amount": 1000.5, "note": "n"}])
_sk_pl = _store_pl12.list_blocks()[0]["sk"]
_store_pl12.set_payments(_sk_pl, [
    {"id": "x", "date": "2026-11-01", "amount": 1000.5, "note": "n"},
    {"id": "y", "date": "2026-11-02", "amount": 24.25, "note": "m"}])
_read_pl = _store_pl12.list_blocks()[0]
t("AT_pl11: ledger survives a float-rejecting table",
  len(_read_pl["payments"]) == 2)
t("AT_pl11: payment amounts read back as native floats",
  _read_pl["payments"][0]["amount"] == 1000.5
  and not isinstance(_read_pl["payments"][0]["amount"], Decimal))
t("AT_pl11: received sums the stored ledger",
  lambda_function._payment_state(_read_pl)[0] == 1024.75)

# ── AT_ue: unified Edit screen ───────────────────────────────────────────────

print("\n=== Unified Edit screen tests ===")

PROPS_UE = FALLBACK_RULES["properties"]

def _edit(store, sk):
    lambda_function._store = store
    return lambda_function.lambda_handler(ev("/admin", {
        "key": ADMIN_SECRET, "tab": "block", "edit_id": sk}), None)["body"]

def _save(store, sk, **fields):
    lambda_function._store = store
    q = {"key": ADMIN_SECRET, "tab": "block", "action": "add_block", "edit_id": sk}
    q.update({k: str(v) for k, v in fields.items()})
    return lambda_function.lambda_handler(ev("/admin", q), None)

def _active(store, checkin):
    return [b for b in store._blocks
            if b.get("status") == "active" and b.get("checkin") == checkin][0]

# AT_ue1 – all three sections are present and visible on one page
_store_ue1 = FakeStore([_pl_block(_two)])
body_ue1 = _edit(_store_ue1, "block#pl")
t("AT_ue1: stay section present", "1 · Stay" in body_ue1)
t("AT_ue1: price section present", "2 · Price" in body_ue1)
t("AT_ue1: payments section present", "3 · Payments" in body_ue1)
t("AT_ue1: stay fields prefilled", 'value="2026-09-10 to 2026-09-13"' in body_ue1)
t("AT_ue1: label prefilled", 'value="Ledger Guest"' in body_ue1)
t("AT_ue1: existing payments listed", "2026-10-05" in body_ue1 and "2026-11-20" in body_ue1)
t("AT_ue1: received/outstanding shown", "Received:" in body_ue1 and "Outstanding:" in body_ue1)
t("AT_ue1: add-payment row present", 'name="pay_amount"' in body_ue1)
t("AT_ue1: pricing controls are NOT hidden behind a disclosure",
  "<details" not in body_ue1)
t("AT_ue1: single submit for the whole page", body_ue1.count("<form") == 1)

# AT_ue2 – editing an UNPRICED block and setting btype prices it
_store_ue2 = FakeStore([{
    "pk": "blocks", "sk": "block#unp2", "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Was Unpriced",
    "created_by": "admin", "status": "active",
}])
body_ue2 = _edit(_store_ue2, "block#unp2")
t("AT_ue2: unpriced block still offers the booking-type select",
  'name="btype"' in body_ue2)
t("AT_ue2: unpriced block prompts for a booking type",
  "Choose a booking type" in body_ue2)
r_ue2 = _save(_store_ue2, "block#unp2",
              dates="2026-09-10 to 2026-09-13", h_tseglina="on",
              label="Was Unpriced", btype="cash", g_tseglina="4")
t("AT_ue2: save succeeds", "Block updated" in r_ue2["body"])
_new_ue2 = _active(_store_ue2, "2026-09-10")
t("AT_ue2: snapshot now exists", bool(_new_ue2.get("snapshot")))
t("AT_ue2: snapshot has a subtotal", _new_ue2["snapshot"]["subtotal"] > 0)
t("AT_ue2: snapshot has a bonus", _new_ue2["snapshot"]["bonus"] > 0)
t("AT_ue2: btype recorded", _new_ue2.get("btype") == "cash")
t("AT_ue2: no override created when priced from scratch",
  "override_subtotal" not in _new_ue2["snapshot"])

# AT_ue3 – a final price differing from calculated creates an override
_store_ue3 = FakeStore([{
    "pk": "blocks", "sk": "block#ue3", "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Discount Guest",
    "created_by": "admin", "status": "active", "btype": "cash",
}])
_r_calc = _save(_store_ue3, "block#ue3", dates="2026-09-10 to 2026-09-13",
                h_tseglina="on", label="Discount Guest", btype="cash", g_tseglina="4")
_calc_sub = _active(_store_ue3, "2026-09-10")["snapshot"]["subtotal"]
_sk3 = _active(_store_ue3, "2026-09-10")["sk"]
r_ue3 = _save(_store_ue3, _sk3, dates="2026-09-10 to 2026-09-13", h_tseglina="on",
              label="Discount Guest", btype="cash", g_tseglina="4",
              final_price=f"{_calc_sub - 200:.2f}", price_note="friend discount")
_new_ue3 = _active(_store_ue3, "2026-09-10")
t("AT_ue3: override created from the Final price field",
  _new_ue3["snapshot"].get("override_subtotal") == round(_calc_sub - 200, 2))
t("AT_ue3: computed price preserved alongside",
  _new_ue3["snapshot"]["computed_subtotal"] == _calc_sub)
t("AT_ue3: reason stored", _new_ue3.get("price_note") == "friend discount")
_det_ue3 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "details": _new_ue3["sk"]}), None)["body"]
t("AT_ue3: details shows the final price", "Final price:" in _det_ue3)
t("AT_ue3: details shows both prices", "line-through" in _det_ue3
  and f"${_calc_sub:,.2f}" in _det_ue3)
t("AT_ue3: details shows the custom reason", "friend discount" in _det_ue3)

# AT_ue4 – leaving Final price equal to calculated stores no override
_store_ue4 = FakeStore([{
    "pk": "blocks", "sk": "block#ue4", "houses": ["tseglina"],
    "checkin": "2026-09-10", "checkout": "2026-09-13", "label": "Full Price",
    "created_by": "admin", "status": "active", "btype": "cash",
}])
_save(_store_ue4, "block#ue4", dates="2026-09-10 to 2026-09-13", h_tseglina="on",
      label="Full Price", btype="cash", g_tseglina="4")
_c4 = _active(_store_ue4, "2026-09-10")["snapshot"]["subtotal"]
_sk4 = _active(_store_ue4, "2026-09-10")["sk"]
_save(_store_ue4, _sk4, dates="2026-09-10 to 2026-09-13", h_tseglina="on",
      label="Full Price", btype="cash", g_tseglina="4", final_price=f"{_c4:.2f}")
_n4 = _active(_store_ue4, "2026-09-10")
t("AT_ue4: equal final price stores no override",
  "override_subtotal" not in _n4["snapshot"])
t("AT_ue4: subtotal is still the calculated one", _n4["snapshot"]["subtotal"] == _c4)

# AT_ue5 – adding a payment from the Edit screen updates totals and the BONUS tab
_store_ue5 = FakeStore([_pl_block([_two[0]])])
r_ue5 = _save(_store_ue5, "block#pl", dates="2026-09-10 to 2026-09-13",
              h_tseglina="on", label="Ledger Guest", btype="cash",
              pay_amount="2625", pay_date="2026-11-20", pay_note="balance")
t("AT_ue5: save succeeds", "Block updated" in r_ue5["body"])
_n5 = _active(_store_ue5, "2026-09-10")
t("AT_ue5: payment appended to the ledger", len(_n5["payments"]) == 2)
t("AT_ue5: carried payment kept", any(p["amount"] == 1000.0 for p in _n5["payments"]))
t("AT_ue5: new payment recorded", any(p["amount"] == 2625.0 for p in _n5["payments"]))
_rec5, _out5, _, _ = lambda_function._payment_state(_n5)
t("AT_ue5: received updated to 3625", _rec5 == 3625.0)
t("AT_ue5: outstanding now zero", _out5 == 0.0)
_edit5 = _edit(_store_ue5, _n5["sk"])
t("AT_ue5: edit screen reflects the new totals", "$3,625.00" in _edit5)
_q5 = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "bonus", "q": "4", "year": "2026"}), None)["body"]
t("AT_ue5: payment appears in the quarterly bonus report", "2026-11-20" in _q5)
_ratio5 = lambda_function._bonus_ratio(_n5)
t("AT_ue5: quarterly bonus accrues at the re-derived ratio",
  f"${round(2625.0 * _ratio5, 2):,.2f}" in _q5)
t("AT_ue5: ratio comes from the recomputed snapshot, not the stale one",
  abs(_ratio5 - 568.40 / 3625.0) > 1e-9)

# AT_ue6 – a conflicting date change rejects the WHOLE save, payment included
_store_ue6 = FakeStore([
    _pl_block([_two[0]]),
    {"pk": "blocks", "sk": "block#blocker", "houses": ["tseglina"],
     "checkin": "2026-09-20", "checkout": "2026-09-25", "label": "Blocker",
     "created_by": "admin", "status": "active"},
])
_before6 = len(_store_ue6._blocks)
r_ue6 = _save(_store_ue6, "block#pl", dates="2026-09-21 to 2026-09-24",
              h_tseglina="on", label="Ledger Guest", btype="cash",
              final_price="999", price_note="should not stick",
              pay_amount="500", pay_date="2026-11-05")
t("AT_ue6: conflict reported naming the blocker",
  "Conflict" in r_ue6["body"] and "Blocker" in r_ue6["body"])
t("AT_ue6: no new block written", len(_store_ue6._blocks) == _before6)
_orig6 = [b for b in _store_ue6._blocks if b["sk"] == "block#pl"][0]
t("AT_ue6: original block untouched and active",
  _orig6["status"] == "active" and _orig6["checkin"] == "2026-09-10")
t("AT_ue6: payment from the same submit NOT written",
  len(_orig6["payments"]) == 1 and _orig6["payments"][0]["amount"] == 1000.0)
t("AT_ue6: override from the same submit NOT written",
  "override_subtotal" not in _orig6.get("snapshot", {}))
t("AT_ue6: price_note from the same submit NOT written",
  _orig6.get("price_note") in (None, ""))
t("AT_ue6: rejected save re-renders the edit screen with submitted values",
  "2026-09-21 to 2026-09-24" in r_ue6["body"] and "3 · Payments" in r_ue6["body"])

# AT_ue7 – removing a payment from the Edit screen returns to the Edit screen
_store_ue7 = FakeStore([_pl_block(_two)])
lambda_function._store = _store_ue7
_pid7 = _two[1]["id"]
r_ue7a = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "edit_id": "block#pl",
    "confirm_rm": _pid7}), None)["body"]
t("AT_ue7: remove needs confirmation on the edit screen", "confirm remove" in r_ue7a)
t("AT_ue7: nothing removed at the confirm step",
  len(_store_ue7._blocks[0]["payments"]) == 2)
r_ue7b = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "action": "remove_payment",
    "block_id": "block#pl", "payment_id": _pid7, "back": "edit"}), None)["body"]
t("AT_ue7: confirmed remove deletes it",
  len(_store_ue7._blocks[0]["payments"]) == 1)
t("AT_ue7: returns to the edit screen, not details", "3 · Payments" in r_ue7b)

# AT_ue9 – a stale snapshot must not become a silent override on an unchanged Save.
# This block's stored subtotal (3625) does not match what the engine now calculates,
# so prefilling from the stored value would pin the stale number as a custom price.
_store_ue9 = FakeStore([_pl_block([], btype="cash")])
body_ue9 = _edit(_store_ue9, "block#pl")
_live9 = lambda_function._quote_for_prefill(
    lambda_function._edit_prefill(_store_ue9._blocks[0], PROPS_UE, {}), PROPS_UE)
t("AT_ue9: calculated price differs from the stale stored subtotal",
  _live9 is not None and abs(_live9.subtotal - 3625.0) > 0.01)
t("AT_ue9: final price prefills from the calculated value, not the stale one",
  f'value="{_live9.subtotal:.2f}"' in body_ue9)
r_ue9 = _save(_store_ue9, "block#pl", dates="2026-09-10 to 2026-09-13",
              h_tseglina="on", label="Ledger Guest", btype="cash",
              final_price=f"{_live9.subtotal:.2f}")
_n9 = _active(_store_ue9, "2026-09-10")
t("AT_ue9: unchanged Save creates no override",
  "override_subtotal" not in _n9["snapshot"])
# ...but a block that really does carry an override still prefills with it
_store_ue9b = FakeStore([_pl_block([], btype="cash", override_subtotal=2000.0,
                                   snapshot=lambda_function._apply_override(
                                       dict(_PL_SNAP), "cash", 2000.0))])
t("AT_ue9: an existing override still prefills the field",
  'value="2000.00"' in _edit(_store_ue9b, "block#pl"))

# AT_ue8 – non-admin cannot reach the edit screen
r_ue8 = lambda_function.lambda_handler(ev("/admin", {
    "tab": "block", "edit_id": "block#pl"}), None)
t("AT_ue8: non-admin denied the edit screen",
  "Access denied" in r_ue8["body"] and "1 · Stay" not in r_ue8["body"])

# ── Summary ──────────────────────────────────────────────────────────────────
print()
print(f"Results: {_passed} passed, {_failed} failed out of {_passed + _failed}")

# Only reached if every assertion above ran. The deploy gate requires this line.
_suite_completed = True
print(f"SUITE COMPLETE: {_passed + _failed} assertions")
if _failed:
    sys.exit(1)
