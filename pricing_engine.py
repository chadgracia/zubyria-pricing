"""
Gracia/Zubyria Properties — Pricing Engine v0.1
Reads rules exported from the Google Sheet (Zubyria Pricing Rules v0.2).
Rules encoded per Chad's decisions of 2026-08-02:
  - Holiday multiplier applies to the APPLICABLE rate for that night
    (weekend rate on Fri/Sat, weekday otherwise), i.e. "multiplies all".
  - Weekend nights: Fri, Sat, plus Sunday night before a holiday Monday.
  - Jacuzzi $100 PER USE, only with Zharyna.
  - Cleaning $100 once per stay per house. Guest pays it; excluded from gross profit.
  - Min stay: holiday row MinStay if any night is in that range, else default 2.
  - Airbnb: 15.5% COMMISSION on the listed subtotal (fee deducted from payout).
    Listing price to net X = X / (1 - 0.155).
  - Payment fees: cash 0%, monobank 1.3%, stripe 5.5% (estimate). Applied to
    direct bookings only; Airbnb processes its own payments.
  - Anya's bonus: 20% of gross profit =
    (nightly + extra guests + jacuzzi) - airbnb fee - cc fee.
"""

from datetime import date, timedelta
from dataclasses import dataclass, field

# ---------- Rules (mirrors the sheet; later: parsed from sheet export) ----------

PROPERTIES = {
    "tseglina": dict(name="Tseglina", weekday=100, weekend=200, base_cap=5, extra_fee=25, max_guests=6, cleaning=100),
    "modryna":  dict(name="Modryna",  weekday=150, weekend=225, base_cap=4, extra_fee=25, max_guests=7, cleaning=100),
    "zharyna":  dict(name="Zharyna",  weekday=100, weekend=200, base_cap=5, extra_fee=25, max_guests=6, cleaning=100),
}

HOLIDAYS = [  # (start, end, label, multiplier, min_stay)
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
]

JACUZZI_FEE = 100          # per use, requires zharyna
DEFAULT_MIN_STAY = 2
AIRBNB_FEE_PCT = 0.155     # commission on listed subtotal
PAYMENT_FEES = {"cash": 0.0, "monobank": 0.013, "stripe": 0.055}
BONUS_PCT = 0.20

# ---------- Engine ----------

def holiday_for(d: date):
    for start, end, label, mult, min_stay in HOLIDAYS:
        if start <= d <= end:
            return dict(label=label, mult=mult, min_stay=min_stay)
    return None

def is_weekend_night(d: date):
    """Fri/Sat night, plus Sunday night before a holiday Monday."""
    if d.weekday() in (4, 5):  # Fri=4, Sat=5
        return True, "Fri/Sat"
    if d.weekday() == 6 and holiday_for(d + timedelta(days=1)):
        return True, "Sunday before holiday Monday"
    return False, None

def night_price(prop_id: str, d: date):
    p = PROPERTIES[prop_id]
    weekend, wk_reason = is_weekend_night(d)
    base = p["weekend"] if weekend else p["weekday"]
    base_label = f"weekend rate ${p['weekend']}" + (f" ({wk_reason})" if wk_reason and wk_reason != "Fri/Sat" else "") if weekend else f"weekday rate ${p['weekday']}"
    hol = holiday_for(d)
    if hol:
        price = base * hol["mult"]
        reason = f"{base_label} x {hol['mult']:g} [{hol['label']}]"
    else:
        price = base
        reason = base_label
    return price, reason, hol

@dataclass
class Quote:
    lines: list = field(default_factory=list)
    subtotal: float = 0.0          # everything the guest pays (direct price)
    rental_price: float = 0.0      # nightly + extra guests + jacuzzi (bonus base)
    cleaning_total: float = 0.0
    airbnb_listing_price: float = None
    airbnb_fee: float = 0.0
    cc_fee: float = 0.0
    gross_profit: float = 0.0
    anya_bonus: float = 0.0
    min_stay_required: int = DEFAULT_MIN_STAY
    errors: list = field(default_factory=list)

