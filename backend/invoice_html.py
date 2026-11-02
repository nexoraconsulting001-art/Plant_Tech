"""The bill as a web page: what the customer opens from the WhatsApp link, and what the app prints to PDF.

All text from the shop or customer is HTML-escaped. Prices include GST; the GST share is listed per rate.
A cancelled bill says so at the top (and which bill replaced it); a corrected bill says which bill it replaces.

GST shops get a tax invoice (CGST rules 46, 46A, 49; layout pending a CA's sign-off, docs/GST-INVOICE.md): the title
(Tax Invoice / Bill of Supply / Invoice-cum-Bill of Supply, from gst.title), the shop's GSTIN and state, HSN,
taxable value, rate and tax under each item, place of supply and the reverse-charge line, a table of tax by HSN
and rate (CGST + SGST, or IGST to another state) and the signatory. The numbers come from gst.split_bill, so
lines, rate totals and HSN totals add up to the bill's total to the paisa. Unregistered shops and "Bill without
GST" bills print as a plain "Bill", as before.

The customer's link (`public`) shows the phone masked (last four digits) and the customer's address only when
the bill needs it (a B2B bill with the buyer's GSTIN, or one of B2C_ADDRESS_FROM or more); the shop's own print
shows both in full.
"""
import re
from datetime import datetime
from html import escape

