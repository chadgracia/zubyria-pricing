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

# New tab names are accepted as aliases of the original query values. Emitted links
# keep the old values, so bookmarked URLs never break.
# Admins may back-date to here (retro reservations for stays already taken).
# Guests can never quote the past — their floor is today.
_RETRO_FLOOR = date(2026, 7, 1)


def _avail_js(min_date):
    """The shared range picker, floored per audience."""
    return AVAIL_JS.replace("__MINDATE__", min_date)


def _date_floor_error(d, admin):
    """Message if d is before the caller's allowed floor, else None."""
    floor = _RETRO_FLOOR if admin else date.today()
    if d >= floor:
        return None
    if admin:
        return f"earliest allowed: {_fmt_date(floor)}"
    return "dates in the past cannot be quoted"


_TAB_ALIASES = {"reservations": "block", "pricing": "price", "finances": "bonus"}

# Where the booking came from. Optional on the forms, but a submitted value must be
# one of these — a free-text channel would make any by-channel report meaningless.
_SOURCES = ("Former Guest", "Instagram", "Instagram Ad", "Airbnb", "Booking",
            "Event Guest", "Word-of-Mouth", "Threads")

_PRICED_BTYPES = ("cash", "airbnb", "monobank", "stripe")


class _Rejected(ValueError):
    """Input the server refuses outright — answered with 400, not a soft message.

    Used for the rules that must never be bypassed by a hand-made URL (a missing
    or non-positive rental fee, an unknown source). Other input problems stay
    200-with-a-message, which is what the form flow expects.
    """


def _rental_fee_from(qs):
    """The submitted rental fee as a positive float, or raise _Rejected.

    `final_price` is the old name for the same field on the Edit screen; it is
    still honoured so an in-flight URL does not lose its price.
    """
    raw = qs.get("rental_fee") if "rental_fee" in qs else qs.get("final_price")
    if raw is None or not str(raw).strip():
        raise _Rejected("Rental fee is required.")
    try:
        fee = float(str(raw).strip())
    except ValueError:
        raise _Rejected("Rental fee must be a number.")
    if fee <= 0:
        raise _Rejected("Rental fee must be greater than zero.")
    return round(fee, 2)


def _source_from(qs, original=None):
    """The submitted source, validated; carried off the edited record when absent.

    An edit that never renders the dropdown (or any programmatic re-save) must not
    silently drop the channel, so absence means "keep", exactly like payments,
    expenses and notes. A present-but-blank value is a deliberate clear.
    """
    if "source" in qs:
        src = (qs.get("source") or "").strip()
        if src and src not in _SOURCES:
            raise _Rejected(f'Unknown source "{src}".')
        return src
    return ((original or {}).get("source") or "").strip()


def _source_select(selected, name="source"):
    """The Source dropdown, shared by the add and edit forms. Blank is the default."""
    opts = ""
    for s in ("",) + _SOURCES:
        sel = " selected" if (selected or "") == s else ""
        opts += f'<option value="{html_esc(s)}"{sel}>{html_esc(s) if s else "—"}</option>'
    return f'<select name="{name}">{opts}</select>'

_BLOCK_COLORS = ["#4e7a5b", "#4a6080", "#7a5a4a", "#6a4f80", "#3d7070"]

# Tape-chart day column width in px. Must match .tape-dc in CSS — bar geometry is
# computed off it, so the two cannot drift apart.
_TAPE_CELL_W = 34

# Diagonal slant width in px for the check-in / check-out bar edges. Fixed px, never a
# percentage — a percentage slant eats a short bar's whole body.
_TAPE_SLANT = 9


def _d(s):
    return date.fromisoformat(s)


def _recompute_from_subtotal(subtotal, cleaning_total, btype):
    """Derive fees / profit / bonus for a given guest-pays total.

    Mirrors pricing_engine.quote()'s model exactly, with the computed subtotal
    swapped for whatever total is passed in. Cleaning is a fixed cost and is never
    discounted, so it comes off the top before the fee is applied to profit.
    """
    from pricing_engine import BOOKING_TYPES, BONUS_PCT
    bt = BOOKING_TYPES.get(btype) or BOOKING_TYPES["cash"]
    rental = round(subtotal - cleaning_total, 2)
    fee = round(subtotal * bt["fee"], 2)
    listing = round(subtotal / (1 - bt["fee"]), 2) if bt["commission"] else None
    gross = round(rental - fee, 2)
    return {
        "rental_price_effective": rental,
        "fee": fee,
        "listing_price": listing,
        "gross_profit": gross,
        "bonus": round(gross * BONUS_PCT, 2),
    }


def _apply_override(snap, btype, override_subtotal):
    """Return a snapshot whose live fields reflect a manual price.

    The originally computed figures are preserved as computed_*, so a revert is
    lossless and the details view can show both. Everything downstream (BONUS tab,
    Remaining, chart marker) keeps reading subtotal/gross_profit/bonus and therefore
    sees the effective numbers with no special-casing.
    """
    out = dict(snap)
    out.setdefault("computed_subtotal", snap.get("subtotal", 0))
    out.setdefault("computed_gross_profit", snap.get("gross_profit", 0))
    out.setdefault("computed_bonus", snap.get("bonus", 0))
    r = _recompute_from_subtotal(override_subtotal,
                                 float(snap.get("cleaning_total", 0) or 0), btype)
    out["override_subtotal"] = override_subtotal
    out["subtotal"] = override_subtotal
    out["gross_profit"] = r["gross_profit"]
    out["bonus"] = r["bonus"]
    out["fee"] = r["fee"]
    out["rental_price_effective"] = r["rental_price_effective"]
    if r["listing_price"] is not None:
        out["listing_price"] = r["listing_price"]
    return out


def _revert_override(snap):
    """Restore the computed figures and drop every override-only field."""
    out = dict(snap)
    if "computed_subtotal" not in out:
        return out
    out["subtotal"] = out.pop("computed_subtotal")
    out["gross_profit"] = out.pop("computed_gross_profit", out.get("gross_profit", 0))
    out["bonus"] = out.pop("computed_bonus", out.get("bonus", 0))
    for k in ("override_subtotal", "fee", "rental_price_effective", "listing_price"):
        out.pop(k, None)
    return out


def _edit_prefill(b, props, qs):
    """Form values for the unified Edit screen.

    A fresh Edit click carries only edit_id, so everything is read off the block
    server-side. A re-render after a rejected save carries the submitted values, so
    those win and the user does not lose their input.
    """
    if qs.get("dates"):
        return dict(qs)
    pf = {
        "dates": f'{b.get("checkin","")} to {b.get("checkout","")}',
        "label": b.get("label", ""),
        "notes": b.get("notes", ""),
        "btype": b.get("btype", ""),
        "price_note": b.get("price_note", ""),
        "source": b.get("source", ""),
    }
    for h in b.get("houses", []):
        pf[f"h_{h}"] = "on"
    qp = b.get("quote_params") or {}
    if isinstance(qp.get("houses"), dict):
        for h, g in qp["houses"].items():
            pf[f"g_{h}"] = str(g)
    for k in ("jacuzzi", "pets", "event_guests"):
        if qp.get(k):
            pf[k] = str(qp[k])
    if qp.get("event"):
        pf["event"] = "on"
    # Prefill the rental fee only from a price that cannot drift: an explicit
    # override, or the stored subtotal of a reservation priced without the
    # calculator (no booking type, so nothing recalculates under it). Where a live
    # calculation exists the field falls back to that instead — prefilling a stale
    # stored subtotal would let an unchanged Save silently mint an override pinning
    # the old number. A legacy unpriced record prefills empty, so the required field
    # forces a fee to be entered before it can be saved.
    if b.get("override_subtotal"):
        pf["rental_fee"] = f"{float(b['override_subtotal']):.2f}"
    elif b.get("btype") not in _PRICED_BTYPES:
        stored = float((b.get("snapshot") or {}).get("subtotal") or 0)
        if stored:
            pf["rental_fee"] = f"{stored:.2f}"
    return pf


def _quote_for_prefill(pf, props):
    """Live quote for the values currently in the Edit form, or None."""
    btype = (pf.get("btype") or "").strip()
    if btype not in _PRICED_BTYPES:
        return None
    try:
        parts = [s.strip() for s in
                 (pf.get("dates") or "").replace(" to ", "|").split("|")]
        if len(parts) != 2:
            return None
        ci, co = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
        houses = [h for h in props if pf.get(f"h_{h}") == "on"]
        if not houses:
            return None
        bookings = {h: int(pf.get(f"g_{h}") or props[h]["base_cap"]) for h in houses}
        q = quote(ci, co, bookings,
                  jacuzzi_uses=int(pf.get("jacuzzi") or 0),
                  pets=int(pf.get("pets") or 0),
                  booking_type=btype,
                  event=pf.get("event") in ("on", "1", "true"),
                  event_guests=int(pf.get("event_guests") or 0))
        return None if q.errors else q
    except (ValueError, KeyError, TypeError):
        return None


