"""
Zubyria Pricing — Lambda handler.
GET  /                   -> HTML quote form (renders results when query params present)
GET  /quote.json         -> JSON quote — for Telegram bot / AI concierge
GET  /admin              -> Admin page (requires ?key= or zadmin cookie)
GET  /availability.json  -> Public availability dict (blocked_by stripped for non-admin)
"""
import json
from datetime import date
from pricing_engine import quote, load_rules, Quote

ADMIN_SECRET = "zubyria$admin!7kQ2mXf9pLw4"

# Module-level store; None = lazy-init to real DynamoDB.
# Tests override this directly: lambda_function._store = FakeStore(...)
_store = None


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
    return checkin, checkout, bookings, jacuzzi, pets, btype


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
                 avail=None, block_url=None):
    pf = prefill or {}
    kp = f"&key={admin_key}" if admin_key else ""
    key_hidden = f'<input type="hidden" name="key" value="{html_esc(admin_key)}">' if admin_key else ""

    tab_block_href = f"/admin?tab=block{kp}"
    tab_price_href = f"/admin?tab=price{kp}"
    tab_nav = (
        f'<nav class="tab-nav">'
        f'<a href="{tab_block_href}" class="{"active" if tab == "block" else ""}">Block</a>'
        f'<a href="{tab_price_href}" class="{"active" if tab == "price" else ""}">Price</a>'
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
    else:
        today = date.today().isoformat()
        upcoming = sorted(
            [b for b in blocks if b.get("status") == "active" and b.get("checkout", "") >= today],
            key=lambda b: b.get("checkin", ""),
        )

        blocks_html = ""
        if upcoming:
            rows = ""
            for b in upcoming:
                houses_str = ", ".join(props.get(h, {}).get("name", h) for h in b.get("houses", []))
                cancel_url = f"/admin?tab=block&action=cancel_block&block_id={html_esc(b['sk'])}{kp}"
                rows += (
                    f'<li style="margin-bottom:8px">'
                    f'<b>{html_esc(b.get("label","?"))}</b>: {html_esc(houses_str)}, '
                    f'{html_esc(b.get("checkin",""))} → {html_esc(b.get("checkout",""))}'
                    f' &nbsp;<a href="{cancel_url}" style="color:var(--err);font-size:12px">[cancel]</a>'
                    f'</li>'
                )
            blocks_html = f'<ul style="padding-left:20px;margin:8px 0 0">{rows}</ul>'
        else:
            blocks_html = '<p style="color:var(--dim);margin:8px 0 0">No upcoming blocks.</p>'

        house_checks = ""
        for h, p in props.items():
            checked = "checked" if pf.get(f"h_{h}") == "on" else ""
            house_checks += (
                f'<label style="margin-right:16px">'
                f'<input type="checkbox" name="h_{h}" {checked}> {html_esc(p["name"])}'
                f'</label>'
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

        dates_val = html_esc(pf.get("dates", ""))
        label_val = html_esc(pf.get("label", ""))
        back_href = f"/?key={admin_key}" if admin_key else "/"

        block_form = (
            f'{msg_html}'
            f'<form method="get" action="/admin">'
            f'<input type="hidden" name="tab" value="block">'
            f'<input type="hidden" name="action" value="add_block">'
            f'{key_hidden}'
            f'<fieldset><legend>Block dates</legend><div class="row">'
            f'<label>Stay <input type="text" id="admin_dates" name="dates" '
            f'placeholder="check-in → check-out" value="{dates_val}" required style="min-width:240px">'
            f'</label></div></fieldset>'
            f'<fieldset><legend>Houses</legend><div class="row">{house_checks}</div></fieldset>'
            f'<fieldset><legend>Guest / reason</legend>'
            f'<input type="text" name="label" value="{label_val}" '
            f'placeholder="Guest name or reason" required style="min-width:260px">'
            f'</fieldset>'
            f'<button type="submit">Block dates</button>'
            f'<a href="{html_esc(back_href)}" class="btn-sec" style="margin-left:10px">← Calculator</a>'
            f'</form>'
            f'<div class="result" style="margin-top:26px">'
            f'<h2>Upcoming blocks</h2>'
            f'{blocks_html}'
            f'</div>'
        )
        main_content = block_form

    flatpickr_init = (
        '<script>flatpickr("#admin_dates",{mode:"range",dateFormat:"Y-m-d",minDate:"today",showMonths:2});</script>'
        if tab == "block" else
        f'<script>{AVAIL_JS}</script>'
    )

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


# ---------- Lambda handler ----------

def lambda_handler(event, context):
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
        msg = ""
        prefill = dict(qs)

        # Block actions (only relevant in block tab)
        if action == "add_block":
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
                r = _get_store().add_block(houses, ci, co, label, created_by="admin")
                if r["ok"]:
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
                price_checkin, price_checkout, price_bookings, jacuzzi, pets, btype = parse_params(qs, props)
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
                                    jacuzzi_uses=jacuzzi, pets=pets, booking_type=btype)
            except (ValueError, KeyError) as e:
                price_error = f"Check your inputs: {e}"

            # "Block these dates" link → pre-fills the BLOCK tab
            if q_price and not q_price.errors and price_checkin and price_checkout:
                houses_qs = "&".join(f"h_{h}=on" for h in price_bookings)
                date_str = qs.get("dates", f"{price_checkin.isoformat()} to {price_checkout.isoformat()}")
                kp = f"&key={ADMIN_SECRET}" if via_key else ""
                price_block_url = f"/admin?tab=block&dates={date_str}&{houses_qs}{kp}"

        try:
            blocks = _get_store().list_blocks()
        except Exception:
            blocks = []

        admin_key = ADMIN_SECRET if via_key else ""
        resp = _resp(200, render_admin(
            props, blocks, msg=msg, prefill=prefill, admin_key=admin_key,
            tab=tab, q=q_price, price_error=price_error, price_params=dict(qs),
            avail=price_avail, block_url=price_block_url,
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
        checkin, checkout, bookings, jacuzzi, pets, btype = parse_params(qs, props)

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

        q = quote(checkin, checkout, bookings, jacuzzi_uses=jacuzzi, pets=pets, booking_type=btype)

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
