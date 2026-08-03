"""Handler acceptance tests — all 23 (10 prior + 6 tape-chart + 7 new)."""
import sys
sys.path.insert(0, ".")
import lambda_function
from datetime import date
from pricing_engine import FALLBACK_RULES

ADMIN_SECRET = lambda_function.ADMIN_SECRET
_passed = 0
_failed = 0


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
                  exclude_id=None, snapshot=None, quote_params=None, btype=None):
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
        self._blocks.append(item)
        return {"ok": True}

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
t("T2: bar spans exactly 3 days (colspan=3)", 'colspan="3"' in body)
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

# Tape 5 – cancel_confirm flow: bar link shows details + Cancel; after cancel bar gone
_store_t5 = FakeStore([{
    "pk": "blocks", "sk": "block#cc1",
    "houses": ["modryna"],
    "checkin": "2026-09-05", "checkout": "2026-09-08",
    "label": "Confirm Cancel Test", "created_by": "admin", "status": "active",
}])
lambda_function._store = _store_t5
# Step A: cancel_confirm shows detail panel
r = lambda_function.lambda_handler(ev("/admin", {
    "key": ADMIN_SECRET, "tab": "block", "cancel_confirm": "block#cc1", "month": "2026-09",
}), None)
body = r["body"]
t("T5: cancel_confirm shows label", "Confirm Cancel Test" in body)
t("T5: cancel_confirm shows cancel action link", "cancel_block" in body)
t("T5: cancel_confirm shows 'Yes, cancel'", "Yes, cancel" in body)
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
t("T6: non-admin sees no tape chart", 'class="tape-bar"' not in body)

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
lambda_function._store = _store_at4
r = lambda_function.lambda_handler(ev("/admin", {"key": ADMIN_SECRET, "tab": "bonus", "month": "2026-08"}), None)
body = r["body"]
t("AT4: BONUS tab renders 200", r["statusCode"] == 200)
t("AT4: revenue block shown", "Revenue Block" in body)
t("AT4: total bonus 100.00", "100.00" in body)
t("AT4: unpriced block listed", "Maintenance Block" in body)
t("AT4: cancelled block absent", "Cancelled Revenue" not in body)
t("AT4: different-month block absent", "Different Month Block" not in body)

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

# ── Summary ──────────────────────────────────────────────────────────────────
print()
print(f"Results: {_passed} passed, {_failed} failed out of {_passed + _failed}")
if _failed:
    sys.exit(1)