import gst
from common import IST
from units import short

ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
        "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def inr(amount: float) -> str:
    """Indian grouping: 1234567.5 -> '₹12,34,567.50' (paise dropped when zero)."""
    amount = round(float(amount), 2)
    rupees = int(abs(amount))
    paise = round((abs(amount) - rupees) * 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{s}" + (f".{paise:02d}" if paise else "")


def _below_hundred(n: int) -> str:
    return ONES[n] if n < 20 else (TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else ""))


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(ONES[n // 100] + " Hundred")
        n %= 100
    if n:
        parts.append(_below_hundred(n))
    return " ".join(parts)


def amount_in_words(amount: float) -> str:
    """1380 -> 'Rupees One Thousand Three Hundred Eighty Only' (Indian lakh/crore system)."""
    amount = round(float(amount), 2)
    rupees, paise = int(amount), round((amount - int(amount)) * 100)
    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"
    parts = []
    for size, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if rupees >= size:
            parts.append(_below_thousand(rupees // size) + " " + label)
            rupees %= size
    if rupees:
        parts.append(_below_thousand(rupees))
    words = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if paise:
        words += " and " + _below_hundred(paise) + " Paise"
    return words + " Only"


def _when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone(IST)
        return dt.strftime("%d %b %Y, %I:%M %p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")
    except (TypeError, ValueError):
        return ""


B2C_ADDRESS_FROM = 50_000  # a bill of this much or more needs the buyer's address even without a GSTIN (rule 46)


def mask_phone(phone: str) -> str:
    """'+919811122334' -> '+91 XXXXXX2334' (the customer's link shows only the last four digits)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return ""
    local = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
    return ("+91 " if local != digits else "") + "X" * (len(local) - 4) + local[-4:]


def pct(rate) -> str:
    """A GST rate as printed: 18, 0.25, 2.5 (never 18.0)."""
    return f"{float(rate):g}"


def _tax_note(line: gst.TaxLine, shop_scheme: str) -> str:
    """The GST details under an item: HSN, taxable value, rate and tax (a composition shop's bill shows no tax)."""
    parts = [f"HSN {escape(line.hsn)}"] if line.hsn else []
    if shop_scheme == "composition":
        return " · ".join(parts)
    if line.taxed:
        parts += [f"Taxable {inr(line.taxable)}", f"GST {pct(line.rate)}% {inr(line.tax)}"]
    else:
        parts.append("Exempt" if line.exempt else "GST 0%")
    return " · ".join(parts)


def _tax_table(split: gst.Split) -> str:
    """Tax by HSN and rate (as GSTR-1 asks for it): taxable value and CGST + SGST, or IGST, with the totals."""
    keys = ("igst",) if split.inter_state else ("cgst", "sgst")
    rows = [f'<tr><td>{escape(r["hsn"]) or "–"}</td><td class="r">{pct(r["rate"])}%</td><td class="r">{inr(r["taxable"])}</td>'
            + "".join(f'<td class="r">{inr(r[k])}</td>' for k in keys) + "</tr>" for r in split.by_hsn]
    total = (f'<tr class="b"><td colspan="2">Total</td><td class="r">{inr(split.taxable)}</td>'
             + "".join(f'<td class="r">{inr(split.total(k))}</td>' for k in keys) + "</tr>")
    head = "".join(f'<th class="r">{k.upper()}</th>' for k in keys)
    return (f'<table class="hsn"><thead><tr><th>HSN</th><th class="r">GST</th><th class="r">Taxable</th>{head}</tr>'
            f'</thead><tbody>{"".join(rows)}{total}</tbody></table>')


def render(inv: dict, biz: dict, *, public: bool = True, logo_src: str | None = None,
           codes: dict[str, dict] | None = None) -> str:
    """The bill page. `public`: the customer's link (phone masked, address only when the bill needs it, a print
    button). `codes`: product id -> {hsn, exempt} for lines saved without them (from gst.bill_codes)."""
    e = lambda s: escape(str(s or ""))  # noqa: E731
    shop_scheme = gst.scheme(biz)
    split = gst.split_bill(inv, biz, codes)
    # "Bill without GST" (gst_removed): decision D6 is open, so such a bill still prints as before, a plain bill
    # with no tax. gst.title() returns "Bill" for it; change that only together with D6.
    doc = gst.title(shop_scheme, split, bool(inv.get("gst_removed")))
    gst_doc = doc != gst.PLAIN
    tax_doc = doc in (gst.TAX_INVOICE, gst.BOTH)
    rows = []
    for i, (it, line) in enumerate(zip(inv.get("items", []), split.lines)):
        qty = it["quantity"]
        qty_s = " ".join(x for x in (str(int(qty)) if float(qty).is_integer() else f"{qty:g}", short(it.get("unit"))) if x)
        note = _tax_note(line, shop_scheme) if gst_doc else ""
        rows.append(
            f'<tr class="{"z" if i % 2 == 0 else ""}"><td>{e(it["name"])}{f"<div class=tx>{note}</div>" if note else ""}</td>'
            f'<td class="c">{qty_s}</td><td class="r">{inr(it["price"])}</td><td class="r b">{inr(it["price"] * qty)}</td></tr>')
    totals = []
    if inv.get("discount"):
        totals.append(f'<div class="t"><span>Subtotal</span><span>{inr(inv["subtotal"])}</span></div>')
        totals.append(f'<div class="t"><span>Discount</span><span>−{inr(inv["discount"])}</span></div>')
    for g in split.by_rate:
        if g["rate"]:
            totals.append(f'<div class="t m"><span>Includes GST {pct(g["rate"])}%</span><span>{inr(g["tax"])}</span></div>')
    shop_line = " · ".join(x for x in (e(biz.get("address")), e(biz.get("phone"))) if x)
    gstin = ""
    if biz.get("gst_registered") and biz.get("gstin"):
        state = gst.state_label(gst.state_of(biz["gstin"]))
        gstin = f'<div class="s">GSTIN {e(biz["gstin"])}{f" · {e(state)}" if state else ""}</div>'
    customer = e(inv.get("customer_name") or "Walk-in customer")
    b2b = bool(inv.get("customer_gstin"))
    buyer_gstin = f'<div class="m">GSTIN {e(inv["customer_gstin"])}</div>' if b2b else ""
    shown_phone = mask_phone(inv.get("customer_phone", "")) if public else inv.get("customer_phone")
    phone = f'<div class="m">{e(shown_phone)}</div>' if shown_phone else ""
    # The customer's link shows the address only when the bill needs it; the shop's own print always does.
    needs_address = b2b or float(inv.get("grand_total") or 0) >= B2C_ADDRESS_FROM
    address = (f'<div class="m addr">{e(inv["customer_address"])}</div>'
               if inv.get("customer_address") and (not public or needs_address) else "")
    paid_to = f'<div class="m">Paid to UPI ID {e(inv["upi_id"])}</div>' if inv.get("upi_id") else ""
    footer = e(biz.get("footer_note") or "Thank you, visit again!")
    button = ('<button class="dl" onclick="window.print()">Download / Print PDF</button>' if public else "")
    logo = f'<img class="logo" src="{e(logo_src)}" alt="">' if logo_src else ""
    cancelled = inv.get("status") == "Cancelled"
    stamp = ""
    if cancelled:
        by = (inv.get("replaced_by") or {}).get("invoice_number")
        when = _when(inv.get("cancelled_at", ""))
        stamp = (f'<div class="void"><b>CANCELLED</b><span>{f"Replaced by {e(by)}" if by else "This bill is no longer valid"}'
                 f'{f" · {when}" if when else ""}</span></div>')
    replaces = (f'<div class="m">Replaces {e(inv["replaces"]["invoice_number"])}</div>'
                if (inv.get("replaces") or {}).get("invoice_number") else "")
    status = ('<span class="paid x">CANCELLED</span>' if cancelled
              else f'<span class="paid">PAID · {e(inv.get("payment_method"))}</span>')
    title_band = ""
    if gst_doc:
        note = f'<div class="m">{gst.COMPOSITION_NOTE}</div>' if shop_scheme == "composition" else ""
        title_band = f'<div class="doc"><b>{doc.upper()}</b>{note}</div>'
    supply = ""
    if tax_doc:
        pos = gst.state_label(gst.place_of_supply(inv, biz))
        supply = (f'<div class="sup">{f"<span>Place of supply {e(pos)}</span>" if pos else ""}'
                  f'<span>Reverse charge: No</span></div>')
    tax_table = _tax_table(split) if tax_doc else ""
    sign = (f'<div class="sign"><div>For {e(biz.get("name"))}</div><div class="line">Authorised signatory</div></div>'
            if gst_doc else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{"Cancelled bill" if cancelled else doc} {e(inv.get("invoice_number"))} · {e(biz.get("name"))}</title>
<style>
@page {{ size: A5; margin: 10mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #EEF3EF; font-family: "Mukta", "Noto Sans", system-ui, sans-serif; color: #10201A; }}
.wrap {{ max-width: 520px; margin: 0 auto; padding: 14px; }}
.card {{ background: #fff; border-radius: 16px; overflow: hidden; box-shadow: 0 2px 10px rgba(16,32,26,.08); }}
.head {{ background: #1B5E3B; color: #fff; padding: 16px 18px; display: flex; align-items: center; gap: 14px; }}
.logo {{ width: 60px; height: 60px; flex: none; object-fit: contain; background: #fff; border-radius: 12px; padding: 4px; }}
.head h1 {{ margin: 0; font-size: 22px; }}
.s {{ font-size: 13px; color: #D5E9DB; margin-top: 2px; }}
.doc {{ padding: 10px 18px 0; text-align: center; font-size: 13px; }}
.doc b {{ font-size: 15px; letter-spacing: .14em; }}
.meta {{ display: flex; justify-content: space-between; gap: 10px; padding: 12px 18px; border-bottom: 1px dashed #C7D6CC; font-size: 14px; }}
.m {{ color: #4B5F55; }}
.meta b {{ font-size: 16px; }}
.who {{ flex: 1; min-width: 0; }}
.addr {{ white-space: pre-line; overflow-wrap: anywhere; }}
.sup {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 12px; padding: 6px 18px; font-size: 12px; color: #4B5F55; border-bottom: 1px dashed #C7D6CC; }}
table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
th {{ text-align: left; font-size: 12px; text-transform: uppercase; color: #4B5F55; padding: 8px 18px 4px; }}
td {{ padding: 8px 18px; }}
tr.z td {{ background: #F5F8F5; }}
.tx {{ font-size: 12px; color: #4B5F55; margin-top: 2px; }}
.hsn {{ font-size: 12px; border-top: 1px dashed #C7D6CC; }}
.hsn th, .hsn td {{ padding: 4px 6px; }}
.hsn th:first-child, .hsn td:first-child {{ padding-left: 18px; }}
.hsn th:last-child, .hsn td:last-child {{ padding-right: 18px; }}
.c {{ text-align: center; }} .r {{ text-align: right; }} .b {{ font-weight: 700; }}
.tot {{ padding: 10px 18px 16px; border-top: 1px dashed #C7D6CC; }}
.t {{ display: flex; justify-content: space-between; font-size: 15px; margin: 2px 0; }}
.grand {{ display: flex; justify-content: space-between; align-items: baseline; margin-top: 6px; }}
.grand span:first-child {{ font-size: 18px; font-weight: 800; }}
.grand span:last-child {{ font-size: 32px; font-weight: 800; color: #134A2E; }}
.words {{ font-size: 13px; color: #4B5F55; }}
.foot {{ display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }}
.sign {{ margin-top: 14px; text-align: right; font-size: 13px; color: #4B5F55; }}
.sign .line {{ display: inline-block; margin-top: 30px; padding-top: 4px; border-top: 1px solid #C7D6CC; min-width: 160px; text-align: center; }}
.void {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; padding: 12px 18px; background: #FDECEC; color: #A32020; border-bottom: 1px solid #F3C7C7; font-size: 14px; }}
.void b {{ font-size: 18px; letter-spacing: .12em; }}
.paid.x {{ border-color: #A32020; color: #A32020; }}
.paid {{ border: 2px solid #1B5E3B; border-radius: 8px; padding: 3px 12px; color: #1B5E3B; font-weight: 800; letter-spacing: .08em; }}
.brand {{ text-align: center; font-size: 12px; color: #6B7C73; margin-top: 12px; }}
.dl {{ display: block; width: 100%; margin-top: 14px; min-height: 56px; border: 0; border-radius: 14px; background: #1B5E3B; color: #fff; font: inherit; font-size: 18px; font-weight: 700; }}
@media print {{ body {{ background: #fff; }} .wrap {{ padding: 0; }} .card {{ box-shadow: none; }} .dl {{ display: none; }} }}
</style></head>
<body><div class="wrap"><article class="card">
<header class="head">{logo}<div><h1>{e(biz.get("name") or "Bill")}</h1><div class="s">{shop_line}</div>{gstin}</div></header>{stamp}{title_band}
<div class="meta"><div class="who"><div class="m">Bill to</div><b>{customer}</b>{buyer_gstin}{phone}{address}</div>
<div style="text-align:right"><div class="m">Bill no.</div><b>{e(inv.get("invoice_number"))}</b><div class="m">{_when(inv.get("created_at", ""))}</div>{replaces}</div></div>{supply}
<table><thead><tr><th>Item</th><th class="c">Qty</th><th class="r">Rate</th><th class="r">Amount</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>{tax_table}
<div class="tot">{"".join(totals)}
<div class="grand"><span>Total</span><span>{inr(inv.get("grand_total", 0))}</span></div>
<div class="words">{amount_in_words(inv.get("grand_total", 0))}</div>{paid_to}
<div class="foot">{status}<span class="m">{footer}</span></div>{sign}
</div></article>{button}<div class="brand">Made with Hara Hisaab · हरा हिसाब</div></div></body></html>"""
