"""
Zubyria Pricing — Lambda handler.
GET  /                   -> HTML quote form (renders results when query params present)
GET  /quote.json         -> JSON quote — for Telegram bot / AI concierge
GET  /admin              -> Admin page (requires ?key= or zadmin cookie)
GET  /availability.json  -> Public availability dict (blocked_by stripped for non-admin)
"""
import json
import urllib.parse
from datetime import date, timedelta
from pricing_engine import quote, load_rules, Quote, holiday_for

ADMIN_SECRET = "zubyria$admin!7kQ2mXf9pLw4"

# Module-level store; None = lazy-init to real DynamoDB.
# Tests override this directly: lambda_function._store = FakeStore(...)
_store = None

_BLOCK_COLORS = ["#4e7a5b", "#4a6080", "#7a5a4a", "#6a4f80", "#3d7070"]

# Tape-chart day column width in px. Must match .tape-dc in CSS — bar geometry is
# computed off it, so the two cannot drift apart.
_TAPE_CELL_W = 34

# Diagonal slant width in px for the check-in / check-out bar edges. Fixed px, never a
# percentage — a percentage slant eats a short bar's whole body.
_TAPE_SLANT = 9


def _d(s):
    return date.fromisoformat(s)


def _block_color(sk):
    return _BLOCK_COLORS[hash(sk) % len(_BLOCK_COLORS)]


def _prev_month(y, m):
    m -= 1
    if m == 0:
        return y - 1, 12
    return y, m


def _next_month_ym(y, m):
    m += 1
    if m == 13:
        return y + 1, 1
    return y, m


def _month_end(y, m):
    ny, nm = _next_month_ym(y, m)
    return date(ny, nm, 1) - timedelta(days=1)


def _get_store():
    global _store
    if _store is None:
        from reservations import get_store
        _store = get_store()
    return _store


def _cookies(event):
    out = {}
    for c in (event.get("cookies") or []):
        if "=" in c:
            k, _, v = c.partition("=")
            out[k.strip()] = v.strip()
    return out


def is_admin(event):
    qs = event.get("queryStringParameters") or {}
    if qs.get("key") == ADMIN_SECRET:
        return True
    return _cookies(event).get("zadmin") == ADMIN_SECRET


def _via_key(event):
    qs = event.get("queryStringParameters") or {}
    return qs.get("key") == ADMIN_SECRET


def _admin_cookie():
    return f"zadmin={ADMIN_SECRET}; Max-Age=31536000; Path=/; HttpOnly"


def html_esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------- CSS ----------

CSS = """
:root{--bg:#1d2320;--panel:#262e29;--ink:#e8e4da;--dim:#9aa69d;--accent:#d8a24a;--err:#e07856;--ok:#8fb98a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 Georgia,'Times New Roman',serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;font-weight:normal;letter-spacing:.04em;margin:0 0 2px}
h1 b{color:var(--accent);font-weight:normal}
.sub{color:var(--dim);font-size:13px;margin-bottom:26px;font-family:Verdana,sans-serif}
form{background:var(--panel);border:1px solid #333c36;border-radius:6px;padding:20px}
fieldset{border:0;padding:0;margin:0 0 16px}
legend{font-family:Verdana,sans-serif;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
label{display:inline-block;margin-right:18px;font-size:15px}
input[type=text],select,input[type=number]{background:var(--bg);color:var(--ink);border:1px solid #3d4741;
border-radius:4px;padding:7px 9px;font:14px Verdana,sans-serif}
input[type=number]{width:70px}
.row{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.house{display:flex;flex-direction:column;gap:8px;background:var(--bg);border:1px solid #3d4741;border-radius:6px;padding:10px;width:230px}
.house img{width:100%;height:130px;object-fit:cover;border-radius:4px}
.nophoto{width:100%;height:130px;border-radius:4px;background:#333c36;display:flex;align-items:center;justify-content:center;font-size:34px;color:var(--dim)}
.houserow{display:flex;align-items:center;gap:8px}
button{background:var(--accent);color:#221c10;border:0;border-radius:4px;padding:10px 26px;
font:bold 14px Verdana,sans-serif;letter-spacing:.06em;cursor:pointer}
button:hover{filter:brightness(1.1)}
.btn-sec{background:transparent;color:var(--dim);border:1px solid #3d4741;border-radius:4px;padding:10px 26px;
font:14px Verdana,sans-serif;letter-spacing:.06em;cursor:pointer;text-decoration:none;display:inline-block}
.btn-sec:hover{color:var(--ink);border-color:#9aa69d}
.result{margin-top:26px;background:var(--panel);border:1px solid #333c36;border-radius:6px;padding:20px}
.result h2{font-size:15px;font-family:Verdana,sans-serif;letter-spacing:.14em;text-transform:uppercase;
color:var(--dim);font-weight:normal;margin:0 0 12px}
.line{font:13px/1.7 'Courier New',monospace;white-space:pre-wrap}
.tot{margin-top:14px;border-top:1px solid #3d4741;padding-top:12px;font-size:17px}
.tot b{color:var(--accent)}
.err{color:var(--err);font-weight:bold}
.bonus{color:var(--ok)}
.tab-nav{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid #333c36}
.tab-nav a{padding:8px 22px;font:13px Verdana,sans-serif;text-decoration:none;color:var(--dim);
border:1px solid transparent;border-bottom:0;margin-bottom:-1px;border-radius:4px 4px 0 0}
.tab-nav a.active{color:var(--accent);border-color:#333c36;background:var(--panel)}
.tab-nav a:not(.active):hover{color:var(--ink)}
.tape-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 0 20px}
.tape-tbl{border-collapse:collapse;table-layout:fixed}
.tape-tbl th,.tape-tbl td{padding:0;border:1px solid #2a322d;vertical-align:middle}
.tape-lh{width:80px;min-width:80px;position:sticky;left:0;z-index:6;background:var(--panel);
font:11px Verdana,sans-serif;color:var(--dim);padding:4px 6px;text-align:left}
.tape-lc{width:80px;min-width:80px;position:sticky;left:0;z-index:5;background:var(--bg);
font:12px Verdana,sans-serif;color:var(--ink);padding:4px 6px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.tape-dh{min-width:34px;width:34px;text-align:center;font:10px Verdana,sans-serif;
color:var(--dim);padding:2px 0}
.tape-dc{min-width:34px;width:34px;max-width:34px;height:28px;padding:1px;position:relative}
.tape-we{background:#1a2320}
.tape-today{outline:2px solid var(--accent);outline-offset:-1px}
.tape-hl{background:#252515}
.tape-dn{font-size:11px;font-weight:bold;line-height:1.2}
.tape-dw{font-size:9px;color:var(--dim);line-height:1}
.tape-hm{font-size:7px;color:var(--accent);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;line-height:1}
/* ONE absolutely-positioned bar per block per house lane — never per-cell fragments,
   so a label can never widen a day column. Geometry (checkout 10:00, check-in 16:00):
   left = 0.5 * cell width into the check-in cell, width = nights * cell width, which
   lands the right edge on the check-out cell's midpoint.

   z-index:2 is load-bearing. Day cells are position:relative with z-index:auto, so a
   later cell that carries a background (.tape-we / .tape-hl) would otherwise paint
   over a bar overflowing from an earlier cell — which truncated bars at a cell edge
   and swallowed the trailing diagonal. Bars must sit above the whole cell layer, and
   the sticky label column above them again (z-index 5/6).

   clip-path is emitted inline per bar: the slant is a fixed px width (never a
   percentage — percentages collapse short bars) and is clamped on narrow bars so the
   unclipped body stays visible down to a single night. */
.tape-bar{position:absolute;top:3px;height:22px;border-radius:2px;overflow:hidden;
white-space:nowrap;text-overflow:ellipsis;font:10px/22px Verdana,sans-serif;color:#fff;
padding:0 9px;text-decoration:none;box-sizing:border-box;z-index:2}
.tape-bar:hover{filter:brightness(1.15)}
.tape-bar-nolabel{font-size:0;padding:0}
"""

