"""
Zubyria Properties — Pricing Engine v0.2
SOURCE OF TRUTH: the published Google Sheet "Zubyria Pricing Rules" (CSV export).
The Lambda fetches it and caches for 10 minutes, so sheet edits go live within
minutes, no deploy needed. If the fetch ever fails, FALLBACK_RULES below keep
pricing alive (they mirror the sheet as of 2026-08-02 and are NOT the place to
edit rules — edit the sheet).

Business rules (decided 2026-08-02):
  - Holiday multiplier base is read from the sheet (holiday_multiplier_base):
    'applicable_rate' = multiplier x that night's normal rate (weekend on Fri/Sat)
    'weekday_rate'    = multiplier x weekday rate, replacing weekend pricing
  - Weekend nights: Fri, Sat, plus Sunday night before a holiday Monday.
  - Jacuzzi per use, only with Zharyna. Pets per stay. Cleaning once per stay
    per house (guest pays; excluded from Anya's bonus base).
  - Max stay per property (MaxStay column). Min stay: holiday row value, else default.
  - Booking types: airbnb = commission on listed subtotal (list = net / (1-fee));
    cash / monobank / stripe = processing fee on subtotal.
  - Anya's bonus: 20% of gross profit = (nightly + extras + jacuzzi + pets) - fees.
"""

import csv, io, time, urllib.request
from datetime import date, timedelta
from dataclasses import dataclass, field

RULES_CSV_URL = ("https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRtiq-ZYM3TcBWonG0Kbn3ew1qCkC_HE8pt3Z4nHr0FXfBbD0YlJOONZIOwapiuxt2_Zfd2KNtLbn6t"
    "/pub?output=csv")
CACHE_TTL_SECONDS = 600

FALLBACK_RULES = {
    "properties": {
        "tseglina": dict(name="Tseglina", weekday=100, weekend=200, base_cap=5, extra_fee=25, max_guests=6, cleaning=100, max_stay=7),
        "modryna":  dict(name="Modryna",  weekday=150, weekend=225, base_cap=4, extra_fee=25, max_guests=7, cleaning=100, max_stay=10),
        "zharyna":  dict(name="Zharyna",  weekday=100, weekend=200, base_cap=5, extra_fee=25, max_guests=6, cleaning=100, max_stay=7),
    },
    "holidays": [
        (date(2026,8,24),  date(2026,8,24),  "Independence Day", 2.0, 2),
        (date(2026,10,1),  date(2026,10,1),  "Defenders Day", 2.0, 2),
        (date(2026,12,24), date(2026,12,25), "Christmas", 2.0, 3),
        (date(2026,12,30), date(2027,1,2),   "New Year (super-peak)", 4.0, 3),
        (date(2027,1,6),   date(2027,1,7),   "Old-calendar Christmas", 2.0, 2),
        (date(2027,2,14),  date(2027,2,14),  "Valentine's Day", 2.0, 2),
        (date(2027,3,8),   date(2027,3,8),   "Women's Day", 2.0, 2),
        (date(2027,4,30),  date(2027,5,2),   "Orthodox Easter + May 1", 2.0, 3),
        (date(2027,5,8),   date(2027,5,9),   "Remembrance Day weekend", 2.0, 3),
        (date(2027,6,19),  date(2027,6,20),  "Trinity weekend", 2.0, 3),
        (date(2027,6,28),  date(2027,6,28),  "Constitution Day", 2.0, 2),
        (date(2027,7,15),  date(2027,7,15),  "Statehood Day", 2.0, 2),
        (date(2027,8,24),  date(2027,8,24),  "Independence Day", 2.0, 2),
        (date(2027,10,1),  date(2027,10,1),  "Defenders Day", 2.0, 2),
        (date(2027,12,24), date(2027,12,25), "Christmas", 2.0, 3),
        (date(2027,12,30), date(2028,1,2),   "New Year (super-peak)", 4.0, 3),
    ],
    "modifiers": {
        "jacuzzi_fee": 100.0, "pet_fee": 25.0, "default_min_stay": 2,
        "holiday_multiplier_base": "applicable_rate",
    },
}

BOOKING_TYPES = {
    "airbnb":   dict(fee=0.155, commission=True,  label="Airbnb (15.5% commission)"),
    "cash":     dict(fee=0.0,   commission=False, label="Cash"),
    "monobank": dict(fee=0.013, commission=False, label="Site — UA card (1.3%)"),
    "stripe":   dict(fee=0.055, commission=False, label="Site — Int'l card (5.5%)"),
}
BONUS_PCT = 0.20

# ---------- Sheet loading & parsing ----------

_cache = {"rules": None, "ts": 0.0, "source": "none"}

