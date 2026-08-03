"""Handler acceptance tests — all 16 (10 prior + 6 tape-chart)."""
import sys
sys.path.insert(0, ".")
import lambda_function
from datetime import date

ADMIN_SECRET = lambda_function.ADMIN_SECRET
_passed = 0
_failed = 0


class FakeStore:
    def __init__(self, blocks=None):
        self._blocks = [dict(b) for b in (blocks or [])]
        self._next = 1000

    def list_blocks(self):
        return [dict(b) for b in self._blocks]

    def availability(self, checkin, checkout):
        result = {}
        for key in ["tseglina", "modryna", "zharyna"]:
            active = [
                b for b in self._blocks
                if b.get("status") == "active"
                and key in b.get("houses", [])
                and date.fromisoformat(b["checkin"]) < checkout
                and date.fromisoformat(b["checkout"]) > checkin
            ]
            if active:
                result[key] = {"available": False, "blocked_by": active[0].get("label", "?")}
            else:
                result[key] = {"available": True, "blocked_by": None}
        return result

    def add_block(self, houses, checkin, checkout, label, created_by="admin"):
        sk = f"block#{self._next}"
        self._next += 1
        self._blocks.append({
            "pk": "blocks", "sk": sk,
            "houses": houses,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "label": label,
            "created_by": created_by,
            "status": "active",
        })
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
# The checkout day (13th) must not be occupied: no td with data-checkin starting on or after 13th
# covering it. We verify no block bar for the 13th in tseglina's row by checking no
# data-checkin="2026-09-13" exists (back-to-back test uses a different store)
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
# Adjacent bars: block1 ends col for 09-12, block2 starts col for 09-13 — no shared cell
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

# ── Summary ──────────────────────────────────────────────────────────────────
print()
print(f"Results: {_passed} passed, {_failed} failed out of {_passed + _failed}")
if _failed:
    sys.exit(1)