def _render_edit_screen(props, b, pf, admin_key, kp, msg, price_warning, confirm_rm):
    """One page, three sections: stay, price, payments.

    Everything about a reservation is editable here — the pricing controls are
    always visible rather than hidden behind a disclosure, which is what made an
    unpriced block look unpriceable.
    """
    def v(k, d=""):
        return html_esc(str(pf.get(k, d)))

    sec = ('style="border:1px solid #333c36;border-radius:6px;padding:14px 16px;'
           'margin-bottom:16px"')
    lg = ('style="font:11px Verdana,sans-serif;text-transform:uppercase;'
          'letter-spacing:.12em;color:var(--accent);padding:0 6px"')

    # --- 1. STAY -----------------------------------------------------------
    house_checks, guest_inputs = "", ""
    for h, hp in props.items():
        ck = " checked" if pf.get(f"h_{h}") == "on" else ""
        house_checks += (f'<label style="margin-right:16px">'
                         f'<input type="checkbox" name="h_{h}"{ck}> '
                         f'{html_esc(hp["name"])}</label>')
        gv = html_esc(str(pf.get(f"g_{h}", hp["base_cap"])))
        guest_inputs += (f'<label style="margin-right:12px">{html_esc(hp["name"])} '
                         f'<input type="number" name="g_{h}" min="1" '
                         f'max="{hp["max_guests"]}" value="{gv}" style="width:50px">'
                         f'</label>')
    ev_chk = " checked" if pf.get("event") in ("on", "1", "true") else ""
    stay = (
        f'<fieldset {sec}><legend {lg}>1 · Stay</legend>'
        f'<div class="row"><label>Dates <input type="text" id="admin_dates" '
        f'name="dates" value="{v("dates")}" required style="min-width:240px">'
        f'</label></div>'
        f'<div class="row" style="margin-top:10px">{house_checks}</div>'
        f'<div class="row" style="margin-top:10px">{guest_inputs}</div>'
        f'<div class="row" style="margin-top:10px">'
        f'<label>Jacuzzi uses <input type="number" name="jacuzzi" min="0" '
        f'value="{v("jacuzzi", "0")}" style="width:60px"></label>'
        f'<label>Pets <input type="number" name="pets" min="0" '
        f'value="{v("pets", "0")}" style="width:60px"></label>'
        f'<label>Event <input type="checkbox" name="event"{ev_chk}></label>'
        f'<label>Event guests <input type="number" name="event_guests" min="0" '
        f'value="{v("event_guests", "0")}" style="width:60px"></label></div>'
        f'<div class="row" style="margin-top:10px">'
        f'<label>Guest / reason <input type="text" name="label" value="{v("label")}" '
        f'required style="min-width:260px"></label>'
        f'<label>Source {_source_select(pf.get("source", ""))}</label></div>'
        f'<label style="display:block;margin-top:10px">'
        f'<span style="display:block;color:var(--dim);font:12px Verdana;'
        f'margin-bottom:4px">Notes</span>'
        f'<textarea name="notes" rows="3" style="width:100%;max-width:420px;'
        f'box-sizing:border-box;background:var(--bg);color:var(--ink);'
        f'border:1px solid #333c36;border-radius:4px;padding:6px 8px;'
        f'font:12px Verdana,sans-serif;resize:vertical">{v("notes")}</textarea>'
        f'</label></fieldset>'
    )

    # --- 2. PRICE ----------------------------------------------------------
    btype_opts = ""
    for bt_id, bt_lbl in [("", "No booking type"), ("cash", "Cash"),
                          ("airbnb", "Airbnb (15.5%)"),
                          ("monobank", "Site — UA card (1.3%)"),
                          ("stripe", "Site — Int'l card (5.5%)")]:
        selm = " selected" if pf.get("btype", "") == bt_id else ""
        btype_opts += f'<option value="{bt_id}"{selm}>{bt_lbl}</option>'
    live = _quote_for_prefill(pf, props)
    if live:
        calc_line = (f'Calculated price: <b>${live.subtotal:,.2f}</b>'
                     f'<span style="color:var(--dim);font-size:12px"> for the inputs '
                     f'above</span>')
        fee_default = pf.get("rental_fee") or f"{live.subtotal:.2f}"
    else:
        calc_line = ('<span style="color:var(--dim)">Choose a booking type above to '
                     'calculate a price, or just enter the rental fee below.</span>')
        fee_default = pf.get("rental_fee", "")
    is_custom = b.get("override_subtotal") is not None
    revert = ""
    if is_custom:
        revert = (f'<a href="/admin?tab=block&action=revert_price'
                  f'&block_id={urllib.parse.quote(b["sk"], safe="")}{kp}" '
                  f'class="btn-sec" style="margin-left:8px">Revert to calculated</a>')
    price = (
        f'<fieldset {sec}><legend {lg}>2 · Pricing</legend>'
        f'<div class="row"><label>Booking type '
        f'<select name="btype">{btype_opts}</select></label></div>'
        f'<p style="margin:10px 0 6px;font:13px Verdana,sans-serif">{calc_line}</p>'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Rental fee ($) <input type="number" name="rental_fee" min="0.01" '
        f'step="0.01" required value="{html_esc(str(fee_default))}" '
        f'style="width:110px"></label>'
        f'<label>Reason <input type="text" name="price_note" '
        f'value="{v("price_note")}" placeholder="e.g. friend discount" '
        f'style="min-width:200px"></label>{revert}</div>'
        f'<p style="color:var(--dim);font-size:11px;font-family:Verdana;'
        f'margin:8px 0 0">Every reservation needs a rental fee. Leave it equal to the '
        f'calculated price for no override. Cleaning is a fixed cost and is never '
        f'discounted.</p>'
        f'</fieldset>'
    )

    # --- 3. PAYMENTS -------------------------------------------------------
    rows = ""
    bid = urllib.parse.quote(b["sk"], safe="")
    for p in _payments_of(b):
        pid = p.get("id", "")
        note = (p.get("note") or "").strip()
        if confirm_rm and confirm_rm == pid:
            rm = (f'<a href="/admin?tab=block&action=remove_payment&block_id={bid}'
                  f'&payment_id={urllib.parse.quote(pid, safe="")}&back=edit{kp}" '
                  f'style="color:var(--err);font-size:11px">confirm remove</a>')
        else:
            rm = (f'<a href="/admin?tab=block&edit_id={bid}'
                  f'&confirm_rm={urllib.parse.quote(pid, safe="")}{kp}" '
                  f'style="color:var(--dim);font-size:11px">remove</a>')
        rows += (f'<li style="margin-bottom:3px">{html_esc(_fmt_date(p.get("date","")))} — '
                 f'<b>{_money(float(p.get("amount") or 0))}</b>'
                 f'{(" — " + html_esc(note)) if note else ""} &nbsp;{rm}</li>')
    listing = (f'<ul style="padding-left:18px;margin:0 0 10px;'
               f'font:12px/1.6 Verdana,sans-serif">{rows}</ul>') if rows else (
        '<p style="color:var(--dim);font-size:12px;margin:0 0 10px">'
        'No payments recorded.</p>')
    received, outstanding, overpaid, _pr = _payment_state(b)
    if outstanding is None:
        tot = (f'Received: <b>{_money(received)}</b> &nbsp;·&nbsp; '
               f'Outstanding: <span style="color:var(--dim)">— (unpriced)</span>')
    elif overpaid:
        tot = (f'Received: <b>{_money(received)}</b> &nbsp;·&nbsp; '
               f'Outstanding: $0.00 <span style="color:var(--err)">(overpaid)</span>')
    else:
        tot = (f'Received: <b>{_money(received)}</b> &nbsp;·&nbsp; '
               f'Outstanding: <b>{_money(outstanding)}</b>')
    payments = (
        f'<fieldset {sec}><legend {lg}>3 · Payments</legend>'
        f'{listing}'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Add payment ($) <input type="number" name="pay_amount" step="0.01" '
        f'style="width:110px"></label>'
        f'<label>Date <input type="date" name="pay_date" '
        f'min="{_RETRO_FLOOR.isoformat()}" '
        f'value="{date.today().isoformat()}" style="width:150px"></label>'
        f'<label>Note <input type="text" name="pay_note" placeholder="optional" '
        f'style="min-width:150px"></label></div>'
        f'<p style="margin:10px 0 0;font:13px Verdana,sans-serif">{tot}</p>'
        f'<p style="color:var(--dim);font-size:11px;font-family:Verdana;'
        f'margin:6px 0 0">Totals update when you save.</p>'
        f'</fieldset>'
    )

    exp_rows = ""
    for e in _expenses_of(b):
        eid = e.get("id", "")
        enote = (e.get("note") or "").strip()
        if confirm_rm and confirm_rm == eid:
            erm = (f'<a href="/admin?tab=block&action=remove_expense&block_id={bid}'
                   f'&expense_id={urllib.parse.quote(eid, safe="")}&back=edit{kp}" '
                   f'style="color:var(--err);font-size:11px">confirm remove</a>')
        else:
            erm = (f'<a href="/admin?tab=block&edit_id={bid}'
                   f'&confirm_rm={urllib.parse.quote(eid, safe="")}{kp}" '
                   f'style="color:var(--dim);font-size:11px">remove</a>')
        exp_rows += (f'<li style="margin-bottom:3px">{html_esc(_fmt_date(e.get("date","")))}'
                     f' — <b>{_money(float(e.get("amount") or 0))}</b>'
                     f'{(" — " + html_esc(enote)) if enote else ""} &nbsp;{erm}</li>')
    exp_list = (f'<ul style="padding-left:18px;margin:0 0 10px;'
                f'font:12px/1.6 Verdana,sans-serif">{exp_rows}</ul>') if exp_rows else (
        '<p style="color:var(--dim);font-size:12px;margin:0 0 10px">'
        'No expenses recorded.</p>')
    exp_tot = _expense_total(b)
    expenses_html = (
        f'<fieldset {sec}><legend {lg}>4 · Expenses</legend>'
        f'{exp_list}'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Add expense ($) <input type="number" name="exp_amount" step="0.01" '
        f'style="width:110px"></label>'
        f'<label>Date <input type="date" name="exp_date" '
        f'min="{_RETRO_FLOOR.isoformat()}" '
        f'value="{date.today().isoformat()}" style="width:150px"></label>'
        f'<label>Note <input type="text" name="exp_note" placeholder="optional" '
        f'style="min-width:150px"></label></div>'
        f'<p style="margin:10px 0 0;font:13px Verdana,sans-serif">'
        f'Expenses: <b>{_money(exp_tot)}</b></p>'
        f'<p style="color:var(--dim);font-size:11px;font-family:Verdana;margin:6px 0 0">'
        f'Your costs, not guest credits — these reduce the bonus but never change '
        f'what the guest still owes.</p>'
        f'</fieldset>'
    )

    if msg or price_warning:
        colour = "var(--err)" if msg.lower().startswith(("error", "conflict")) else "var(--ok)"
        banner = '<div class="result" style="margin-bottom:16px">'
        if msg:
            banner += f'<p style="margin:0;color:{colour}">{html_esc(msg)}</p>'
        if price_warning:
            banner += (f'<p style="margin:6px 0 0;color:var(--accent);font-size:12px">'
                       f'{html_esc(price_warning)}</p>')
        banner += '</div>'
    else:
        banner = ""

    back = f'/admin?tab=block&details={bid}{kp}'
    key_hidden = (f'<input type="hidden" name="key" value="{html_esc(admin_key)}">'
                  if admin_key else "")
    return (
        f'<h2 style="font:16px Verdana,sans-serif;margin:0 0 4px">Edit reservation</h2>'
        f'<p style="color:var(--dim);font-size:12px;margin:0 0 16px">'
        f'{html_esc(b.get("label","?"))} · everything about this booking in one place</p>'
        f'{banner}'
        f'<form method="get" action="/admin">'
        f'<input type="hidden" name="tab" value="block">'
        f'<input type="hidden" name="action" value="add_block">'
        f'<input type="hidden" name="edit_id" value="{html_esc(b["sk"])}">'
        f'{key_hidden}'
        f'{stay}{price}{payments}{expenses_html}'
        f'<button type="submit">Save reservation</button>'
        f'<a href="{html_esc(back)}" class="btn-sec" style="margin-left:10px">Cancel</a>'
        f'</form>'
    )


