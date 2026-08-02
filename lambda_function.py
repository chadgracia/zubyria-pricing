"""
Zubyria Pricing — Lambda handler.
GET  /            -> HTML quote form (renders results when query params present)
GET  /quote.json  -> JSON quote (same params) — for future Telegram bot / AI concierge
"""
import json
from datetime import date
from urllib.parse import parse_qs
from pricing_engine import quote, PROPERTIES, Quote

HOUSES = list(PROPERTIES.keys())

def parse_params(qs: dict):
    p = {k: v for k, v in (qs or {}).items()}
    checkin = date.fromisoformat(p["checkin"])
    checkout = date.fromisoformat(p["checkout"])
    bookings = {}
    for h in HOUSES:
        if p.get(f"h_{h}") == "on":
            bookings[h] = int(p.get(f"g_{h}", PROPERTIES[h]["base_cap"]))
    jacuzzi = int(p.get("jacuzzi", 0) or 0)
    channel = p.get("channel", "direct")
    payment = p.get("payment", "cash")
    return checkin, checkout, bookings, jacuzzi, channel, payment

def quote_to_dict(q: Quote):
    return {
        "errors": q.errors, "lines": q.lines,
        "guest_pays_direct": q.subtotal,
        "airbnb_listing_price": q.airbnb_listing_price,
        "rental_price": q.rental_price, "cleaning_total": q.cleaning_total,
        "airbnb_fee": q.airbnb_fee, "cc_fee": q.cc_fee,
        "gross_profit": q.gross_profit, "anya_bonus": q.anya_bonus,
        "min_stay_required": q.min_stay_required,
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
input[type=date],select,input[type=number]{background:var(--bg);color:var(--ink);border:1px solid #3d4741;
border-radius:4px;padding:7px 9px;font:14px Verdana,sans-serif}
input[type=number]{width:70px}
.row{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.house{display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid #3d4741;border-radius:4px;padding:8px 12px}
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

def render_page(params=None, q: Quote = None, error=None):
    v = params or {}
    def val(k, d=""): return v.get(k, d)
    def chk(k): return "checked" if v.get(k) == "on" else ""
    def sel(k, opt, d=None): return "selected" if v.get(k, d) == opt else ""
    house_rows = ""
    for h in HOUSES:
        p = PROPERTIES[h]
        opts = "".join(f'<option {"selected" if str(g)==val("g_"+h, str(p["base_cap"])) else ""}>{g}</option>'
                       for g in range(1, p["max_guests"] + 1))
        house_rows += f'''<div class="house"><input type="checkbox" name="h_{h}" id="h_{h}" {chk("h_"+h)}>
        <label for="h_{h}" style="margin:0">{p["name"]}</label>
        <select name="g_{h}" title="guests">{opts}</select><span style="color:var(--dim);font-size:12px">guests</span></div>'''
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
                    f'<span class="bonus">Anya\'s bonus (20%): ${q.anya_bonus:,.2f}</span></div>')
        result_html = f'<div class="result"><h2>Quote &amp; reasoning</h2>{body}</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zubyria — pricing</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Zubyria <b>pricing</b></h1>
<div class="sub">Three houses · banya · jacuzzi — internal quote tool</div>
<form method="get" action="/">
<fieldset><legend>Dates</legend><div class="row">
<label>Check-in <input type="date" name="checkin" value="{val('checkin')}" required></label>
<label>Check-out <input type="date" name="checkout" value="{val('checkout')}" required></label>
</div></fieldset>
<fieldset><legend>Houses &amp; guests</legend><div class="row">{house_rows}</div></fieldset>
<fieldset><legend>Extras &amp; channel</legend><div class="row">
<label>Jacuzzi uses <input type="number" name="jacuzzi" min="0" value="{val('jacuzzi','0')}"></label>
<label>Channel <select name="channel">
<option value="direct" {sel('channel','direct','direct')}>Direct</option>
<option value="airbnb" {sel('channel','airbnb')}>Airbnb</option></select></label>
<label>Payment <select name="payment">
<option value="cash" {sel('payment','cash','cash')}>Cash (0%)</option>
<option value="monobank" {sel('payment','monobank')}>Monobank (1.3%)</option>
<option value="stripe" {sel('payment','stripe')}>Stripe (~5.5%)</option></select></label>
</div></fieldset>
<button type="submit">Calculate price</button>
</form>{result_html}</div></body></html>"""

def lambda_handler(event, context):
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}
    wants_json = path.rstrip("/").endswith("quote.json")
    if not qs.get("checkin"):
        if wants_json:
            return _resp(400, {"error": "checkin, checkout required; h_<house>=on, g_<house>, jacuzzi, channel, payment"}, json_=True)
        return _resp(200, render_page())
    try:
        checkin, checkout, bookings, jacuzzi, channel, payment = parse_params(qs)
        if not bookings:
            raise ValueError("Select at least one house")
        q = quote(checkin, checkout, bookings, jacuzzi_uses=jacuzzi, channel=channel, payment=payment)
    except (ValueError, KeyError) as e:
        if wants_json: return _resp(400, {"error": str(e)}, json_=True)
        return _resp(200, render_page(qs, error=f"Check your inputs: {e}"))
    if wants_json:
        return _resp(200, quote_to_dict(q), json_=True)
    return _resp(200, render_page(qs, q=q))

def _resp(status, body, json_=False):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json" if json_ else "text/html; charset=utf-8"},
            "body": json.dumps(body) if json_ else body}