def _num(s, default=None):
    try:
        return float(str(s).replace("$", "").replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return default

def parse_rules_csv(text: str) -> dict:
    """Parse the published sheet. Sections start with '## '. Tolerant of extra
    columns, blank rows, and the EXAMPLE holiday row."""
    props, holidays, modifiers = {}, [], {}
    section, headers = None, None
    for row in csv.reader(io.StringIO(text)):
        first = (row[0] if row else "").strip()
        if first.startswith("## "):
            section, headers = first[3:].strip().upper(), None
            continue
        if not any(c.strip() for c in row):
            continue
        if section and section.startswith("PROPERTIES"):
            if first == "PropertyID":
                headers = [h.strip() for h in row]; continue
            if headers and first:
                d = dict(zip(headers, row))
                pid = d.get("PropertyID", "").strip().lower()
                if not pid: continue
                props[pid] = dict(
                    name=d.get("Name", pid).strip(),
                    weekday=_num(d.get("WeekdayRate")), weekend=_num(d.get("WeekendRate")),
                    base_cap=int(_num(d.get("BaseCapacity"), 0)),
                    extra_fee=_num(d.get("ExtraGuestFee"), 0),
                    max_guests=int(_num(d.get("MaxGuests"), 0)),
                    cleaning=_num(d.get("CleaningFee"), 0),
                    max_stay=int(_num(d.get("MaxStay"), 365)),
                    photo=d.get("PhotoURL", "").strip(),
                )
        elif section and section.startswith("HOLIDAY"):
            if first == "StartDate":
                headers = [h.strip() for h in row]; continue
            if headers and first:
                d = dict(zip(headers, row))
                label = d.get("Label", "").strip()
                if "EXAMPLE" in label.upper(): continue
                try:
                    start = date.fromisoformat(d.get("StartDate", "").strip())
                    end = date.fromisoformat(d.get("EndDate", "").strip())
                except ValueError:
                    continue
                mult = _num(d.get("Multiplier"), 1.0)
                min_stay = int(_num(d.get("MinStay"), 0) or 0)
                holidays.append((start, end, label, mult, min_stay))
        elif section and (section.startswith("MODIFIERS") or section.startswith("BONUS")):
            if first == "Key": continue
            key = first.strip().lower()
            if key and len(row) > 1:
                raw = row[1].strip()
                modifiers[key] = _num(raw) if _num(raw) is not None else raw
    if not props or not holidays:
        raise ValueError(f"Sheet parse incomplete: {len(props)} properties, {len(holidays)} holidays")
    return {"properties": props, "holidays": holidays, "modifiers": modifiers}

def load_rules(force_refresh=False) -> dict:
    now = time.time()
    if not force_refresh and _cache["rules"] and now - _cache["ts"] < CACHE_TTL_SECONDS:
        return _cache["rules"]
    try:
        with urllib.request.urlopen(RULES_CSV_URL, timeout=6) as r:
            rules = parse_rules_csv(r.read().decode("utf-8"))
        _cache.update(rules=rules, ts=now, source="sheet")
    except Exception:
        if not _cache["rules"]:
            _cache.update(rules=FALLBACK_RULES, ts=now, source="fallback")
        # else: keep serving last good sheet rules past TTL
    return _cache["rules"]

def rules_source() -> str:
    return _cache["source"]

# ---------- Engine ----------

def holiday_for(d: date, rules):
    for start, end, label, mult, min_stay in rules["holidays"]:
        if start <= d <= end:
            return dict(label=label, mult=mult, min_stay=min_stay)
    return None

def is_weekend_night(d: date, rules):
    if d.weekday() in (4, 5):
        return True, "Fri/Sat"
    if d.weekday() == 6 and holiday_for(d + timedelta(days=1), rules):
        return True, "Sunday before holiday Monday"
    return False, None

def night_price(prop_id: str, d: date, rules):
    p = rules["properties"][prop_id]
    mult_base = str(rules["modifiers"].get("holiday_multiplier_base", "applicable_rate")).strip().lower()
    weekend, wk_reason = is_weekend_night(d, rules)
    base = p["weekend"] if weekend else p["weekday"]
    base_label = (f"weekend rate ${p['weekend']:g}" + (f" ({wk_reason})" if wk_reason and wk_reason != "Fri/Sat" else "")) if weekend else f"weekday rate ${p['weekday']:g}"
    hol = holiday_for(d, rules)
    if hol:
        if mult_base == "weekday_rate":
            price = p["weekday"] * hol["mult"]
            reason = f"weekday rate ${p['weekday']:g} x {hol['mult']:g} [{hol['label']}]"
        else:
            price = base * hol["mult"]
            reason = f"{base_label} x {hol['mult']:g} [{hol['label']}]"
    else:
        price, reason = base, base_label
    return price, reason, hol

@dataclass
class Quote:
    lines: list = field(default_factory=list)
    subtotal: float = 0.0
    rental_price: float = 0.0
    cleaning_total: float = 0.0
    airbnb_listing_price: float = None
    airbnb_fee: float = 0.0
    cc_fee: float = 0.0
    gross_profit: float = 0.0
    anya_bonus: float = 0.0
    min_stay_required: int = 2
    rules_source: str = ""
    errors: list = field(default_factory=list)

def quote(check_in: date, check_out: date, bookings: dict, jacuzzi_uses: int = 0,
          pets: int = 0, booking_type: str = "cash", rules: dict = None) -> Quote:
    q = Quote()
    if booking_type not in BOOKING_TYPES:
        q.errors.append(f"Unknown booking type: {booking_type}"); return q
    rules = rules or load_rules()
    q.rules_source = rules_source() or "provided"
    mods = rules["modifiers"]
    jacuzzi_fee = _num(mods.get("jacuzzi_fee"), 100.0)
    pet_fee = _num(mods.get("pet_fee"), 25.0)
    default_min_stay = int(_num(mods.get("default_min_stay"), 2))

    nights = (check_out - check_in).days
    if nights <= 0:
        q.errors.append("check_out must be after check_in"); return q
    if jacuzzi_uses and "zharyna" not in bookings:
        q.errors.append("Jacuzzi requires renting Zharyna (it is on its terrace)")
    for pid, guests in bookings.items():
        if pid not in rules["properties"]:
            q.errors.append(f"Unknown property: {pid}"); return q
        p = rules["properties"][pid]
        if guests > p["max_guests"]:
            q.errors.append(f"{p['name']}: {guests} guests exceeds max {p['max_guests']}")
        if nights > p["max_stay"]:
            q.errors.append(f"{p['name']}: {nights} nights exceeds maximum stay of {p['max_stay']}")
    if q.errors: return q

    q.min_stay_required = default_min_stay
    for i in range(nights):
        hol = holiday_for(check_in + timedelta(days=i), rules)
        if hol and hol["min_stay"]:
            q.min_stay_required = max(q.min_stay_required, hol["min_stay"])
    if nights < q.min_stay_required:
        q.errors.append(f"Minimum stay for these dates is {q.min_stay_required} nights (requested {nights})")
        return q

    nightly_total = extra_total = 0.0
    for pid, guests in bookings.items():
        p = rules["properties"][pid]
        extra_guests = max(0, guests - p["base_cap"])
        for i in range(nights):
            d = check_in + timedelta(days=i)
            price, reason, _hol = night_price(pid, d, rules)
            nightly_total += price
            line = f"{p['name']} {d.isoformat()} ({d.strftime('%a')}): ${price:g} — {reason}"
            if extra_guests:
                ex = extra_guests * p["extra_fee"]
                extra_total += ex
                line += f" + ${ex:g} extra guests ({extra_guests} x ${p['extra_fee']:g})"
            q.lines.append(line)
        q.cleaning_total += p["cleaning"]
        q.lines.append(f"{p['name']}: cleaning fee ${p['cleaning']:g} (once per stay)")

    jacuzzi_total = jacuzzi_uses * jacuzzi_fee
    if jacuzzi_total:
        q.lines.append(f"Jacuzzi: {jacuzzi_uses} use(s) x ${jacuzzi_fee:g} = ${jacuzzi_total:g}")
    pet_total = pets * pet_fee
    if pet_total:
        q.lines.append(f"Pets: {pets} x ${pet_fee:g} per stay = ${pet_total:g}")

    q.rental_price = nightly_total + extra_total + jacuzzi_total + pet_total
    q.subtotal = q.rental_price + q.cleaning_total

    bt = BOOKING_TYPES[booking_type]
    if bt["commission"]:
        q.airbnb_listing_price = round(q.subtotal / (1 - bt["fee"]), 2)
        q.airbnb_fee = round(q.subtotal * bt["fee"], 2)
        q.lines.append(f"{bt['label']}: list at ${q.airbnb_listing_price:,.2f} to net ${q.subtotal:,.2f}")
        q.gross_profit = q.rental_price - q.airbnb_fee
    else:
        q.cc_fee = round(q.subtotal * bt["fee"], 2)
        if q.cc_fee:
            q.lines.append(f"{bt['label']}: processing fee ${q.cc_fee:,.2f}")
        q.gross_profit = q.rental_price - q.cc_fee

    q.anya_bonus = round(q.gross_profit * BONUS_PCT, 2)
    return q