def _payments_section(b, kp, confirm_remove=""):
    """Ledger list + inline add form for the details view (admin-only, like its caller)."""
    key_hidden = (f'<input type="hidden" name="key" value="{html_esc(kp[5:])}">'
                  if kp else "")
    bid = urllib.parse.quote(b["sk"], safe="")
    rows = ""
    for p in _payments_of(b):
        pid = p.get("id", "")
        amt = float(p.get("amount") or 0)
        note = (p.get("note") or "").strip()
        # Two-step remove, matching the block-cancel pattern: never single-click.
        if confirm_remove and confirm_remove == pid:
            rm = (f'<a href="/admin?tab=block&action=remove_payment'
                  f'&block_id={bid}&payment_id={urllib.parse.quote(pid, safe="")}{kp}" '
                  f'style="color:var(--err);font-size:11px">confirm remove</a>')
        else:
            rm = (f'<a href="/admin?tab=block&details={bid}'
                  f'&confirm_rm={urllib.parse.quote(pid, safe="")}{kp}" '
                  f'style="color:var(--dim);font-size:11px">remove</a>')
        rows += (
            f'<li style="margin-bottom:3px">{html_esc(_fmt_date(p.get("date","")))} — '
            f'<b>{_money(amt)}</b>'
            f'{(" — " + html_esc(note)) if note else ""} &nbsp;{rm}</li>'
        )
    listing = (f'<ul style="padding-left:18px;margin:6px 0 10px;font:12px/1.6 Verdana,'
               f'sans-serif">{rows}</ul>') if rows else (
        '<p style="color:var(--dim);font-size:12px;margin:6px 0 10px">'
        'No payments recorded.</p>')
    return (
        f'<div style="margin-top:14px">'
        f'<h3 style="font:11px Verdana,sans-serif;text-transform:uppercase;'
        f'letter-spacing:.1em;color:var(--dim);margin:0 0 4px">Payments</h3>'
        f'{listing}'
        f'<form method="get" action="/admin">'
        f'<input type="hidden" name="tab" value="block">'
        f'<input type="hidden" name="action" value="add_payment">'
        f'<input type="hidden" name="block_id" value="{html_esc(b["sk"])}">'
        f'{key_hidden}'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Amount ($) <input type="number" name="amount" step="0.01" '
        f'style="width:100px"></label>'
        f'<label>Date <input type="date" name="pay_date" '
        f'min="{_RETRO_FLOOR.isoformat()}" '
        f'value="{date.today().isoformat()}" style="width:150px"></label>'
        f'<label>Note <input type="text" name="pay_note" '
        f'placeholder="optional" style="min-width:150px"></label>'
        f'<button type="submit">Add payment</button>'
        f'</div></form></div>'
    )


def _expenses_section(b, kp, confirm_remove=""):
    """Expense ledger + quick-add for the details view (mirrors _payments_section)."""
    key_hidden = (f'<input type="hidden" name="key" value="{html_esc(kp[5:])}">'
                  if kp else "")
    bid = urllib.parse.quote(b["sk"], safe="")
    rows = ""
    for e in _expenses_of(b):
        eid = e.get("id", "")
        note = (e.get("note") or "").strip()
        if confirm_remove and confirm_remove == eid:
            rm = (f'<a href="/admin?tab=block&action=remove_expense'
                  f'&block_id={bid}&expense_id={urllib.parse.quote(eid, safe="")}{kp}" '
                  f'style="color:var(--err);font-size:11px">confirm remove</a>')
        else:
            rm = (f'<a href="/admin?tab=block&details={bid}'
                  f'&confirm_rm={urllib.parse.quote(eid, safe="")}{kp}" '
                  f'style="color:var(--dim);font-size:11px">remove</a>')
        rows += (f'<li style="margin-bottom:3px">{html_esc(_fmt_date(e.get("date","")))}'
                 f' — <b>{_money(float(e.get("amount") or 0))}</b>'
                 f'{(" — " + html_esc(note)) if note else ""} &nbsp;{rm}</li>')
    listing = (f'<ul style="padding-left:18px;margin:6px 0 10px;'
               f'font:12px/1.6 Verdana,sans-serif">{rows}</ul>') if rows else (
        '<p style="color:var(--dim);font-size:12px;margin:6px 0 10px">'
        'No expenses recorded.</p>')
    return (
        f'<div style="margin-top:14px">'
        f'<h3 style="font:11px Verdana,sans-serif;text-transform:uppercase;'
        f'letter-spacing:.1em;color:var(--dim);margin:0 0 4px">Expenses</h3>'
        f'{listing}'
        f'<form method="get" action="/admin">'
        f'<input type="hidden" name="tab" value="block">'
        f'<input type="hidden" name="action" value="add_expense">'
        f'<input type="hidden" name="block_id" value="{html_esc(b["sk"])}">'
        f'{key_hidden}'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Amount ($) <input type="number" name="amount" step="0.01" '
        f'style="width:100px"></label>'
        f'<label>Date <input type="date" name="exp_date" '
        f'min="{_RETRO_FLOOR.isoformat()}" '
        f'value="{date.today().isoformat()}" style="width:150px"></label>'
        f'<label>Note <input type="text" name="exp_note" '
        f'placeholder="optional" style="min-width:150px"></label>'
        f'<button type="submit">Add expense</button>'
        f'</div></form></div>'
    )