# Flatpickr range script with live availability greying (not an f-string to avoid brace escaping)
AVAIL_JS = """
flatpickr('#dates', {
  mode:'range', dateFormat:'Y-m-d', minDate:'today', showMonths:2,
  onClose: function(dates) {
    if (dates.length !== 2) return;
    var ci = dates[0].toISOString().slice(0,10);
    var co = dates[1].toISOString().slice(0,10);
    fetch('/availability.json?checkin='+ci+'&checkout='+co)
      .then(function(r){return r.json();})
      .then(function(av){
        document.querySelectorAll('.house').forEach(function(card){
          var cb = card.querySelector('input[type=checkbox]');
          if (!cb) return;
          var hid = cb.name.replace('h_','');
          var info = av[hid];
          var avl = !info || info.available !== false;
          cb.disabled = !avl;
          if (!avl) cb.checked = false;
          card.style.opacity = avl ? '1' : '0.45';
          var note = card.querySelector('.booked-note');
          if (!avl) {
            if (!note) {
              note = document.createElement('span');
              note.className = 'booked-note';
              note.style.cssText = 'color:var(--err);font-size:11px;font-family:Verdana;display:block';
              card.appendChild(note);
            }
            note.textContent = 'booked';
          } else if (note) { note.textContent = ''; }
        });
      }).catch(function(){});
  }
});
"""

FLATPICKR_CSS = (
    '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.css">'
    '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/themes/dark.min.css">'
)
FLATPICKR_JS = '<script src="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.js"></script>'


# ---------- HTML rendering ----------

def parse_params(qs: dict, props: dict):
    p = {k: v for k, v in (qs or {}).items()}
    if p.get("dates") and not p.get("checkin"):
        parts = [s.strip() for s in p["dates"].replace(" to ", "|").replace(" — ", "|").split("|")]
        if len(parts) != 2:
            raise ValueError("Select both check-in and check-out dates on the calendar")
        p["checkin"], p["checkout"] = parts
    checkin = date.fromisoformat(p["checkin"])
    checkout = date.fromisoformat(p["checkout"])
    bookings = {}
    for h in props:
        if p.get(f"h_{h}") == "on":
            bookings[h] = int(p.get(f"g_{h}", props[h]["base_cap"]))
    jacuzzi = int(p.get("jacuzzi", 0) or 0)
    pets = int(p.get("pets", 0) or 0)
    btype = p.get("btype", "cash")
    ev_raw = p.get("event", "")
    event_use = ev_raw in ("on", "1", "true")
    event_guests = int(p.get("event_guests", 0) or 0)
    return checkin, checkout, bookings, jacuzzi, pets, btype, event_use, event_guests


def quote_to_dict(q: Quote):
    return {
        "errors": q.errors, "lines": q.lines,
        "guest_pays_direct": q.subtotal,
        "airbnb_listing_price": q.airbnb_listing_price,
        "rental_price": q.rental_price, "cleaning_total": q.cleaning_total,
        "airbnb_fee": q.airbnb_fee, "cc_fee": q.cc_fee,
        "gross_profit": q.gross_profit, "anya_bonus": q.anya_bonus,
        "min_stay_required": q.min_stay_required,
        "rules_source": q.rules_source,
    }


