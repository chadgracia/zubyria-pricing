"""
Zubyria Pricing — Lambda handler.
GET  /            -> HTML quote form (renders results when query params present)
GET  /quote.json  -> JSON quote (same params) — for future Telegram bot / AI concierge
"""
import json
from datetime import date
from urllib.parse import parse_qs
from pricing_engine import quote, load_rules, Quote

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
.house{display:flex;flex-direction:column;gap:8px;background:var(--bg);border:1px solid #3d4741;border-radius:6px;padding:10px;width:190px}
.house img{width:100%;height:100px;object-fit:cover;border-radius:4px}
.nophoto{width:100%;height:100px;border-radius:4px;background:#333c36;display:flex;align-items:center;justify-content:center;font-size:34px;color:var(--dim)}
.houserow{display:flex;align-items:center;gap:8px}
button{background:var(--accent);color:#221c10;border:0;border-radius:4px;padding:10px 26px;
font:bold 14px Verdana,sans-serif;letter-spacing:.06em;cursor:pointer}
button:hover{filter:brightness(1.1)}
.result{margin-top:26px;background:var(--panel);border:1px solid #333c36;border-radius:6px;padding:20px}
.result h2{font-size:15px;font-family:Verdana,sans-serif;letter-spacing:.14em;text-transform:uppercase;
color:var(--dim);font-weight:normal;margin:0 0 12px}
.line{font:13px/1.7 'Courier New',monospace;white-space:pre-wrap}
.tot{margin-top:14px;border-top:1px solid #3d4741;padding-top:12px;font-size:17px}
.tot b{color:var(--accent)}
.err{color:var(--err);font-weight:bold}
.bonus{color:var(--ok)}
"""

def render_page(props: dict, params=None, q: Quote = None, error=None):
    v = params or {}
    def val(k, d=""): return v.get(k, d)
    def chk(k): return "checked" if v.get(k) == "on" else ""
    def sel(k, opt, d=None): return "selected" if v.get(k, d) == opt else ""
    house_rows = ""
    for h in props:
        p = props[h]
        opts = "".join(f'<option {"selected" if str(g)==val("g_"+h, str(p["base_cap"])) else ""}>{g}</option>'
                       for g in range(1, p["max_guests"] + 1))
        photo = f'<img src="{p["photo"]}" alt="{p["name"]}" loading="lazy">' if p.get("photo") else '<div class="nophoto">{}</div>'.format(p["name"][0])
        house_rows += f'''<div class="house">{photo}<div class="houserow"><input type="checkbox" name="h_{h}" id="h_{h}" {chk("h_"+h)}>
        <label for="h_{h}" style="margin:0">{p["name"]}</label>
        <select name="g_{h}" title="guests">{opts}</select><span style="color:var(--dim);font-size:12px">guests</span></div></div>'''
    result_html = ""
    if error:
        result_html = f'<div class="result"><p class="err">{error}</p></div>'
    elif q:
        if q.errors:
            body = "".join(f'<p class="err">{e}</p>' for e in q.errors)
        else:
            lines = "\n".join(q.lines)
            abnb = f'<br>List on Airbnb at <b>${q.airbnb_listing_price:,.2f}</b> to net this amount' if q.airbnb_listing_price else ""
            fee = f'<br>Channel/payment fees: ${(q.airbnb_fee + q.cc_fee):,.2f}' if (q.airbnb_fee or q.cc_fee) else ""
            body = (f'<div class="line">{lines}</div>'
                    f'<div class="tot">Guest pays: <b>${q.subtotal:,.2f}</b>{abnb}{fee}'
                    f'<br>Gross profit: ${q.gross_profit:,.2f} &nbsp;·&nbsp; '
                    f'<span class="bonus">Anya\'s bonus (20%): ${q.anya_bonus:,.2f}</span></div>'
                    f'<div style="color:var(--dim);font-size:11px;margin-top:8px;font-family:Verdana">rules: {q.rules_source}</div>')
        result_html = f'<div class="result"><h2>Quote &amp; reasoning</h2>{body}</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zubyria Reservation Calculator</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/themes/dark.min.css">
<style>{CSS}</style></head><body><div class="wrap">
<h1>Zubyria <b>Reservation Calculator</b></h1>
<div class="sub">Three houses · banya · jacuzzi — internal quote tool</div>
<form method="get" action="/">
<fieldset><legend>Dates</legend><div class="row">
<label>Stay <input type="text" id="dates" name="dates" placeholder="check-in → check-out" value="{val('dates')}" required style="min-width:240px"></label>
</div></fieldset>
<fieldset><legend>Houses &amp; guests</legend><div class="row">{house_rows}</div></fieldset>
<fieldset><legend>Extras &amp; channel</legend><div class="row">
<label>Jacuzzi uses <input type="number" name="jacuzzi" min="0" value="{val('jacuzzi','0')}"></label>
<label>Pets <input type="number" name="pets" min="0" value="{val('pets','0')}"></label>
<label>Booking type <select name="btype">
<option value="cash" {sel('btype','cash','cash')}>Cash (0%)</option>
<option value="airbnb" {sel('btype','airbnb')}>Airbnb (15.5%)</option>
<option value="monobank" {sel('btype','monobank')}>Website — Monobank (1.3%)</option>
<option value="stripe" {sel('btype','stripe')}>Website — Stripe (~5.5%)</option></select></label>
</div></fieldset>
<button type="submit">Calculate price</button>
</form>{result_html}</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.js"></script>
<script>
flatpickr('#dates', {{mode:'range', dateFormat:'Y-m-d', minDate:'today', showMonths:2}});
</script></body></html>"""

def lambda_handler(event, context):
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}
    props = load_rules()["properties"]
    wants_json = path.rstrip("/").endswith("quote.json")
    if not (qs.get("checkin") or qs.get("dates")):
        if wants_json:
            return _resp(400, {"error": "checkin, checkout required; h_<house>=on, g_<house>, jacuzzi, pets, btype"}, json_=True)
        return _resp(200, render_page(props))
    try:
        checkin, checkout, bookings, jacuzzi, pets, btype = parse_params(qs, props)
        if not bookings:
            raise ValueError("Select at least one house")
        q = quote(checkin, checkout, bookings, jacuzzi_uses=jacuzzi, pets=pets, booking_type=btype)
    except (ValueError, KeyError) as e:
        if wants_json: return _resp(400, {"error": str(e)}, json_=True)
        return _resp(200, render_page(props, qs, error=f"Check your inputs: {e}"))
    if wants_json:
        return _resp(200, quote_to_dict(q), json_=True)
    return _resp(200, render_page(props, qs, q=q))

def _resp(status, body, json_=False):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json" if json_ else "text/html; charset=utf-8"},
            "body": json.dumps(body) if json_ else body}