def _price_override_form(b, snap, subtotal, is_custom, kp):
    """Collapsed 'Edit price' panel for the details view (admin-only, like its caller).

    Uses <details> so it needs no JS, and a GET form matching every other admin action.
    """
    key_hidden = ""
    if kp:
        key_hidden = f'<input type="hidden" name="key" value="{html_esc(kp[5:])}">'
    cleaning = float(snap.get("cleaning_total", 0) or 0)
    note_val = html_esc((b.get("price_note") or "").strip())
    revert = ""
    if is_custom:
        revert_url = (f"/admin?tab=block&action=revert_price"
                      f"&block_id={urllib.parse.quote(b['sk'], safe='')}{kp}")
        revert = (f'<a href="{html_esc(revert_url)}" class="btn-sec" '
                  f'style="margin-left:8px">Revert to calculated</a>')
    hint = (f'Cleaning (${cleaning:,.2f}) is a fixed cost and is never discounted.'
            if cleaning else 'Fees and bonus are recalculated from this price.')
    return (
        f'<details style="margin-top:10px">'
        f'<summary style="cursor:pointer;color:var(--dim);font:12px Verdana">'
        f'Edit price</summary>'
        f'<form method="get" action="/admin" style="margin-top:8px">'
        f'<input type="hidden" name="tab" value="block">'
        f'<input type="hidden" name="action" value="set_price">'
        f'<input type="hidden" name="block_id" value="{html_esc(b["sk"])}">'
        f'{key_hidden}'
        f'<div class="row" style="align-items:flex-end">'
        f'<label>Final price ($) <input type="number" name="price" min="0" step="0.01" '
        f'value="{subtotal:.2f}" style="width:110px"></label>'
        f'<label>Reason <input type="text" name="price_note" value="{note_val}" '
        f'placeholder="e.g. friend discount" style="min-width:200px"></label>'
        f'</div>'
        f'<p style="color:var(--dim);font-size:11px;font-family:Verdana;margin:6px 0 8px">'
        f'{html_esc(hint)}</p>'
        f'<button type="submit">Save price</button>{revert}'
        f'</form></details>'
    )


def _payments_of(b):
    """The block's payment ledger — the single source of truth for cash received.

    Legacy blocks carry a single `deposit` scalar and no ledger; those are migrated
    lazily here into payment #1 so every reader sees one shape. The migration is
    materialised on the next write; until then `deposit` is only ever read through
    this function, and once a ledger exists `deposit` is ignored outright.
    """
    pays = b.get("payments")
    if isinstance(pays, list):
        return sorted(pays, key=lambda p: (p.get("date") or "", p.get("id") or ""))
    dep = float(b.get("deposit") or 0)
    if dep:
        return [{
            "id": "legacy",
            "date": (b.get("created_at") or b.get("checkin") or "")[:10],
            "amount": dep,
            "note": "downpayment (migrated)",
        }]
    return []


def _new_payment(amount, date_str, note):
    import uuid
    return {"id": uuid.uuid4().hex[:10], "date": date_str,
            "amount": round(float(amount), 2), "note": note or ""}


def _exp_line(b):
    tot = _expense_total(b)
    return f'<br>Expenses: {_money(tot)}' if tot else ""


def _expenses_of(b):
    """The reservation's expense ledger. Mirrors _payments_of but has no legacy
    scalar to migrate, so it is simply the stored list."""
    exp = b.get("expenses")
    return sorted(exp, key=lambda e: (e.get("date") or "", e.get("id") or "")) \
        if isinstance(exp, list) else []


def _expense_total(b):
    return round(sum(float(e.get("amount") or 0) for e in _expenses_of(b)), 2)


def _effective_subtotal(b):
    snap = b.get("snapshot")
    return float(snap.get("subtotal", 0) or 0) if snap else None


def _bonus_ratio(b):
    """Share of each dollar received that is Anya's, override-aware.

    Bonus is still earned on gross profit; this only spreads the already-computed
    bonus across the money as it actually arrives (cash basis).
    """
    snap = b.get("snapshot")
    if not snap:
        return None
    subtotal = float(snap.get("subtotal", 0) or 0)
    if not subtotal:
        return None
    return float(snap.get("bonus", 0) or 0) / subtotal


def _payment_state(b):
    """(received, outstanding, overpaid, priced) from the ledger alone.

    outstanding is clamped at 0 and is None for a block with no price snapshot —
    money can be recorded against an unpriced block, but a balance cannot be.
    """
    received = round(sum(float(p.get("amount") or 0) for p in _payments_of(b)), 2)
    subtotal = _effective_subtotal(b)
    if subtotal is None:
        return received, None, False, False
    raw = round(subtotal - received, 2)
    return received, max(0.0, raw), raw < 0, True


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


def _ord(n):
    """1st, 2nd, 3rd, 4th … 11th/12th/13th are the exceptions to the 1/2/3 rule."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _fmt_date(iso, with_year=True):
    """'2026-08-14' -> 'Aug 14th, 2026'. ISO is kept for URLs, JSON and storage."""
    if not iso:
        return ""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    head = f"{d.strftime('%b')} {_ord(d.day)}"
    return f"{head}, {d.year}" if with_year else head


def _fmt_range(ci, co):
    """'Aug 14th → Aug 16th, 2026 (2 nights)', repeating the year only if it differs."""
    try:
        a, b = date.fromisoformat(str(ci)[:10]), date.fromisoformat(str(co)[:10])
    except (ValueError, TypeError):
        return f"{ci} → {co}"
    nights = (b - a).days
    same_year = a.year == b.year
    left = _fmt_date(a, with_year=not same_year)
    out = f"{left} → {_fmt_date(b)}"
    return f"{out} ({nights} night{'' if nights == 1 else 's'})"


_LEGACY_EDIT_RE = __import__("re").compile(r"^\s*edited from\s+(\S+)\s*$", __import__("re").I)


def _lineage_parent(b):
    """The sk this record was edited from, or None.

    New records carry edited_from. Records written before that field existed
    encoded the lineage in created_by as "edited from <sk>"; parse those too so the
    migration needs no backfill.
    """
    if b.get("edited_from"):
        return b["edited_from"]
    m = _LEGACY_EDIT_RE.match(str(b.get("created_by") or ""))
    return m.group(1) if m else None


def _superseded_sks(blocks):
    """sks that a later edit replaced — their money lives on the newer record.

    Covers both the explicit marker (status/superseded_by, written going forward)
    and the legacy shape, where the edit only left the successor pointing back.
    """
    out = set()
    for b in blocks:
        parent = _lineage_parent(b)
        if parent:
            out.add(parent)
        if b.get("superseded_by"):
            out.add(b["sk"])
    return out


def _is_superseded(b, superseded_sks):
    """True when this record was replaced by an edit rather than cancelled.

    A genuine guest cancellation is NOT superseded: its money still counts.
    """
    if b.get("status") == "superseded":
        return True
    return b.get("status") == "cancelled" and b.get("sk") in superseded_sks


def _provenance(b):
    """Human provenance line. Never renders a raw sk."""
    when = _fmt_date(b.get("created_at", ""))
    if _lineage_parent(b):
        return f"Edited {when}" if when else "Edited"
    who = (b.get("created_by") or "").strip()
    if when and who:
        return f"Created {when} by {html_esc(who)}"
    if when:
        return f"Created {when}"
    return f"Created by {html_esc(who)}" if who else ""


def _money(x):
    """Currency with the sign outside the symbol: -$625.00, not $-625.00."""
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _quarter_bounds(y, q):
    """First and last day of quarter q (1-4) in year y."""
    start_m = 3 * (q - 1) + 1
    return date(y, start_m, 1), _month_end(y, start_m + 2)


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
/* Unpaid balance: dashed rule along the bar's foot. Painted as a background-image,
   which is why bars set background-color (not the background shorthand) inline —
   the shorthand would reset this to none. */
.tape-unpaid{background-image:repeating-linear-gradient(90deg,
rgba(255,255,255,.6) 0 4px,rgba(255,255,255,0) 4px 8px);
background-repeat:no-repeat;background-position:0 100%;background-size:100% 2px}
"""