def _calc_form_html(props: dict, v: dict, avail: dict, form_action: str,
                    extra_inputs: str, q, error, block_url, show_internal: bool,
                    reset_href: str = None):
    """Returns calculator form + optional result HTML. No page wrapper."""
    avail = avail or {}
    reset_href = reset_href or form_action

    def val(k, d=""): return v.get(k, d)
    def chk(k): return "checked" if v.get(k) == "on" else ""
    def sel(k, opt, d=None): return "selected" if v.get(k, d) == opt else ""

    house_rows = ""
    for h in props:
        p = props[h]
        unavail = h in avail and not avail[h]["available"]
        opts = "".join(
            f'<option {"selected" if str(g)==val("g_"+h, str(p["base_cap"])) else ""}>{g}</option>'
            for g in range(1, p["max_guests"] + 1)
        )
        photo = (f'<img src="{p["photo"]}" alt="{p["name"]}" loading="lazy">'
                 if p.get("photo")
                 else '<div class="nophoto">{}</div>'.format(p["name"][0]))
        card_style = ' style="opacity:0.45"' if unavail else ''
        disabled = ' disabled' if unavail else ''
        booked = ('<span class="booked-note" style="color:var(--err);font-size:11px;'
                  'font-family:Verdana;display:block">booked</span>') if unavail else ''
        house_rows += (
            f'<div class="house"{card_style}>{photo}'
            f'<div class="houserow">'
            f'<input type="checkbox" name="h_{h}" id="h_{h}" {chk("h_"+h)}{disabled}>'
            f'<label for="h_{h}" style="margin:0">{p["name"]}</label>'
            f'<select name="g_{h}" title="guests">{opts}</select>'
            f'<span style="color:var(--dim);font-size:12px">guests</span>'
            f'</div>{booked}</div>'
        )

    result_html = ""
    if error:
        result_html = f'<div class="result"><p class="err">{html_esc(error)}</p></div>'
    elif q:
        if q.errors:
            body = "".join(f'<p class="err">{html_esc(e)}</p>' for e in q.errors)
        else:
            lines = "\n".join(q.lines)
            abnb = (f'<br>List on Airbnb at <b>${q.airbnb_listing_price:,.2f}</b> to net ${q.subtotal:,.2f}'
                    if q.airbnb_listing_price else "")
            fee = (f'<br>Channel/payment fees: ${(q.airbnb_fee + q.cc_fee):,.2f}'
                   if (q.airbnb_fee or q.cc_fee) else "")
            admin_link = (
                f'<p style="margin-top:8px"><a href="{html_esc(block_url)}" '
                f'style="color:var(--dim);font-size:13px;font-family:Verdana">'
                f'→ Block these dates in admin</a></p>'
                if block_url else ""
            )
            internal = (
                f'<br>Gross profit: ${q.gross_profit:,.2f} &nbsp;·&nbsp; '
                f'<span class="bonus">Anya\'s bonus (20%): ${q.anya_bonus:,.2f}</span>'
            ) if show_internal else ""
            body = (
                f'<div class="tot" style="border-top:0;margin-top:0;padding-top:0">'
                f'Guest pays: <b>${q.subtotal:,.2f}</b>{abnb}{fee}'
                f'{internal}</div>'
                f'<details style="margin-top:14px"><summary style="cursor:pointer;color:var(--dim);'
                f'font:12px Verdana">Show reasoning</summary>'
                f'<div class="line" style="margin-top:8px">{lines}</div></details>'
                f'<div style="color:var(--dim);font-size:11px;margin-top:8px;font-family:Verdana">'
                f'rules: {q.rules_source} &nbsp;·&nbsp; '
                f'<a href="/?refresh_rules=1" style="color:var(--dim)">refresh rules</a></div>'
                f'{admin_link}'
            )
        result_html = f'<div class="result"><h2>Quote &amp; reasoning</h2>{body}</div>'

    return (
        f'<form method="get" action="{html_esc(form_action)}">'
        f'{extra_inputs}'
        f'<fieldset><legend>Dates</legend><div class="row">'
        f'<label>Stay <input type="text" id="dates" name="dates" '
        f'placeholder="check-in → check-out" value="{val("dates")}" required style="min-width:240px"></label>'
        f'</div></fieldset>'
        f'<fieldset><legend>Houses &amp; guests</legend><div class="row">{house_rows}</div></fieldset>'
        f'<fieldset><legend>Extras &amp; channel</legend><div class="row">'
        f'<label>Jacuzzi uses <input type="number" name="jacuzzi" min="0" value="{val("jacuzzi","0")}"></label>'
        f'<label>Pets <input type="number" name="pets" min="0" value="{val("pets","0")}"></label>'
        f'<label>Event <input type="checkbox" name="event" id="event"'
        f'{"  checked" if v.get("event") in ("on","1","true") else ""}></label>'
        f'<label>Event guests <input type="number" name="event_guests" min="0" value="{val("event_guests","0")}"></label>'
        f'<label>Booking type <select name="btype">'
        f'<option value="cash" {sel("btype","cash","cash")}>Cash (0%)</option>'
        f'<option value="airbnb" {sel("btype","airbnb")}>Airbnb (15.5%)</option>'
        f'<option value="monobank" {sel("btype","monobank")}>Site — UA card (1.3%)</option>'
        f'<option value="stripe" {sel("btype","stripe")}>Site — Int\'l card (5.5%)</option>'
        f'</select></label>'
        f'</div></fieldset>'
        f'<button type="submit">Calculate price</button>'
        f'<a href="{html_esc(reset_href)}" class="btn-sec" style="margin-left:10px">Reset</a>'
        f'</form>{result_html}'
    )


