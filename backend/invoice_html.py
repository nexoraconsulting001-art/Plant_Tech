"""The bill as a web page: what the customer opens from the WhatsApp link, and what the app prints to PDF.

All text from the shop or customer is HTML-escaped. Prices include GST; the GST share is listed per rate.
"""
from datetime import datetime
from html import escape

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


def render(inv: dict, biz: dict, *, public: bool = True, logo_src: str | None = None) -> str:
    e = lambda s: escape(str(s or ""))  # noqa: E731
    rows = []
    for i, it in enumerate(inv.get("items", [])):
        qty = it["quantity"]
        qty_s = " ".join(x for x in (str(int(qty)) if float(qty).is_integer() else f"{qty:g}", short(it.get("unit"))) if x)
        rows.append(
            f'<tr class="{"z" if i % 2 == 0 else ""}"><td>{e(it["name"])}</td><td class="c">{qty_s}</td>'
            f'<td class="r">{inr(it["price"])}</td><td class="r b">{inr(it["price"] * qty)}</td></tr>')
    totals = []
    if inv.get("discount"):
        totals.append(f'<div class="t"><span>Subtotal</span><span>{inr(inv["subtotal"])}</span></div>')
        totals.append(f'<div class="t"><span>Discount</span><span>−{inr(inv["discount"])}</span></div>')
    for g in inv.get("gst_breakup", []):
        totals.append(f'<div class="t m"><span>Includes GST {g["rate"]:g}%</span><span>{inr(g["tax"])}</span></div>')
    shop_line = " · ".join(x for x in (e(biz.get("address")), e(biz.get("phone"))) if x)
    gstin = f'<div class="s">GSTIN {e(biz.get("gstin"))}</div>' if biz.get("gst_registered") and biz.get("gstin") else ""
    customer = e(inv.get("customer_name") or "Walk-in customer")
    phone = f'<div class="m">{e(inv["customer_phone"])}</div>' if inv.get("customer_phone") else ""
    address = f'<div class="m addr">{e(inv["customer_address"])}</div>' if inv.get("customer_address") else ""
    footer = e(biz.get("footer_note") or "Thank you, visit again!")
    button = ('<button class="dl" onclick="window.print()">Download / Print PDF</button>' if public else "")
    logo = f'<img class="logo" src="{e(logo_src)}" alt="">' if logo_src else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Bill {e(inv.get("invoice_number"))} · {e(biz.get("name"))}</title>
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
.meta {{ display: flex; justify-content: space-between; gap: 10px; padding: 12px 18px; border-bottom: 1px dashed #C7D6CC; font-size: 14px; }}
.m {{ color: #4B5F55; }}
.meta b {{ font-size: 16px; }}
.who {{ flex: 1; min-width: 0; }}
.addr {{ white-space: pre-line; overflow-wrap: anywhere; }}
table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
th {{ text-align: left; font-size: 12px; text-transform: uppercase; color: #4B5F55; padding: 8px 18px 4px; }}
td {{ padding: 8px 18px; }}
tr.z td {{ background: #F5F8F5; }}
.c {{ text-align: center; }} .r {{ text-align: right; }} .b {{ font-weight: 700; }}
.tot {{ padding: 10px 18px 16px; border-top: 1px dashed #C7D6CC; }}
.t {{ display: flex; justify-content: space-between; font-size: 15px; margin: 2px 0; }}
.grand {{ display: flex; justify-content: space-between; align-items: baseline; margin-top: 6px; }}
.grand span:first-child {{ font-size: 18px; font-weight: 800; }}
.grand span:last-child {{ font-size: 32px; font-weight: 800; color: #134A2E; }}
.words {{ font-size: 13px; color: #4B5F55; }}
.foot {{ display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }}
.paid {{ border: 2px solid #1B5E3B; border-radius: 8px; padding: 3px 12px; color: #1B5E3B; font-weight: 800; letter-spacing: .08em; }}
.brand {{ text-align: center; font-size: 12px; color: #6B7C73; margin-top: 12px; }}
.dl {{ display: block; width: 100%; margin-top: 14px; min-height: 56px; border: 0; border-radius: 14px; background: #1B5E3B; color: #fff; font: inherit; font-size: 18px; font-weight: 700; }}
@media print {{ body {{ background: #fff; }} .wrap {{ padding: 0; }} .card {{ box-shadow: none; }} .dl {{ display: none; }} }}
</style></head>
<body><div class="wrap"><article class="card">
<header class="head">{logo}<div><h1>{e(biz.get("name") or "Bill")}</h1><div class="s">{shop_line}</div>{gstin}</div></header>
<div class="meta"><div class="who"><div class="m">Bill to</div><b>{customer}</b>{phone}{address}</div>
<div style="text-align:right"><div class="m">Bill no.</div><b>{e(inv.get("invoice_number"))}</b><div class="m">{_when(inv.get("created_at", ""))}</div></div></div>
<table><thead><tr><th>Item</th><th class="c">Qty</th><th class="r">Rate</th><th class="r">Amount</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<div class="tot">{"".join(totals)}
<div class="grand"><span>Total</span><span>{inr(inv.get("grand_total", 0))}</span></div>
<div class="words">{amount_in_words(inv.get("grand_total", 0))}</div>
<div class="foot"><span class="paid">PAID · {e(inv.get("payment_method"))}</span><span class="m">{footer}</span></div>
</div></article>{button}<div class="brand">Made with Hara Hisaab · हरा हिसाब</div></div></body></html>"""