# Flatpickr range script with live availability greying (not an f-string to avoid brace escaping)
AVAIL_JS = """
flatpickr('#dates', {
  mode:'range', dateFormat:'Y-m-d', minDate:'__MINDATE__', showMonths:2,
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

            # Show the label from ~1.5 cells up; the bar clips it with an ellipsis and
            # never grows to fit. Below that there is no room, so tooltip only.
            nolabel = width < 1.5 * _TAPE_CELL_W
            label = b.get("label", "?")

            # Unpaid balance marker — priced blocks still owing money. Fully paid and
            # unpriced blocks stay unmarked.
            _dep, _rem, _over, _priced = _payment_state(b)
            unpaid = " tape-unpaid" if (_priced and _rem and _rem > 0) else ""

            tip = label
            if (b.get("notes") or "").strip():
                tip += " · has notes"
            if unpaid:
                tip += f" · ${_rem:,.2f} remaining"

            detail_url = f"/admin?tab=block&details={html_esc(b['sk'])}{kp}"
            anchored.setdefault(anchor, []).append(
                f'<a class="tape-bar{slant}{edge}{unpaid}'
                f'{" tape-bar-nolabel" if nolabel else ""}"'
                f' href="{detail_url}"'
                f' style="background-color:{_block_color(b["sk"])};'
                f'left:{left_off}px;width:{width}px;{clip}"'
                f' title="{html_esc(tip)}"'
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
        'ends mid-day&nbsp;=&nbsp;check-out 10am&nbsp;&nbsp;·&nbsp;&nbsp;'
        'dashed underline&nbsp;=&nbsp;balance still owing</p>'
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
        f'<script>{_avail_js("today")}</script>'
        f'</body></html>'
    )


def render_admin(props: dict, blocks: list, msg="", prefill=None, admin_key="",
                 tab="block", q=None, price_error=None, price_params=None,
                 avail=None, block_url=None, month_str="", detail_block=None,
                 confirm_cancel=False, price_warning="", confirm_rm="",
                 qs_bonus=None, edit_target=None):
    qs_bonus = qs_bonus or {}
    pf = prefill or {}
    kp = f"&key={admin_key}" if admin_key else ""
    key_hidden = f'<input type="hidden" name="key" value="{html_esc(admin_key)}">' if admin_key else ""

    tab_block_href = f"/admin?tab=block{kp}"
    tab_price_href = f"/admin?tab=price{kp}"
    tab_bonus_href = f"/admin?tab=bonus{kp}"
    tab_nav = (
        f'<nav class="tab-nav">'
        f'<a href="{tab_block_href}" class="{"active" if tab == "block" else ""}">Reservations</a>'
        f'<a href="{tab_price_href}" class="{"active" if tab == "price" else ""}">Pricing</a>'
        f'<a href="{tab_bonus_href}" class="{"active" if tab == "bonus" else ""}">Finances</a>'
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
        # Cash basis: the report is driven by PAYMENTS dated in the quarter, not by
        # bookings. Bonus is still earned on gross profit — bonus_ratio just spreads
        # each booking's already-computed bonus across the money as it arrives.
        today_q = date.today()
        try:
            q_sel = int(qs_bonus.get("q") or ((today_q.month - 1) // 3 + 1))
        except (ValueError, TypeError):
            q_sel = (today_q.month - 1) // 3 + 1
        q_sel = min(4, max(1, q_sel))
        try:
            y_sel = int(qs_bonus.get("year") or today_q.year)
        except (ValueError, TypeError):
            y_sel = today_q.year
        q_start, q_end = _quarter_bounds(y_sel, q_sel)
        q_label = f"Q{q_sel} {y_sel}"

        # Collect payments in range, grouped by block. Cancelled blocks are included:
        # money that changed hands still counts in the quarter it was received.
        # Records replaced by an edit are excluded outright: the live copy carries
        # their payments and expenses, so counting both double-counts the money.
        # Genuinely cancelled records still count — that money really changed hands.
        _sup = _superseded_sks(blocks)
        excluded = [b for b in blocks if _is_superseded(b, _sup)
                    and (_payments_of(b) or _expenses_of(b))]
        _live = [b for b in blocks if not _is_superseded(b, _sup)]

        groups, unpriced_groups = [], []
        total_received = 0.0
        total_bonus = 0.0
        for b in sorted(_live, key=lambda x: (x.get("label") or "")):
            hits = [p for p in _payments_of(b)
                    if q_start.isoformat() <= (p.get("date") or "") <= q_end.isoformat()]
            if not hits:
                continue
            ratio = _bonus_ratio(b)
            got = round(sum(float(p.get("amount") or 0) for p in hits), 2)
            total_received += got
            if ratio is None:
                unpriced_groups.append((b, hits, got))
            else:
                earned = round(sum(round(float(p.get("amount") or 0) * ratio, 2)
                                   for p in hits), 2)
                total_bonus += earned
                groups.append((b, hits, got, earned))
        # Expenses reduce profit, and the bonus is a share of profit, so each
        # expense in the quarter claws back BONUS_PCT of itself. Rounded per entry,
        # matching how payment accruals round.
        from pricing_engine import BONUS_PCT as _BPCT
        exp_groups, total_expenses, total_exp_impact = [], 0.0, 0.0
        for b in sorted(_live, key=lambda x: (x.get("label") or "")):
            hits = [e for e in _expenses_of(b)
                    if q_start.isoformat() <= (e.get("date") or "") <= q_end.isoformat()]
            if not hits:
                continue
            spent = round(sum(float(e.get("amount") or 0) for e in hits), 2)
            impact = round(sum(round(float(e.get("amount") or 0) * _BPCT, 2)
                               for e in hits), 2)
            total_expenses += spent
            total_exp_impact += impact
            exp_groups.append((b, hits, spent, impact))
        total_received = round(total_received, 2)
        total_expenses = round(total_expenses, 2)
        total_exp_impact = round(total_exp_impact, 2)
        total_bonus = round(total_bonus - total_exp_impact, 2)

        qnav = ""
        for qi in range(1, 5):
            on = " active" if qi == q_sel else ""
            qnav += (f'<a href="/admin?tab=bonus&q={qi}&year={y_sel}{kp}" '
                     f'class="btn-sec" style="padding:4px 12px'
                     f'{";border-color:var(--accent);color:var(--accent)" if on else ""}">'
                     f'Q{qi}</a>')
        bonus_nav = (
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;'
            f'flex-wrap:wrap">'
            f'<a href="/admin?tab=bonus&q={q_sel}&year={y_sel - 1}{kp}" class="btn-sec" '
            f'style="padding:4px 12px">← {y_sel - 1}</a>'
            f'<span style="font:13px Verdana,sans-serif;color:var(--dim)">{y_sel}</span>'
            f'<a href="/admin?tab=bonus&q={q_sel}&year={y_sel + 1}{kp}" class="btn-sec" '
            f'style="padding:4px 12px">{y_sel + 1} →</a>'
            f'<span style="width:12px"></span>{qnav}</div>'
        )

        tbl_s = 'style="border-collapse:collapse;width:100%;font:13px Verdana,sans-serif"'
        th_s = ('style="text-align:left;padding:8px 12px;border-bottom:1px solid #333c36;'
                'color:var(--dim);font-size:11px;letter-spacing:.1em;'
                'text-transform:uppercase"')
        td_s = 'style="padding:8px 12px;border-bottom:1px solid #2a322d"'

        if groups:
            rows_b = ""
            for b, hits, got, earned in groups:
                tag = ""
                if b.get("status") != "active":
                    tag = ('<span style="color:var(--dim);font-size:10px;'
                           'text-transform:uppercase"> cancelled</span>')
                if (b.get("snapshot") or {}).get("override_subtotal") is not None:
                    tag += ('<span style="color:var(--dim);font-size:10px;'
                            'text-transform:uppercase"> custom</span>')
                span = len(hits)
                for i, pmt in enumerate(hits):
                    amt = float(pmt.get("amount") or 0)
                    pb = round(amt * _bonus_ratio(b), 2)
                    note = (pmt.get("note") or "").strip()
                    lead = ""
                    if i == 0:
                        lead = (
                            f'<td {td_s} rowspan="{span}">'
                            f'{html_esc(b.get("label","?"))}{tag}<br>'
                            f'<span style="color:var(--dim);font-size:11px">'
                            f'{html_esc(_fmt_range(b.get("checkin",""), b.get("checkout","")))}'
                            f'</span></td>'
                        )
                    note_html = ""
                    if note:
                        note_html = ('<span style="color:var(--dim);font-size:11px"> '
                                     + html_esc(note) + '</span>')
                    rows_b += (
                        f'<tr>{lead}'
                        f'<td {td_s}>{html_esc(_fmt_date(pmt.get("date","")))}</td>'
                        f'<td {td_s}>{_money(amt)}{note_html}</td>'
                        f'<td class="bonus" {td_s}>{_money(pb)}</td>'
                        f'</tr>'
                    )
            priced_html = (
                f'<div style="overflow-x:auto"><table {tbl_s}>'
                f'<thead><tr><th {th_s}>Booking</th><th {th_s}>Payment date</th>'
                f'<th {th_s}>Amount</th><th {th_s}>Bonus earned</th></tr></thead>'
                f'<tbody>{rows_b}</tbody></table></div>'
            )
        else:
            priced_html = (f'<p style="color:var(--dim)">No payments received in '
                           f'{html_esc(q_label)}.</p>')

        if exp_groups:
            erows = ""
            for b, hits, spent, impact in exp_groups:
                span = len(hits)
                for i, e in enumerate(hits):
                    amt = float(e.get("amount") or 0)
                    note = (e.get("note") or "").strip()
                    nh = ('<span style="color:var(--dim);font-size:11px"> '
                          + html_esc(note) + '</span>') if note else ""
                    lead = ""
                    if i == 0:
                        lead = (f'<td {td_s} rowspan="{span}">'
                                f'{html_esc(b.get("label","?"))}</td>')
                    erows += (
                        f'<tr>{lead}'
                        f'<td {td_s}>{html_esc(_fmt_date(e.get("date","")))}</td>'
                        f'<td {td_s}>{_money(amt)}{nh}</td>'
                        f'<td {td_s} style="padding:8px 12px;'
                        f'border-bottom:1px solid #2a322d;color:var(--err)">'
                        f'{_money(-round(amt * _BPCT, 2))}</td></tr>'
                    )
            expenses_html = (
                f'<div style="margin-top:20px;overflow-x:auto">'
                f'<h2 style="font:13px Verdana,sans-serif;text-transform:uppercase;'
                f'letter-spacing:.1em;color:var(--dim);margin-bottom:8px">Expenses</h2>'
                f'<table {tbl_s}><thead><tr>'
                f'<th {th_s}>Reservation</th><th {th_s}>Date</th>'
                f'<th {th_s}>Amount</th><th {th_s}>Bonus impact</th>'
                f'</tr></thead><tbody>{erows}</tbody></table></div>'
            )
        else:
            expenses_html = ""

        if unpriced_groups:
            up_items = ""
            for b, hits, got in unpriced_groups:
                dates = ", ".join(f'{html_esc(_fmt_date(h.get("date","")))} {_money(float(h.get("amount") or 0))}'
                                  for h in hits)
                up_items += (f'<li>{html_esc(b.get("label","?"))}: ${got:,.2f} '
                             f'<span style="color:var(--dim);font-size:11px">'
                             f'({dates})</span></li>')
            unpriced_html = (
                f'<div style="margin-top:20px">'
                f'<h2 style="font:13px Verdana,sans-serif;text-transform:uppercase;'
                f'letter-spacing:.1em;color:var(--dim);margin-bottom:8px">'
                f'Unpriced — bonus not computable</h2>'
                f'<ul style="padding-left:20px;margin:0;font-size:13px">{up_items}</ul>'
                f'</div>'
            )
        else:
            unpriced_html = ""

        if excluded:
            ex_items = "".join(
                f'<li>{html_esc(b.get("label","?"))} — '
                f'{_money(round(sum(float(p.get("amount") or 0) for p in _payments_of(b)), 2))}'
                f' received, '
                f'{_money(_expense_total(b))} expenses</li>'
                for b in excluded)
            excluded_html = (
                f'<div style="margin-top:20px;color:var(--dim);font-size:12px">'
                f'<p style="margin:0 0 4px">Excluded (superseded by an edit) — '
                f'counted on the current version of each reservation:</p>'
                f'<ul style="padding-left:20px;margin:0">{ex_items}</ul></div>'
            )
        else:
            excluded_html = ""

        totals_html = (
            f'<div class="tot" style="margin-top:16px;display:flex;gap:28px;'
            f'flex-wrap:wrap;align-items:baseline">'
            f'<span>Received: <b>{_money(total_received)}</b></span>'
            f'<span>Expenses: <b>{_money(total_expenses)}</b></span>'
            f'<span class="bonus" style="font-size:18px">'
            f'BONUS DUE: <b>{_money(total_bonus)}</b></span></div>'
        )

        main_content = (
            f'<h2 style="font:16px Verdana,sans-serif;margin:0 0 16px">'
            f'Anya\'s Bonus — {html_esc(q_label)} '
            f'<span style="color:var(--dim);font-size:12px">(cash basis)</span></h2>'
            f'{bonus_nav}'
            f'<div class="result">{priced_html}{expenses_html}{unpriced_html}{excluded_html}{totals_html}</div>'
        )

    elif tab == "block" and prefill and (prefill.get("edit_id") or "").strip() \
            and edit_target is not None:
        main_content = _render_edit_screen(
            props, edit_target,
            _edit_prefill(edit_target, props, prefill),
            admin_key, kp, msg, price_warning, confirm_rm,
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

        # Prev reaches back to the retro floor and stops there — there is nothing
        # bookable before it, so walking further would only show empty windows.
        py, pm = _prev_month(month_date.year, month_date.month)
        if date(py, pm, 1) < _RETRO_FLOOR.replace(day=1):
            py, pm = _RETRO_FLOOR.year, _RETRO_FLOOR.month
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
            ]
            if admin_key:
                edit_parts.append(f"key={urllib.parse.quote(admin_key, safe='')}")
            edit_url = "/admin?" + "&".join(edit_parts)

            # Financial section
            from pricing_engine import BOOKING_TYPES as _BT
            snap = b.get("snapshot")
            dep, remaining, overpaid, _priced = _payment_state(b)
            ratio = _bonus_ratio(b)
            if dep:
                earned = (f'<br>Bonus earned to date: ${dep * ratio:,.2f} '
                          f'<span style="color:var(--dim)">of '
                          f'${float(snap.get("bonus", 0) or 0):,.2f}</span>'
                          ) if ratio else ""
                if remaining is None:
                    pay_html = (
                        f'<br>Received: ${dep:,.2f}'
                        f'<br>Outstanding: <span style="color:var(--dim)">— (unpriced)</span>'
                    )
                elif overpaid:
                    pay_html = (
                        f'<br>Received: ${dep:,.2f}'
                        f'<br>Outstanding: $0.00 '
                        f'<span style="color:var(--err)">(overpaid by '
                        f'${dep - float(snap.get("subtotal", 0) or 0):,.2f})</span>'
                        f'{earned}'
                    )
                else:
                    pay_html = (
                        f'<br>Received: ${dep:,.2f}'
                        f'<br>Outstanding: ${remaining:,.2f}{_exp_line(b)}{earned}'
                    )
            else:
                pay_html = ""
            if snap:
                # A reservation priced by its rental fee alone has no booking type;
                # naming one would claim a payment method nobody chose.
                btype_key = b.get("btype") or ""
                bt_info = _BT.get(btype_key, {})
                bt_label = bt_info.get("label") or "No booking type"
                subtotal = snap.get("subtotal", 0)
                gross_profit = snap.get("gross_profit", 0)
                bonus = snap.get("bonus", 0)
                fee_rate = bt_info.get("fee", 0.0)
                fee_amount = round(subtotal * fee_rate, 2) if fee_rate else 0.0
                fee_html = ""
                if fee_amount:
                    fee_label = "Airbnb commission" if bt_info.get("commission") else "Processing fee"
                    fee_html = f'<br>{html_esc(fee_label)}: ${fee_amount:,.2f}'

                # Manual override: show the custom price with the calculated one
                # struck through beside it, so both numbers stay visible.
                is_custom = snap.get("override_subtotal") is not None
                if is_custom:
                    note_txt = (b.get("price_note") or "").strip()
                    note_html = (f' (custom — {html_esc(note_txt)})' if note_txt
                                 else " (custom)")
                    price_line = (
                        f'Final price: <b>${subtotal:,.2f}</b>'
                        f'<span style="color:var(--dim);font-size:12px">{note_html}</span>'
                        f'&nbsp;<span style="color:var(--dim);text-decoration:line-through">'
                        f'${snap.get("computed_subtotal", 0):,.2f}</span>'
                    )
                else:
                    price_line = f'Guest pays: <b>${subtotal:,.2f}</b>'

                # Airbnb: the listed number must be grossed up so the net matches.
                listing_html = ""
                if bt_info.get("commission"):
                    lp = snap.get("listing_price")
                    if lp is None and fee_rate < 1:
                        lp = round(subtotal / (1 - fee_rate), 2)
                    if lp:
                        listing_html = f'<br>List at: ${lp:,.2f}'

                finance_html = (
                    f'<div class="tot" style="border-top:0;margin-top:8px;padding-top:0">'
                    f'<span style="color:var(--dim);font-size:11px;font-family:Verdana">'
                    f'{html_esc(bt_label)}</span><br>'
                    f'{price_line}{listing_html}{fee_html}'
                    f'{pay_html}'
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
            elif dep:
                # Deposit recorded but never priced — show what was paid, not a total.
                finance_html = (
                    f'<div class="tot" style="border-top:0;margin-top:8px;padding-top:0">'
                    f'<span style="color:var(--dim);font-size:11px;font-family:Verdana">'
                    f'No pricing recorded</span>'
                    f'{pay_html}</div>'
                )
            else:
                finance_html = (
                    f'<p style="color:var(--dim);font-size:13px;font-family:Verdana;margin:8px 0 0">'
                    f'No pricing recorded</p>'
                )

            # Notes — own dim heading, omitted entirely when empty. white-space:pre-wrap
            # keeps the author's line breaks; html_esc keeps any markup inert.
            notes_val = (b.get("notes") or "").strip()
            if notes_val:
                notes_html = (
                    f'<div style="margin-top:14px">'
                    f'<h3 style="font:11px Verdana,sans-serif;text-transform:uppercase;'
                    f'letter-spacing:.1em;color:var(--dim);margin:0 0 4px">Notes</h3>'
                    f'<p style="margin:0;font:13px/1.5 Verdana,sans-serif;'
                    f'white-space:pre-wrap">{html_esc(notes_val)}</p></div>'
                )
            else:
                notes_html = ""

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

            # Source is optional, so the line disappears rather than reading "—".
            src_val = (b.get("source") or "").strip()
            source_line = f'Source: {html_esc(src_val)}<br>' if src_val else ""

            done_url = f"/admin?tab=block{kp}"
            confirm_html = (
                f'<div class="result" style="margin-bottom:20px">'
                f'<h2>Details</h2>'
                f'<p style="margin-bottom:4px"><b>{html_esc(b.get("label","?"))}</b><br>'
                f'Houses: {html_esc(houses_str)}<br>'
                f'{html_esc(_fmt_range(b.get("checkin",""), b.get("checkout","")))}<br>'
                f'{source_line}'
                f'{_provenance(b)}</p>'
                f'{finance_html}'
                f'{_payments_section(b, kp, confirm_rm)}'
                f'{_expenses_section(b, kp, confirm_rm)}'
                f'{notes_html}'
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
                    f'{html_esc(_fmt_range(b.get("checkin",""), b.get("checkout","")))}'
                    f' &nbsp;<a href="{det_url}" style="color:var(--dim);font-size:12px">[details]</a>'
                    f'</li>'
                )
            blocks_html = f'<ul style="padding-left:20px;margin:8px 0 0">{rows}</ul>'
        else:
            blocks_html = '<p style="color:var(--dim);margin:8px 0 0">No upcoming reservations.</p>'

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
        for bt_id, bt_lbl in [("", "No booking type"), ("cash", "Cash"), ("airbnb", "Airbnb (15.5%)"),
                                ("monobank", "Site — UA card (1.3%)"), ("stripe", "Site — Int'l card (5.5%)")]:
            sel_a = " selected" if pf.get("btype", "") == bt_id else ""
            btype_opts += f'<option value="{bt_id}"{sel_a}>{bt_lbl}</option>'
        ev_chk = " checked" if pf.get("event") in ("on", "1", "true") else ""
        # The rental fee is required, so it lives outside the disclosure below: a
        # required control inside a closed <details> cannot be focused, and the
        # browser refuses the submit without ever showing the user why.
        fee_and_source = (
            f'<fieldset><legend>Rental fee</legend><div class="row">'
            f'<label>Rental fee ($) <input type="number" name="rental_fee" '
            f'min="0.01" step="0.01" required '
            f'value="{html_esc(pf.get("rental_fee", ""))}" placeholder="0.00" '
            f'style="width:110px"></label>'
            f'<label>Source {_source_select(pf.get("source", ""))}</label>'
            f'</div>'
            f'<p style="color:var(--dim);font-size:11px;font-family:Verdana;'
            f'margin:8px 0 0">What the guest is charged for the stay. Source is '
            f'optional.</p></fieldset>'
        )

        pricing_expander = (
            f'<details{popen} style="margin-top:12px">'
            f'<summary style="cursor:pointer;color:var(--dim);font:12px Verdana">Rate calculator (optional — for bonus tracking)</summary>'
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
            f'</div>'
            f'<div class="row" style="margin-top:10px">'
            f'<label>Downpayment ($) <input type="number" name="deposit" min="0" step="0.01" '
            f'value="{html_esc(pf.get("deposit",""))}" placeholder="0.00" style="width:90px"></label>'
            f'</div>'
            f'</div></details>'
        )

        notes_field = (
            f'<label style="display:block;margin-top:12px">'
            f'<span style="display:block;color:var(--dim);font:12px Verdana;margin-bottom:4px">'
            f'Notes (optional)</span>'
            f'<textarea name="notes" rows="3" placeholder="Anything worth remembering about this booking" '
            f'style="width:100%;max-width:420px;box-sizing:border-box;background:var(--bg);'
            f'color:var(--ink);border:1px solid #333c36;border-radius:4px;padding:6px 8px;'
            f'font:12px Verdana,sans-serif;resize:vertical">'
            f'{html_esc(pf.get("notes",""))}</textarea></label>'
        )

        if msg or price_warning:
            ok = msg.lower().startswith("reservation") and "conflict" not in msg.lower()
            msg_color = "var(--ok)" if ok else "var(--err)"
            msg_line = (f'<p style="margin:0;color:{msg_color}">{html_esc(msg)}</p>'
                        if msg else "")
            warn_line = ""
            if price_warning:
                warn_line = (
                    f'<p style="margin:6px 0 0;color:var(--accent);font-size:12px">'
                    f'{html_esc(price_warning)}</p>'
                )
            msg_html = f'<div class="result">{msg_line}{warn_line}</div>'
        else:
            msg_html = ""

        edit_id = pf.get("edit_id", "")
        edit_hidden = f'<input type="hidden" name="edit_id" value="{html_esc(edit_id)}">' if edit_id else ""
        block_btn = "Update reservation" if edit_id else "Add reservation"
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
            f'<fieldset><legend>{"Edit reservation" if edit_id else "Add reservation"}</legend><div class="row">'
            f'<label>Stay <input type="text" id="admin_dates" name="dates" '
            f'placeholder="check-in → check-out" value="{dates_val}" required style="min-width:240px">'
            f'</label></div></fieldset>'
            f'<fieldset><legend>Houses</legend><div class="row">{house_checks}</div></fieldset>'
            f'<fieldset><legend>Guest / reason</legend>'
            f'<input type="text" name="label" value="{label_val}" '
            f'placeholder="Guest name or reason" required style="min-width:260px">'
            f'{notes_field}'
            f'{pricing_expander}</fieldset>'
            f'{fee_and_source}'
            f'<button type="submit">{block_btn}</button>'
            f'<a href="{html_esc(back_href)}" class="btn-sec" style="margin-left:10px">← Calculator</a>'
            f'</form>'
            f'<div class="result" style="margin-top:26px">'
            f'<h2>Upcoming reservations</h2>'
            f'{blocks_html}'
            f'</div>'
        )

    if tab == "block":
        flatpickr_init = (
            f'<script>flatpickr("#admin_dates",{{mode:"range",dateFormat:"Y-m-d",'
            f'minDate:"{_RETRO_FLOOR.isoformat()}",showMonths:2}});</script>'
        )
    elif tab == "price":
        flatpickr_init = f'<script>{_avail_js(_RETRO_FLOOR.isoformat())}</script>'
    else:
        flatpickr_init = ""

    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Zubyria Admin</title>'
        f'{FLATPICKR_CSS}'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        f'<h1>Zubyria <b>Admin</b></h1>'
        f'<div class="sub">Reservations &amp; finances</div>'
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

        tab = _TAB_ALIASES.get(qs.get("tab", "block"), qs.get("tab", "block"))
        action = qs.get("action", "")
        month_str = qs.get("month", "")
        msg = ""
        price_warning = ""
        prefill = dict(qs)
        status = 200

        # Block actions (only relevant in block tab)
        if action == "add_block":
            edit_id = qs.get("edit_id", "").strip()
            dates_str = qs.get("dates", "")
            houses = [h for h in props if qs.get(f"h_{h}") == "on"]
            label = qs.get("label", "").strip()
            notes = qs.get("notes", "").strip()
            try:
                deposit = float(qs.get("deposit", "") or 0)
            except ValueError:
                deposit = 0.0
            if deposit < 0:
                deposit = 0.0
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
                _floor_err = _date_floor_error(ci, admin=True)
                if _floor_err:
                    raise ValueError(_floor_err)

                # The record being replaced, read once: an edit recreates the item,
                # so everything that lives on the reservation rather than in the
                # form (ledgers, source, price reason) is carried across from here.
                orig = None
                if edit_id:
                    for _ob in _get_store().list_blocks():
                        if _ob.get("sk") == edit_id:
                            orig = _ob
                            break

                # Both refuse the save outright (400) rather than writing a
                # reservation with no price or an unrecognised channel.
                rental_fee = _rental_fee_from(qs)
                source = _source_from(qs, orig)

                # Snapshot computation if btype provided
                snapshot = None
                quote_params_out = None
                snap_btype = qs.get("btype", "").strip()
                if snap_btype and snap_btype in _PRICED_BTYPES:
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
                                # kept so a manual override can re-derive fees:
                                # cleaning is a fixed cost and is never discounted
                                "cleaning_total": snap_q.cleaning_total,
                                "rental_price": snap_q.rental_price,
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

                # The submitted rental fee is the price. Where the calculator also
                # produced one, a fee that differs from it is a manual override —
                # that is also how an override is cleared without a separate revert,
                # by submitting the calculated figure. The reason comes from the form
                # when the form carries the field, else off the record being edited,
                # so a programmatic re-save cannot orphan an override's explanation.
                price_note = (qs.get("price_note", "").strip() if "price_note" in qs
                              else ((orig or {}).get("price_note") or "").strip())
                ov_in = 0.0
                _calc = float((snapshot or {}).get("subtotal") or 0)
                if snapshot and abs(rental_fee - _calc) > 0.005:
                    ov_in = rental_fee
                if ov_in and snapshot:
                    cleaning = float(snapshot.get("cleaning_total", 0) or 0)
                    if ov_in >= cleaning:
                        snapshot = _apply_override(snapshot, snap_btype, ov_in)
                        if edit_id:
                            price_warning = (
                                f"Custom price ${ov_in:,.2f} kept from before this "
                                f"change — re-check that it still applies to the new "
                                f"dates/params."
                            )
                        else:
                            price_warning = (
                                f"Rental fee ${ov_in:,.2f} recorded as a custom price "
                                f"— the calculated price for this stay is "
                                f"${_calc:,.2f}."
                            )
                    else:
                        ov_in = 0.0
                        price_warning = (
                            f"Rental fee ${rental_fee:,.2f} is below the cleaning cost "
                            f"(${cleaning:,.2f}), so the calculated price was kept."
                        )

                # With no calculator inputs the rental fee alone is the price, so the
                # record still gets a snapshot and reads like every other priced
                # reservation. Nothing recalculates under it, hence no override and
                # no cleaning split — the fee is the whole of it.
                if snapshot is None:
                    _derived = _recompute_from_subtotal(rental_fee, 0.0, "cash")
                    snapshot = {
                        "subtotal": rental_fee,
                        "cleaning_total": 0.0,
                        "rental_price": rental_fee,
                        "gross_profit": _derived["gross_profit"],
                        "bonus": _derived["bonus"],
                    }

                # Ledger carried server-side off the block being edited — payments are
                # too large and too sensitive to thread through a query string, and
                # the edit path recreates the item, which would otherwise orphan them.
                carry_payments = [dict(p) for p in _payments_of(orig)] if orig else []
                carry_expenses = [dict(e) for e in _expenses_of(orig)] if orig else []
                try:
                    exp_amt = float(qs.get("exp_amount", "") or 0)
                except ValueError:
                    exp_amt = 0.0
                if exp_amt:
                    _ed = (qs.get("exp_date") or "").strip() or date.today().isoformat()
                    try:
                        date.fromisoformat(_ed)
                    except ValueError:
                        _ed = date.today().isoformat()
                    carry_expenses.append(_new_payment(
                        exp_amt, _ed, qs.get("exp_note", "").strip()))

                if deposit:
                    # Add form only: this is payment #1 for a brand-new booking.
                    carry_payments.append(_new_payment(
                        deposit, date.today().isoformat(), "downpayment"))
                try:
                    pay_amt = float(qs.get("pay_amount", "") or 0)
                except ValueError:
                    pay_amt = 0.0
                if pay_amt:
                    # Entered in the Payments section of the Edit screen. It reaches
                    # the store only via add_block, which rejects on conflict before
                    # writing anything — so a conflicting save persists neither the
                    # block nor the payment.
                    _pd = (qs.get("pay_date") or "").strip() or date.today().isoformat()
                    try:
                        date.fromisoformat(_pd)
                    except ValueError:
                        _pd = date.today().isoformat()
                    carry_payments.append(_new_payment(
                        pay_amt, _pd, qs.get("pay_note", "").strip()))

                r = _get_store().add_block(
                    houses, ci, co, label,
                    created_by="admin",
                    edited_from=edit_id or None,
                    exclude_id=edit_id or None,
                    snapshot=snapshot,
                    quote_params=quote_params_out,
                    btype=snap_btype or None,
                    notes=notes or None,
                    override_subtotal=ov_in or None,
                    price_note=price_note or None,
                    payments=carry_payments or None,
                    expenses=carry_expenses or None,
                    source=source or None,
                )
                if r["ok"]:
                    if edit_id:
                        # Supersede, not cancel: the money moved to the new record,
                        # so finances must not count both copies.
                        _get_store().supersede_block(edit_id, r.get("id") or "")
                        msg = f"Reservation updated: {label} ({_fmt_range(parts[0], parts[1])})"
                    else:
                        msg = f"Reservation added: {label} ({_fmt_range(parts[0], parts[1])})"
                    prefill = {}
                else:
                    conflicts = ", ".join(r["conflicts"])
                    msg = f"Conflict: those dates are already taken by: {conflicts}"
            except _Rejected as e:
                # A rule the form itself enforces: reaching here means the request
                # bypassed it, so answer 400 rather than a soft in-page message.
                msg = f"Error: {e}"
                status = 400
            except ValueError as e:
                msg = f"Error: {e}"
            tab = "block"

        elif action in ("add_expense", "remove_expense"):
            block_id = qs.get("block_id", "")
            target = None
            for b in _get_store().list_blocks():
                if b.get("sk") == block_id:
                    target = b
                    break
            if not target:
                msg = "Error: reservation not found."
            else:
                ledger = [dict(e) for e in _expenses_of(target)]
                if action == "remove_expense":
                    eid = qs.get("expense_id", "")
                    kept = [e for e in ledger if e.get("id") != eid]
                    if len(kept) == len(ledger):
                        msg = "Error: expense not found."
                    else:
                        _get_store().set_expenses(block_id, kept)
                        msg = "Expense removed."
                else:
                    try:
                        amt = float(qs.get("amount", "") or 0)
                    except ValueError:
                        amt = 0.0
                    edate = (qs.get("exp_date", "") or "").strip() or date.today().isoformat()
                    try:
                        date.fromisoformat(edate)
                    except ValueError:
                        edate = ""
                    if not amt:
                        msg = "Error: enter an expense amount."
                    elif not edate:
                        msg = "Error: enter a valid expense date."
                    else:
                        ledger.append(_new_payment(amt, edate, qs.get("exp_note", "").strip()))
                        _get_store().set_expenses(block_id, ledger)
                        msg = f"Expense of {_money(amt)} recorded."
            tab = "block"
            if qs.get("back") == "edit":
                qs = dict(qs, edit_id=block_id)
                prefill = dict(qs)
                action = ""
            elif not qs.get("details"):
                qs = dict(qs, details=block_id)

        elif action in ("add_payment", "remove_payment"):
            block_id = qs.get("block_id", "")
            target = None
            for b in _get_store().list_blocks():
                if b.get("sk") == block_id:
                    target = b
                    break
            if not target:
                msg = "Error: reservation not found."
            else:
                # Derive from the migrated view, so a legacy deposit is materialised
                # into the ledger by the first write rather than silently dropped.
                ledger = [dict(p) for p in _payments_of(target)]
                if action == "remove_payment":
                    pid = qs.get("payment_id", "")
                    kept = [p for p in ledger if p.get("id") != pid]
                    if len(kept) == len(ledger):
                        msg = "Error: payment not found."
                    else:
                        _get_store().set_payments(block_id, kept)
                        msg = "Payment removed."
                else:
                    try:
                        amt = float(qs.get("amount", "") or 0)
                    except ValueError:
                        amt = 0.0
                    pdate = (qs.get("pay_date", "") or "").strip() or date.today().isoformat()
                    try:
                        date.fromisoformat(pdate)
                    except ValueError:
                        pdate = ""
                    if not amt:
                        msg = "Error: enter a payment amount."
                    elif not pdate:
                        msg = "Error: enter a valid payment date."
                    else:
                        ledger.append(_new_payment(amt, pdate, qs.get("pay_note", "").strip()))
                        _get_store().set_payments(block_id, ledger)
                        msg = f"Payment of ${amt:,.2f} recorded."
            tab = "block"
            if qs.get("back") == "edit":
                qs = dict(qs, edit_id=block_id)
                prefill = dict(qs)
                action = ""
            elif not qs.get("details"):
                qs = dict(qs, details=block_id)

        elif action in ("set_price", "revert_price"):
            block_id = qs.get("block_id", "")
            target = None
            for b in _get_store().list_blocks():
                if b.get("sk") == block_id and b.get("status") == "active":
                    target = b
                    break
            if not target:
                msg = "Error: reservation not found."
            elif not target.get("snapshot"):
                msg = "Error: this reservation has no price to override."
            elif action == "revert_price":
                _get_store().set_price_override(
                    block_id, _revert_override(target["snapshot"]), None, None)
                msg = "Price reverted to calculated."
            else:
                snap = target["snapshot"]
                cleaning = float(snap.get("cleaning_total", 0) or 0)
                try:
                    new_price = float(qs.get("price", "") or 0)
                except ValueError:
                    new_price = -1.0
                # Same floor as the rental fee on the forms: this action is the only
                # other way to write a price, so it must not be a way around the rule.
                if new_price <= 0:
                    msg = "Error: enter a price greater than zero."
                    status = 400
                elif new_price < cleaning:
                    msg = (f"Error: price must be at least the cleaning cost "
                           f"(${cleaning:,.2f}).")
                else:
                    _get_store().set_price_override(
                        block_id,
                        _apply_override(snap, target.get("btype", "cash"), new_price),
                        new_price,
                        qs.get("price_note", "").strip(),
                    )
                    msg = f"Price set to ${new_price:,.2f}."
            tab = "block"
            if qs.get("back") == "edit" or action == "revert_price":
                qs = dict(qs, edit_id=block_id)
                prefill = dict(qs)
                action = ""
            elif not qs.get("details"):
                qs = dict(qs, details=block_id)

        elif action == "cancel_block":
            block_id = qs.get("block_id", "")
            if block_id:
                try:
                    _get_store().cancel_block(block_id)
                    msg = "Reservation cancelled."
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
                _floor_err = _date_floor_error(price_checkin, admin=True)
                blocked = [h for h in price_bookings if h in price_avail and not price_avail[h]["available"]]
                if _floor_err:
                    price_error = f"Check your inputs: {_floor_err}"
                elif blocked:
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
                    # The rental fee is required on the add form, so hand it the price
                    # just quoted — it stays editable before the reservation is saved.
                    f"&rental_fee={q_price.subtotal:.2f}"
                    f"{event_part}{kp_link}"
                )

        try:
            blocks = _get_store().list_blocks()
        except Exception:
            blocks = []

        # Resolve the block being edited (drives the unified Edit screen)
        edit_target = None
        _eid = (qs.get("edit_id") or "").strip()
        if _eid and tab == "block":
            for b in blocks:
                if b.get("sk") == _eid and b.get("status") == "active":
                    edit_target = b
                    break

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
        resp = _resp(status, render_admin(
            props, blocks, msg=msg, prefill=prefill, admin_key=admin_key,
            tab=tab, q=q_price, price_error=price_error, price_params=dict(qs),
            avail=price_avail, block_url=price_block_url,
            month_str=month_str, detail_block=detail_block,
            confirm_cancel=confirm_cancel, price_warning=price_warning,
            confirm_rm=qs.get("confirm_rm", ""), qs_bonus=dict(qs),
            edit_target=edit_target,
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

        _floor_err = _date_floor_error(checkin, admin=False)
        if _floor_err:
            raise ValueError(_floor_err)

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
        block_url = (f"/admin?tab=block&dates={date_str}&{houses_qs}"
                     f"&rental_fee={q.subtotal:.2f}{kp}")

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