def _render_tape_chart(props, blocks, win_start, win_end, today_date, rules,
                        detail_block=None, admin_key=""):
    kp = f"&key={admin_key}" if admin_key else ""
    n_days = (win_end - win_start).days + 1
    window_days = [win_start + timedelta(days=i) for i in range(n_days)]
    WD = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

    # Header row
    header = '<th class="tape-lh"></th>'
    for d in window_days:
        hol = holiday_for(d, rules)
        cls = "tape-dh"
        if d.weekday() >= 5:
            cls += " tape-we"
        if d == today_date:
            cls += " tape-today"
        if hol:
            cls += " tape-hl"
        ttl = f' title="{html_esc(hol["label"])}"' if hol else ""
        hm = (f'<div class="tape-hm" title="{html_esc(hol["label"])}">'
              f'{html_esc(hol["label"][:6])}</div>') if hol else ""
        header += (
            f'<th class="{cls}"{ttl}>'
            f'<div class="tape-dn">{d.day}</div>'
            f'<div class="tape-dw">{WD[d.weekday()]}</div>'
            f'{hm}</th>'
        )

    # Filter active blocks intersecting window.
    # Include bo == win_start so checkout-day left-half bars render at window edge.
    # Checkout 10:00 / checkin 16:00
    active = []
    for b in blocks:
        if b.get("status") != "active":
            continue
        try:
            bc, bo = _d(b["checkin"]), _d(b["checkout"])
        except (KeyError, ValueError):
            continue
        if bo >= win_start and bc <= win_end:
            active.append(b)

    # House rows. One absolutely-positioned bar per block, anchored in the cell of its
    # first visible day; geometry is px math off the fixed cell width, so a bar's label
    # can never widen a day column. Occupied-night cells keep the tape-occ marker.
    rows_html = ""
    for house_id, hp in props.items():
        anchored = {}   # day → list of bar HTML, emitted inside that day's <td>
        occupied = set()  # nights the house is booked (checkin .. checkout-1)

        for b in active:
            if house_id not in b.get("houses", []):
                continue
            bc, bo = _d(b["checkin"]), _d(b["checkout"])

            clipped_start = bc < win_start  # block began before the visible window
            clipped_end = bo > win_end      # checkout falls beyond the visible window

            night = max(bc, win_start)
            last_night = min(bo - timedelta(days=1), win_end)
            while night <= last_night:
                occupied.add(night)
                night += timedelta(days=1)

            anchor = win_start if clipped_start else bc
            if not (win_start <= anchor <= win_end):
                continue

            # Half-cell shift at the check-in edge; flat at a clipped window boundary.
            # left is (checkin_col_index + 0.5) * cell width, expressed relative to the
            # anchor cell that hosts the bar, so it never rounds to a whole cell.
            left_off = 0 if clipped_start else _TAPE_CELL_W // 2
            if clipped_end:
                right_off = (win_end - anchor).days * _TAPE_CELL_W + _TAPE_CELL_W
            else:
                # nights * cell width lands the right edge on the checkout cell midpoint
                right_off = (bo - anchor).days * _TAPE_CELL_W + _TAPE_CELL_W // 2
            width = right_off - left_off
            if width <= 0:
                continue

            # Fixed-px slant, clamped on short bars so the body never collapses.
            slant_px = min(_TAPE_SLANT, max(4, width // 4))
            if clipped_start and clipped_end:
                clip = ""            # flat at both window edges
                slant = ""
            elif clipped_start:
                clip = (f"clip-path:polygon(0 0,100% 0,"
                        f"calc(100% - {slant_px}px) 100%,0 100%);")
                slant = " tape-slant-end"
            elif clipped_end:
                clip = (f"clip-path:polygon({slant_px}px 0,100% 0,"
                        f"100% 100%,0 100%);")
                slant = " tape-slant-start"
            else:
                clip = (f"clip-path:polygon({slant_px}px 0,100% 0,"
                        f"calc(100% - {slant_px}px) 100%,0 100%);")
                slant = " tape-slant-both"

            edge = ("" if clipped_start else " tape-half-start") + \
                   ("" if clipped_end else " tape-half-end")

            # Under ~3 cells there is no room for text once the slants eat both ends.
            nolabel = width < 3 * _TAPE_CELL_W
            label = b.get("label", "?")
            detail_url = f"/admin?tab=block&details={html_esc(b['sk'])}{kp}"
            anchored.setdefault(anchor, []).append(
                f'<a class="tape-bar{slant}{edge}'
                f'{" tape-bar-nolabel" if nolabel else ""}"'
                f' href="{detail_url}"'
                f' style="background:{_block_color(b["sk"])};'
                f'left:{left_off}px;width:{width}px;{clip}"'
                f' title="{html_esc(label)}"'
                f' data-house="{html_esc(house_id)}"'
                f' data-checkin="{html_esc(b["checkin"])}"'
                f' data-checkout="{html_esc(b["checkout"])}">'
                f'{"" if nolabel else html_esc(label)}</a>'
            )

        cells = ""
        for d in window_days:
            cls = "tape-dc"
            if d.weekday() >= 5:
                cls += " tape-we"
            if d == today_date:
                cls += " tape-today"
            if holiday_for(d, rules):
                cls += " tape-hl"
            if d in occupied:
                cls += " tape-occ"
            cells += f'<td class="{cls}">{"".join(anchored.get(d, ()))}</td>'

        rows_html += (
            f'<tr><td class="tape-lc">{html_esc(hp["name"])}</td>{cells}</tr>'
        )

    legend = (
        '<p style="color:var(--dim);font-size:11px;margin:4px 0 16px">'
        'bar starts mid-day&nbsp;=&nbsp;check-in 4pm&nbsp;&nbsp;·&nbsp;&nbsp;'
        'ends mid-day&nbsp;=&nbsp;check-out 10am</p>'
    )
    return (
        f'<div class="tape-wrap">'
        f'<table class="tape-tbl">'
        f'<thead><tr>{header}</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
        f'</div>'
        f'{legend}'
    )


def render_page(props: dict, params=None, q: Quote = None, error=None,
                avail=None, block_url=None):
    v = params or {}
    form_html = _calc_form_html(props, v, avail, "/", "", q, error, block_url,
                                show_internal=False)
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Zubyria Reservation Calculator</title>'
        f'{FLATPICKR_CSS}'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        f'<h1>Zubyria <b>Reservation Calculator</b></h1>'
        f'<div class="sub">Three houses · banya · jacuzzi</div>'
        f'{form_html}'
        f'</div>'
        f'{FLATPICKR_JS}'
        f'<script>{AVAIL_JS}</script>'
        f'</body></html>'
    )


def render_admin(props: dict, blocks: list, msg="", prefill=None, admin_key="",
                 tab="block", q=None, price_error=None, price_params=None,
                 avail=None, block_url=None, month_str="", detail_block=None,
                 confirm_cancel=False):
    pf = prefill or {}
    kp = f"&key={admin_key}" if admin_key else ""
    key_hidden = f'<input type="hidden" name="key" value="{html_esc(admin_key)}">' if admin_key else ""

    tab_block_href = f"/admin?tab=block{kp}"
    tab_price_href = f"/admin?tab=price{kp}"
    tab_bonus_href = f"/admin?tab=bonus{kp}"
    tab_nav = (
        f'<nav class="tab-nav">'
        f'<a href="{tab_block_href}" class="{"active" if tab == "block" else ""}">Block</a>'
        f'<a href="{tab_price_href}" class="{"active" if tab == "price" else ""}">Price</a>'
        f'<a href="{tab_bonus_href}" class="{"active" if tab == "bonus" else ""}">Bonus</a>'
        f'</nav>'
    )

    if tab == "price":
        price_extra = f'<input type="hidden" name="tab" value="price">{key_hidden}'
        reset_href = f"/admin?tab=price{kp}"
        price_html = _calc_form_html(
            props, price_params or {}, avail, "/admin",
            price_extra, q, price_error, block_url,
            show_internal=True, reset_href=reset_href,
        )
        main_content = price_html

    elif tab == "bonus":
        # Parse month
        if month_str:
            try:
                bonus_month_date = date.fromisoformat(month_str + "-01")
            except ValueError:
                bonus_month_date = date.today().replace(day=1)
        else:
            bonus_month_date = date.today().replace(day=1)
        bm_y, bm_m = bonus_month_date.year, bonus_month_date.month
        month_end_day = _month_end(bm_y, bm_m)

        py_b, pm_b = _prev_month(bm_y, bm_m)
        ny_b, nm_b = _next_month_ym(bm_y, bm_m)
        month_label_b = bonus_month_date.strftime("%B %Y")
        bonus_month_nav = (
            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
            f'<a href="/admin?tab=bonus&month={py_b}-{pm_b:02d}{kp}" class="btn-sec" style="padding:4px 12px">← Prev</a>'
            f'<span style="font:13px Verdana,sans-serif;color:var(--dim)">{html_esc(month_label_b)}</span>'
            f'<a href="/admin?tab=bonus&month={ny_b}-{nm_b:02d}{kp}" class="btn-sec" style="padding:4px 12px">Next →</a>'
            f'</div>'
        )

        month_start_iso = bonus_month_date.isoformat()
        month_end_iso = month_end_day.isoformat()
        month_blocks = [
            b for b in blocks
            if b.get("status") == "active"
            and month_start_iso <= b.get("checkin", "") <= month_end_iso
        ]
        priced = [b for b in month_blocks if b.get("snapshot")]
        unpriced = [b for b in month_blocks if not b.get("snapshot")]
        total_bonus = sum(b["snapshot"].get("bonus", 0) for b in priced)

        if priced:
            tbl_s = 'style="border-collapse:collapse;width:100%;font:13px Verdana,sans-serif"'
            th_s = 'style="text-align:left;padding:8px 12px;border-bottom:1px solid #333c36;color:var(--dim);font-size:11px;letter-spacing:.1em;text-transform:uppercase"'
            rows_b = ""
            for b in priced:
                hs = ", ".join(props.get(h, {}).get("name", h) for h in b.get("houses", []))
                snap = b["snapshot"]
                rows_b += (
                    f'<tr>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #2a322d">{html_esc(b.get("label","?"))}</td>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #2a322d">{html_esc(b.get("checkin",""))} → {html_esc(b.get("checkout",""))}</td>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #2a322d">{html_esc(hs)}</td>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #2a322d">{html_esc(b.get("btype","—"))}</td>'
                    f'<td style="padding:8px 12px;border-bottom:1px solid #2a322d">${snap.get("subtotal",0):,.2f}</td>'
                    f'<td class="bonus" style="padding:8px 12px;border-bottom:1px solid #2a322d">${snap.get("bonus",0):,.2f}</td>'
                    f'</tr>'
                )
            priced_html = (
                f'<div style="overflow-x:auto"><table {tbl_s}>'
                f'<thead><tr>'
                f'<th {th_s}>Label</th><th {th_s}>Dates</th><th {th_s}>Houses</th>'
                f'<th {th_s}>Type</th><th {th_s}>Guest pays</th><th {th_s}>Anya\'s bonus</th>'
                f'</tr></thead><tbody>{rows_b}</tbody>'
                f'<tfoot><tr>'
                f'<td colspan="5" style="padding:10px 12px;font-weight:bold">Total bonus</td>'
                f'<td class="bonus" style="padding:10px 12px;font-weight:bold">${total_bonus:,.2f}</td>'
                f'</tr></tfoot></table></div>'
            )
        else:
            priced_html = f'<p style="color:var(--dim)">No priced blocks in {html_esc(month_label_b)}.</p>'

        if unpriced:
            up_items = "".join(
                f'<li>{html_esc(b.get("label","?"))}: {html_esc(b.get("checkin",""))} → {html_esc(b.get("checkout",""))}</li>'
                for b in unpriced
            )
            unpriced_html = (
                f'<div style="margin-top:20px">'
                f'<h2 style="font:13px Verdana,sans-serif;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin-bottom:8px">Unpriced blocks (no snapshot)</h2>'
                f'<ul style="padding-left:20px;margin:0;font-size:13px">{up_items}</ul>'
                f'</div>'
            )
        else:
            unpriced_html = ""

        main_content = (
            f'<h2 style="font:16px Verdana,sans-serif;margin:0 0 16px">Anya\'s Bonus — {html_esc(month_label_b)}</h2>'
            f'{bonus_month_nav}'
            f'<div class="result">{priced_html}{unpriced_html}</div>'
        )

    else:
        # BLOCK tab
        if month_str:
            try:
                month_date = date.fromisoformat(month_str + "-01")
            except ValueError:
                month_date = date.today().replace(day=1)
        else:
            month_date = date.today().replace(day=1)
        ny, nm = _next_month_ym(month_date.year, month_date.month)
        win_start = month_date
        win_end = _month_end(ny, nm)
        today_date = date.today()

        py, pm = _prev_month(month_date.year, month_date.month)
        ny2, nm2 = _next_month_ym(month_date.year, month_date.month)
        prev_href = f"/admin?tab=block&month={py}-{pm:02d}{kp}"
        next_href = f"/admin?tab=block&month={ny2}-{nm2:02d}{kp}"
        window_label = f'{month_date.strftime("%B %Y")} – {date(ny, nm, 1).strftime("%B %Y")}'
        month_nav = (
            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
            f'<a href="{prev_href}" class="btn-sec" style="padding:4px 12px">← Prev</a>'
            f'<span style="font:13px Verdana,sans-serif;color:var(--dim)">{window_label}</span>'
            f'<a href="{next_href}" class="btn-sec" style="padding:4px 12px">Next →</a>'
            f'</div>'
        )

        rules = load_rules()
        tape_html = _render_tape_chart(
            props, blocks, win_start, win_end, today_date, rules,
            detail_block=detail_block, admin_key=admin_key,
        )

        # Details panel (shown when a tape bar is clicked)
        if detail_block:
            b = detail_block
            houses_str = ", ".join(props.get(h, {}).get("name", h) for h in b.get("houses", []))
            try:
                nights = (_d(b["checkout"]) - _d(b["checkin"])).days
            except (KeyError, ValueError):
                nights = 0
            nights_label = f"{nights} night{'s' if nights != 1 else ''}"
            created_at_short = (b.get("created_at") or "")[:10]

            # Build Edit URL (pre-fills the block form)
            edit_parts = [
                "tab=block",
                f"edit_id={urllib.parse.quote(b['sk'], safe='')}",
                f"dates={urllib.parse.quote(b['checkin'] + ' to ' + b['checkout'], safe='')}",
            ]
            for h in b.get("houses", []):
                edit_parts.append(f"h_{h}=on")
            edit_parts.append(f"label={urllib.parse.quote(b.get('label', ''), safe='')}")
            if b.get("btype"):
                edit_parts.append(f"btype={urllib.parse.quote(b['btype'], safe='')}")
                qp = b.get("quote_params") or {}
                for h2 in props:
                    if isinstance(qp.get("houses"), dict) and h2 in qp["houses"]:
                        edit_parts.append(f"g_{h2}={qp['houses'][h2]}")
                if qp.get("jacuzzi"):
                    edit_parts.append(f"jacuzzi={qp['jacuzzi']}")
                if qp.get("pets"):
                    edit_parts.append(f"pets={qp['pets']}")
                if qp.get("event"):
                    edit_parts.append("event=on")
                    edit_parts.append(f"event_guests={qp.get('event_guests', 0)}")
            if admin_key:
                edit_parts.append(f"key={urllib.parse.quote(admin_key, safe='')}")
            edit_url = "/admin?" + "&".join(edit_parts)

            # Financial section
            from pricing_engine import BOOKING_TYPES as _BT
            snap = b.get("snapshot")
            if snap:
                btype_key = b.get("btype", "cash")
                bt_info = _BT.get(btype_key, {})
                bt_label = bt_info.get("label", btype_key)
                subtotal = snap.get("subtotal", 0)
                gross_profit = snap.get("gross_profit", 0)
                bonus = snap.get("bonus", 0)
                fee_rate = bt_info.get("fee", 0.0)
                fee_amount = round(subtotal * fee_rate, 2) if fee_rate else 0.0
                fee_html = ""
                if fee_amount:
                    fee_label = "Airbnb commission" if bt_info.get("commission") else "Processing fee"
                    fee_html = f'<br>{html_esc(fee_label)}: ${fee_amount:,.2f}'
                finance_html = (
                    f'<div class="tot" style="border-top:0;margin-top:8px;padding-top:0">'
                    f'<span style="color:var(--dim);font-size:11px;font-family:Verdana">'
                    f'{html_esc(bt_label)}</span><br>'
                    f'Guest pays: <b>${subtotal:,.2f}</b>{fee_html}'
                    f'<br>Gross profit: ${gross_profit:,.2f} &nbsp;·&nbsp; '
                    f'<span class="bonus">Anya\'s bonus (20%): ${bonus:,.2f}</span>'
                    f'</div>'
                )
                # Quote params summary
                qp = b.get("quote_params") or {}
                if qp:
                    qp_parts = []
                    if isinstance(qp.get("houses"), dict):
                        for h, g in qp["houses"].items():
                            qp_parts.append(f"{props.get(h, {}).get('name', h)}: {g} guests")
                    if qp.get("jacuzzi"):
                        qp_parts.append(f"Jacuzzi: {qp['jacuzzi']} uses")
                    if qp.get("pets"):
                        qp_parts.append(f"Pets: {qp['pets']}")
                    if qp.get("event"):
                        qp_parts.append(f"Event, {qp.get('event_guests', 0)} guests")
                    if qp_parts:
                        finance_html += (
                            f'<div style="margin-top:6px;font-size:12px;font-family:Verdana;'
                            f'color:var(--dim)">Priced for: {html_esc(", ".join(qp_parts))}</div>'
                        )
            else:
                finance_html = (
                    f'<p style="color:var(--dim);font-size:13px;font-family:Verdana;margin:8px 0 0">'
                    f'No pricing recorded</p>'
                )

            # Two-step cancel: first click shows confirm_cancel on same page;
            # second click (Confirm cancel) triggers the actual cancel action.
            cancel_step1_url = (
                f"/admin?tab=block&details={urllib.parse.quote(b['sk'], safe='')}"
                f"&confirm_cancel=1{kp}"
            )
            cancel_step2_url = (
                f"/admin?tab=block&action=cancel_block"
                f"&block_id={urllib.parse.quote(b['sk'], safe='')}{kp}"
            )
            if confirm_cancel:
                cancel_btn = (
                    f'<a href="{html_esc(cancel_step2_url)}" class="btn-sec" '
                    f'style="color:var(--err);border-color:var(--err)">Confirm cancel</a>'
                )
            else:
                cancel_btn = (
                    f'<a href="{html_esc(cancel_step1_url)}" class="btn-sec" '
                    f'style="color:var(--err);border-color:var(--err)">Cancel</a>'
                )

            done_url = f"/admin?tab=block{kp}"
            confirm_html = (
                f'<div class="result" style="margin-bottom:20px">'
                f'<h2>Details</h2>'
                f'<p style="margin-bottom:4px"><b>{html_esc(b.get("label","?"))}</b><br>'
                f'Houses: {html_esc(houses_str)}<br>'
                f'{html_esc(b.get("checkin",""))} → {html_esc(b.get("checkout",""))}'
                f' ({html_esc(nights_label)})<br>'
                f'Created by: {html_esc(b.get("created_by",""))}'
                f'{(" · " + html_esc(created_at_short)) if created_at_short else ""}</p>'
                f'{finance_html}'
                f'<div style="border-top:1px solid #2a322d;margin-top:14px;padding-top:12px;'
                f'display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
                f'<a href="{html_esc(edit_url)}" class="btn-sec">Edit</a>'
                f'{cancel_btn}'
                f'<a href="{html_esc(done_url)}" class="btn-sec">Done</a>'
                f'</div>'
                f'</div>'
            )
        else:
            confirm_html = ""

        today_iso = today_date.isoformat()
        upcoming = sorted(
            [b for b in blocks if b.get("status") == "active" and b.get("checkout", "") >= today_iso],
            key=lambda b: b.get("checkin", ""),
        )
        if upcoming:
            rows = ""
            for b in upcoming:
                houses_str = ", ".join(props.get(h, {}).get("name", h) for h in b.get("houses", []))
                det_url = f"/admin?tab=block&details={urllib.parse.quote(b['sk'], safe='')}{kp}"
                rows += (
                    f'<li style="margin-bottom:8px">'
                    f'<b>{html_esc(b.get("label","?"))}</b>: {html_esc(houses_str)}, '
                    f'{html_esc(b.get("checkin",""))} → {html_esc(b.get("checkout",""))}'
                    f' &nbsp;<a href="{det_url}" style="color:var(--dim);font-size:12px">[details]</a>'
                    f'</li>'
                )
            blocks_html = f'<ul style="padding-left:20px;margin:8px 0 0">{rows}</ul>'
        else:
            blocks_html = '<p style="color:var(--dim);margin:8px 0 0">No upcoming blocks.</p>'

        # House checkboxes — support prefill for edit / "Block these dates" flow
        house_checks = ""
        for h, hp in props.items():
            h_checked = " checked" if pf.get(f"h_{h}") == "on" else ""
            house_checks += (
                f'<label style="margin-right:16px">'
                f'<input type="checkbox" name="h_{h}"{h_checked}> {html_esc(hp["name"])}'
                f'</label>'
            )

        # Pricing expander (optional snapshot capture)
        guest_inputs = ""
        for h, hp in props.items():
            gv = html_esc(pf.get(f"g_{h}", str(hp["base_cap"])))
            guest_inputs += (
                f'<label style="margin-right:12px">{html_esc(hp["name"])} guests '
                f'<input type="number" name="g_{h}" min="1" max="{hp["max_guests"]}" value="{gv}" style="width:50px">'
                f'</label>'
            )
        has_btype = bool(pf.get("btype"))
        popen = " open" if has_btype else ""
        btype_opts = ""
        for bt_id, bt_lbl in [("", "No pricing"), ("cash", "Cash"), ("airbnb", "Airbnb (15.5%)"),
                                ("monobank", "Site — UA card (1.3%)"), ("stripe", "Site — Int'l card (5.5%)")]:
            sel_a = " selected" if pf.get("btype", "") == bt_id else ""
            btype_opts += f'<option value="{bt_id}"{sel_a}>{bt_lbl}</option>'
        ev_chk = " checked" if pf.get("event") in ("on", "1", "true") else ""
        pricing_expander = (
            f'<details{popen} style="margin-top:12px">'
            f'<summary style="cursor:pointer;color:var(--dim);font:12px Verdana">Pricing (optional — for bonus tracking)</summary>'
            f'<div style="margin-top:10px">'
            f'<div class="row" style="margin-bottom:10px">'
            f'<label>Booking type <select name="btype">{btype_opts}</select></label>'
            f'</div>'
            f'<div class="row" style="margin-bottom:10px">{guest_inputs}</div>'
            f'<div class="row">'
            f'<label>Jacuzzi uses <input type="number" name="jacuzzi" min="0" value="{html_esc(pf.get("jacuzzi","0"))}" style="width:60px"></label>'
            f'<label>Pets <input type="number" name="pets" min="0" value="{html_esc(pf.get("pets","0"))}" style="width:60px"></label>'
            f'<label>Event <input type="checkbox" name="event"{ev_chk}></label>'
            f'<label>Event guests <input type="number" name="event_guests" min="0" value="{html_esc(pf.get("event_guests","0"))}" style="width:60px"></label>'
            f'</div></div></details>'
        )

        if msg:
            ok = msg.lower().startswith("block") and "conflict" not in msg.lower()
            msg_color = "var(--ok)" if ok else "var(--err)"
            msg_html = (
                f'<div class="result"><p style="margin:0;color:{msg_color}">'
                f'{html_esc(msg)}</p></div>'
            )
        else:
            msg_html = ""

        edit_id = pf.get("edit_id", "")
        edit_hidden = f'<input type="hidden" name="edit_id" value="{html_esc(edit_id)}">' if edit_id else ""
        block_btn = "Update block" if edit_id else "Block dates"
        dates_val = html_esc(pf.get("dates", ""))
        label_val = html_esc(pf.get("label", ""))
        back_href = f"/?key={admin_key}" if admin_key else "/"

        main_content = (
            f'{confirm_html}'
            f'{month_nav}'
            f'{tape_html}'
            f'{msg_html}'
            f'<form method="get" action="/admin">'
            f'<input type="hidden" name="tab" value="block">'
            f'<input type="hidden" name="action" value="add_block">'
            f'{key_hidden}'
            f'{edit_hidden}'
            f'<fieldset><legend>{"Edit block" if edit_id else "Block dates"}</legend><div class="row">'
            f'<label>Stay <input type="text" id="admin_dates" name="dates" '
            f'placeholder="check-in → check-out" value="{dates_val}" required style="min-width:240px">'
            f'</label></div></fieldset>'
            f'<fieldset><legend>Houses</legend><div class="row">{house_checks}</div></fieldset>'
            f'<fieldset><legend>Guest / reason</legend>'
            f'<input type="text" name="label" value="{label_val}" '
            f'placeholder="Guest name or reason" required style="min-width:260px">'
            f'{pricing_expander}</fieldset>'
            f'<button type="submit">{block_btn}</button>'
            f'<a href="{html_esc(back_href)}" class="btn-sec" style="margin-left:10px">← Calculator</a>'
            f'</form>'
            f'<div class="result" style="margin-top:26px">'
            f'<h2>Upcoming blocks</h2>'
            f'{blocks_html}'
            f'</div>'
        )

    if tab == "block":
        flatpickr_init = '<script>flatpickr("#admin_dates",{mode:"range",dateFormat:"Y-m-d",minDate:"today",showMonths:2});</script>'
    elif tab == "price":
        flatpickr_init = f'<script>{AVAIL_JS}</script>'
    else:
        flatpickr_init = ""

    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Zubyria Admin</title>'
        f'{FLATPICKR_CSS}'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        f'<h1>Zubyria <b>Admin</b></h1>'
        f'<div class="sub">Date blocks &amp; reservations</div>'
        f'{tab_nav}'
        f'{main_content}'
        f'</div>'
        f'{FLATPICKR_JS}'
        f'{flatpickr_init}'
        f'</body></html>'
    )