def quote(check_in: date, check_out: date, bookings: dict, jacuzzi_uses: int = 0,
          channel: str = "direct", payment: str = "cash") -> Quote:
    """
    bookings: {property_id: guest_count}
    channel: 'direct' | 'airbnb'
    payment: 'cash' | 'monobank' | 'stripe'  (ignored for airbnb)
    """
    q = Quote()
    nights = (check_out - check_in).days
    if nights <= 0:
        q.errors.append("check_out must be after check_in"); return q
    if jacuzzi_uses and "zharyna" not in bookings:
        q.errors.append("Jacuzzi requires renting Zharyna (it is on its terrace)")
    for pid, guests in bookings.items():
        p = PROPERTIES[pid]
        if guests > p["max_guests"]:
            q.errors.append(f"{p['name']}: {guests} guests exceeds max {p['max_guests']}")
    if q.errors: return q

    # min stay: strictest holiday touched by any night
    for i in range(nights):
        hol = holiday_for(check_in + timedelta(days=i))
        if hol: q.min_stay_required = max(q.min_stay_required, hol["min_stay"])
    if nights < q.min_stay_required:
        q.errors.append(f"Minimum stay for these dates is {q.min_stay_required} nights (requested {nights})")
        return q

    nightly_total = extra_total = 0.0
    for pid, guests in bookings.items():
        p = PROPERTIES[pid]
        extra_guests = max(0, guests - p["base_cap"])
        for i in range(nights):
            d = check_in + timedelta(days=i)
            price, reason, _ = night_price(pid, d)
            nightly_total += price
            line = f"{p['name']} {d.isoformat()} ({d.strftime('%a')}): ${price:g} — {reason}"
            if extra_guests:
                ex = extra_guests * p["extra_fee"]
                extra_total += ex
                line += f" + ${ex} extra guests ({extra_guests} x ${p['extra_fee']})"
            q.lines.append(line)
        q.cleaning_total += p["cleaning"]
        q.lines.append(f"{p['name']}: cleaning fee ${p['cleaning']} (once per stay)")

    jacuzzi_total = jacuzzi_uses * JACUZZI_FEE
    if jacuzzi_total:
        q.lines.append(f"Jacuzzi: {jacuzzi_uses} use(s) x ${JACUZZI_FEE} = ${jacuzzi_total}")

    q.rental_price = nightly_total + extra_total + jacuzzi_total
    q.subtotal = q.rental_price + q.cleaning_total

    if channel == "airbnb":
        q.airbnb_listing_price = round(q.subtotal / (1 - AIRBNB_FEE_PCT), 2)
        q.airbnb_fee = round(q.subtotal * AIRBNB_FEE_PCT, 2)  # fee if listed at direct price (no gross-up)
        q.lines.append(f"Airbnb: list at ${q.airbnb_listing_price:,.2f} to net ${q.subtotal:,.2f} (15.5% commission)")
        q.gross_profit = q.rental_price - q.airbnb_fee
    else:
        pct = PAYMENT_FEES[payment]
        q.cc_fee = round(q.subtotal * pct, 2)
        if q.cc_fee:
            q.lines.append(f"Payment ({payment}): processing fee {pct:.1%} = ${q.cc_fee:,.2f}")
        q.gross_profit = q.rental_price - q.cc_fee

    q.anya_bonus = round(q.gross_profit * BONUS_PCT, 2)
    return q

def print_quote(q: Quote, title=""):
    print(f"\n=== {title} ===")
    if q.errors:
        for e in q.errors: print("ERROR:", e)
        return
    for l in q.lines: print(" ", l)
    print(f"  Guest pays (direct): ${q.subtotal:,.2f}")
    if q.airbnb_listing_price: print(f"  Airbnb listing price: ${q.airbnb_listing_price:,.2f}")
    print(f"  Gross profit: ${q.gross_profit:,.2f}   Anya bonus (20%): ${q.anya_bonus:,.2f}")