def _access_denied():
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        '<h1>Zubyria <b>Admin</b></h1>'
        '<p style="color:var(--err)">Access denied.</p>'
        '<a href="/" class="btn-sec">← Calculator</a>'
        '</div></body></html>'
    )


def _error_page():
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        '<h1>Zubyria <b>Error</b></h1>'
        '<p class="err">An internal error occurred. Please try again.</p>'
        '<a href="/" class="btn-sec">← Calculator</a>'
        '</div></body></html>'
    )


# ---------- Lambda handler ----------

def _lambda_handler(event, context):
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}
    admin = is_admin(event)
    via_key = _via_key(event)

    # Refresh rules cache
    if qs.get("refresh_rules"):
        load_rules(force_refresh=True)
        return _resp(200, render_page(load_rules()["properties"]))

    props = load_rules()["properties"]
    wants_json = path.rstrip("/").endswith("quote.json")

    # ── /admin ──────────────────────────────────────────────────────────────
    if path.rstrip("/") == "/admin":
        if not admin:
            return _resp(200, _access_denied())

        tab = qs.get("tab", "block")
        action = qs.get("action", "")
        month_str = qs.get("month", "")
        msg = ""
        prefill = dict(qs)

        # Block actions (only relevant in block tab)
        if action == "add_block":
            edit_id = qs.get("edit_id", "").strip()
            dates_str = qs.get("dates", "")
            houses = [h for h in props if qs.get(f"h_{h}") == "on"]
            label = qs.get("label", "").strip()
            try:
                parts = [s.strip() for s in
                         dates_str.replace(" to ", "|").replace(" — ", "|").split("|")]
                if len(parts) != 2:
                    raise ValueError("Select a date range")
                if not label:
                    raise ValueError("Label is required")
                if not houses:
                    raise ValueError("Select at least one house")
                ci = date.fromisoformat(parts[0])
                co = date.fromisoformat(parts[1])
                # Snapshot computation if btype provided
                snapshot = None
                quote_params_out = None
                snap_btype = qs.get("btype", "").strip()
                if snap_btype and snap_btype in ("cash", "airbnb", "monobank", "stripe"):
                    try:
                        snap_bookings = {h: int(qs.get(f"g_{h}", props[h]["base_cap"])) for h in houses}
                        snap_jac = int(qs.get("jacuzzi", 0) or 0)
                        snap_pets = int(qs.get("pets", 0) or 0)
                        snap_event = qs.get("event") in ("on", "1", "true")
                        snap_event_guests = int(qs.get("event_guests", 0) or 0)
                        snap_q = quote(ci, co, snap_bookings,
                                       jacuzzi_uses=snap_jac, pets=snap_pets,
                                       booking_type=snap_btype,
                                       event=snap_event, event_guests=snap_event_guests)
                        if not snap_q.errors:
                            snapshot = {
                                "subtotal": snap_q.subtotal,
                                "gross_profit": snap_q.gross_profit,
                                "bonus": snap_q.anya_bonus,
                            }
                            quote_params_out = {
                                "houses": snap_bookings,
                                "jacuzzi": snap_jac,
                                "pets": snap_pets,
                                "btype": snap_btype,
                                "event": snap_event,
                                "event_guests": snap_event_guests,
                            }
                    except Exception:
                        pass
                r = _get_store().add_block(
                    houses, ci, co, label,
                    created_by=f"edited from {edit_id}" if edit_id else "admin",
                    exclude_id=edit_id or None,
                    snapshot=snapshot,
                    quote_params=quote_params_out,
                    btype=snap_btype or None,
                )
                if r["ok"]:
                    if edit_id:
                        _get_store().cancel_block(edit_id)
                        msg = f"Block updated: {label} ({parts[0]} → {parts[1]})"
                    else:
                        msg = f"Block added: {label} ({parts[0]} → {parts[1]})"
                    prefill = {}
                else:
                    conflicts = ", ".join(r["conflicts"])
                    msg = f"Conflict: those dates are already blocked by: {conflicts}"
            except ValueError as e:
                msg = f"Error: {e}"
            tab = "block"

        elif action == "cancel_block":
            block_id = qs.get("block_id", "")
            if block_id:
                try:
                    _get_store().cancel_block(block_id)
                    msg = "Block cancelled."
                except Exception as e:
                    msg = f"Error cancelling: {e}"
            tab = "block"

        # Price calculation (PRICE tab)
        q_price = None
        price_error = None
        price_avail = {}
        price_block_url = None
        price_checkin = price_checkout = None
        price_bookings = {}

        if tab == "price" and (qs.get("dates") or qs.get("checkin")):
            try:
                price_checkin, price_checkout, price_bookings, jacuzzi, pets, btype, price_event_use, price_event_guests = parse_params(qs, props)
                try:
                    price_avail = _get_store().availability(price_checkin, price_checkout)
                except Exception:
                    pass
                blocked = [h for h in price_bookings if h in price_avail and not price_avail[h]["available"]]
                if blocked:
                    names = ", ".join(props[h]["name"] for h in blocked)
                    price_error = f"Check your inputs: {names} not available for those dates"
                elif not price_bookings:
                    price_error = "Check your inputs: Select at least one house"
                else:
                    q_price = quote(price_checkin, price_checkout, price_bookings,
                                    jacuzzi_uses=jacuzzi, pets=pets, booking_type=btype,
                                    event=price_event_use, event_guests=price_event_guests)
            except (ValueError, KeyError) as e:
                price_error = f"Check your inputs: {e}"

            # "Block these dates" link → pre-fills the BLOCK tab with full pricing params
            if q_price and not q_price.errors and price_checkin and price_checkout:
                houses_qs = "&".join(f"h_{h}=on" for h in price_bookings)
                guests_qs = "&".join(f"g_{h}={price_bookings[h]}" for h in price_bookings)
                date_str = qs.get("dates", f"{price_checkin.isoformat()} to {price_checkout.isoformat()}")
                kp_link = f"&key={ADMIN_SECRET}" if via_key else ""
                event_part = f"&event=on&event_guests={price_event_guests}" if price_event_use else ""
                price_block_url = (
                    f"/admin?tab=block&dates={urllib.parse.quote(date_str, safe='')}"
                    f"&{houses_qs}&{guests_qs}"
                    f"&btype={btype}&jacuzzi={jacuzzi}&pets={pets}"
                    f"{event_part}{kp_link}"
                )

        try:
            blocks = _get_store().list_blocks()
        except Exception:
            blocks = []

        # Resolve detail block (block tab only)
        detail_block = None
        detail_id = qs.get("details", "")
        confirm_cancel = bool(qs.get("confirm_cancel"))
        if detail_id and tab == "block":
            for b in blocks:
                if b.get("sk") == detail_id and b.get("status") == "active":
                    detail_block = b
                    break

        admin_key = ADMIN_SECRET if via_key else ""
        resp = _resp(200, render_admin(
            props, blocks, msg=msg, prefill=prefill, admin_key=admin_key,
            tab=tab, q=q_price, price_error=price_error, price_params=dict(qs),
            avail=price_avail, block_url=price_block_url,
            month_str=month_str, detail_block=detail_block,
            confirm_cancel=confirm_cancel,
        ))
        if via_key:
            resp["cookies"] = [_admin_cookie()]
        return resp

    # ── /availability.json ───────────────────────────────────────────────────
    if path.rstrip("/").endswith("availability.json"):
        ci, co = qs.get("checkin"), qs.get("checkout")
        if not ci or not co:
            return _resp(400, {"error": "checkin and checkout required"}, json_=True)
        try:
            checkin_d = date.fromisoformat(ci)
            checkout_d = date.fromisoformat(co)
            raw = _get_store().availability(checkin_d, checkout_d)
        except Exception as e:
            return _resp(500, {"error": str(e)}, json_=True)
        result = {}
        for h in props:
            blocked = h in raw and not raw[h]["available"]
            info = {"available": not blocked}
            if admin:
                info["blocked_by"] = raw[h]["blocked_by"] if blocked else None
            result[h] = info
        return _resp(200, result, json_=True)

    # ── / and /quote.json ────────────────────────────────────────────────────
    if not (qs.get("checkin") or qs.get("dates")):
        if wants_json:
            return _resp(400, {"error": "checkin, checkout required; h_<house>=on, g_<house>, jacuzzi, pets, btype"}, json_=True)
        return _resp(200, render_page(props))

    avail = {}
    q = None
    block_url = None
    checkin = checkout = None
    bookings = {}
    try:
        checkin, checkout, bookings, jacuzzi, pets, btype, event_use, event_guests = parse_params(qs, props)

        # Availability (best-effort; don't break pricing if DynamoDB is down)
        try:
            avail = _get_store().availability(checkin, checkout)
        except Exception:
            pass

        if not bookings:
            raise ValueError("Select at least one house")

        # Server-side block check
        blocked = [h for h in bookings if h in avail and not avail[h]["available"]]
        if blocked:
            names = ", ".join(props[h]["name"] for h in blocked)
            err = f"{names} not available for those dates"
            if wants_json:
                return _resp(400, {"error": err}, json_=True)
            return _resp(200, render_page(props, qs, error=f"Check your inputs: {err}", avail=avail))

        q = quote(checkin, checkout, bookings, jacuzzi_uses=jacuzzi, pets=pets, booking_type=btype,
                  event=event_use, event_guests=event_guests)

    except (ValueError, KeyError) as e:
        if wants_json:
            return _resp(400, {"error": str(e)}, json_=True)
        return _resp(200, render_page(props, qs, error=f"Check your inputs: {e}", avail=avail))

    if wants_json:
        return _resp(200, quote_to_dict(q), json_=True)

    # Admin "Block these dates" shortcut (public page, logged-in admin)
    if admin and q and not q.errors and checkin and checkout:
        houses_qs = "&".join(f"h_{h}=on" for h in bookings)
        date_str = qs.get("dates", f"{checkin.isoformat()} to {checkout.isoformat()}")
        kp = f"&key={ADMIN_SECRET}" if via_key else ""
        block_url = f"/admin?tab=block&dates={date_str}&{houses_qs}{kp}"

    return _resp(200, render_page(props, qs, q=q, avail=avail, block_url=block_url))


def _resp(status, body, json_=False):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json" if json_ else "text/html; charset=utf-8"},
        "body": json.dumps(body) if json_ else body,
    }


def lambda_handler(event, context):
    try:
        return _lambda_handler(event, context)
    except Exception as exc:
        import traceback
        print(f"UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
        path = (event.get("rawPath") or "/").rstrip("/")
        wants_json = path.endswith("quote.json") or path.endswith("availability.json")
        if wants_json:
            return _resp(500, {"error": str(exc) or "Internal server error"}, json_=True)
        return _resp(500, _error_page())
