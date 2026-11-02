"""GST returns from a shop's bills: GSTR-1 (outward supplies) and the GSTR-3B summary, for one month.

    data = await gstr.collect(db, business, "2026-10")
    gstr.gstr1_json(data)     the JSON the GST portal and the GSTN offline tool import
    gstr.gstr1_csv(data)      {section: CSV text} in the offline tool's sheet layout (b2b, b2cl, b2cs, ...)
    gstr.gstr3b(data)         table 3.1 and 3.2 totals

What each bill becomes (prices include GST; a line's taxable value is its `net` minus its `tax`, as printed):
- A bill with the buyer's GSTIN (`customer_gstin`, set once bills can carry one) is B2B; every other bill is B2C.
- Place of supply: the bill's `place_of_supply` when it has one, else the shop's state (an over-the-counter sale).
  Same state: CGST + SGST, half each (CGST gets the odd paisa); other state: IGST.
- B2C: inter-state bills over B2CL_LIMIT go to b2cl one by one, the rest are summed per place and rate (b2cs).
- 0% lines are nil-rated or exempt supplies (table 8, `nil`): `tax_class` on the line, product or category
  ("nil", "exempt", "non_gst"), else "exempt" (live plants, HSN 0602, are exempt).
- HSN summary (table 12), split B2B / B2C: the HSN on the line, else the product's, else its category's (WP-9 adds
  them). Lines without one are summed under "" and listed in `warnings`: the portal refuses a blank HSN.
- Documents issued (table 13): per number series, the first and last number, how many, how many cancelled.
- Cancelled bills (cancelled or replaced by a corrected bill) count only in table 13. A bill cancelled after its
  month ended is listed in `warnings`: a return already filed needs an amendment or credit note.
- Credit and debit notes (cdnr, cdnur) are empty until the app issues credit notes (Wave 2).
- Bills made with "Bill without GST" (decision D6 still open) carry 0% lines and so land in `nil`; they are listed
  in `warnings` for the shop's CA. Nothing here changes how they were billed.

Money is Decimal, rounded half up to the paisa at the end of each sum. The JSON follows the layout the GSTN offline
tool (3.x) produces; check a first file with the offline tool before filing, and have the CA sign it off (D7).
"""
import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from errors import render
from units import unit_of

B2CL_LIMIT = Decimal(100000)  # inter-state B2C invoices above this go to b2cl (from 1 Aug 2024)
JSON_VERSION = "GST3.1.8"
TAX_CLASSES = ("nil", "exempt", "non_gst")
DEFAULT_ZERO_CLASS = "exempt"
PAISA = Decimal("0.01")
ZERO = Decimal(0)

STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}
# GSTN unit quantity codes for the app's units (units.py)
UQC = {"pcs": "NOS", "bag": "BAG", "packet": "PAC", "tray": "OTH", "kg": "KGS", "g": "GMS", "litre": "LTR",
       "ml": "MLT", "metre": "MTR"}
UQC_NAMES = {"NOS": "NUMBERS", "BAG": "BAGS", "PAC": "PACKS", "OTH": "OTHERS", "KGS": "KILOGRAMS", "GMS": "GRAMMES",
             "LTR": "LITRES", "MLT": "MILILITRE", "MTR": "METERS"}
GSTIN = re.compile(r"^\d{2}[A-Z0-9]{13}$")
MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def d(x) -> Decimal:
    return Decimal(str(x or 0))


def r2(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


def num(x: Decimal) -> float:
    """A rounded amount as the JSON number the portal expects."""
    return float(r2(x))


def rate_num(rate: Decimal) -> float | int:
    return int(rate) if rate == rate.to_integral_value() else float(rate)


def state_of(gstin: str | None) -> str:
    g = (gstin or "").strip().upper()
    return g[:2] if GSTIN.match(g) and g[:2] in STATES else ""


def month_range(month: str) -> tuple[str, str]:
    """'2026-10' -> ('2026-10-01', '2026-10-31'), the date_key range of the month."""
    m = MONTH.match(month)
    if not m:
        raise ValueError("month must be YYYY-MM")
    y, mo = int(m[1]), int(m[2])
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return f"{month}-01", date.fromordinal(nxt.toordinal() - 1).isoformat()


def fp(month: str) -> str:
    """Return period as GSTN writes it: '2026-10' -> '102026'."""
    return month[5:7] + month[:4]


def series(number: str) -> tuple[str, int]:
    """'INV-0042' -> ('INV-', 42): bills are grouped into series by everything before the trailing digits."""
    m = re.match(r"^(.*?)(\d+)$", number or "")
    return (m[1], int(m[2])) if m else (number or "", 0)


@dataclass
class Tax:
    txval: Decimal = ZERO
    iamt: Decimal = ZERO
    camt: Decimal = ZERO
    samt: Decimal = ZERO
    csamt: Decimal = ZERO
    qty: Decimal = ZERO
    val: Decimal = ZERO

    def add(self, other: "Tax"):
        for f in ("txval", "iamt", "camt", "samt", "csamt", "qty", "val"):
            setattr(self, f, getattr(self, f) + getattr(other, f))

    def amounts(self) -> dict:
        return {"txval": num(self.txval), "iamt": num(self.iamt), "camt": num(self.camt), "samt": num(self.samt),
                "csamt": num(self.csamt)}


@dataclass
class Bill:
    number: str
    day: str
    value: Decimal
    pos: str
    inter: bool
    ctin: str
    name: str
    rates: dict = field(default_factory=dict)  # rate -> Tax (taxed lines only)


@dataclass
class Month:
    """Everything the returns need from one month of bills (built by `collect`)."""
    gstin: str
    state: str
    month: str
    bills: list = field(default_factory=list)  # live bills (Bill)
    nil: dict = field(default_factory=lambda: defaultdict(Tax))  # (sply_ty, class) -> Tax
    hsn: dict = field(default_factory=lambda: {"b2b": defaultdict(Tax), "b2c": defaultdict(Tax)})  # (hsn, uqc, rate) -> Tax
    hsn_desc: dict = field(default_factory=dict)
    docs: dict = field(default_factory=dict)  # series -> {"first", "last", "total", "cancel"}
    warnings: list = field(default_factory=list)


def split_tax(tax: Decimal, inter: bool) -> dict:
    tax = r2(tax)
    if inter:
        return {"iamt": tax, "camt": ZERO, "samt": ZERO}
    half = r2(tax / 2)
    return {"iamt": ZERO, "camt": half, "samt": tax - half}


def _line_tax(line: dict) -> tuple[Decimal, Decimal, Decimal]:
    """(rate, taxable value, tax) of one bill line, from what was printed on the bill."""
    rate = d(line.get("gst_percent"))
    if "net" in line:
        net, tax = d(line["net"]), d(line.get("tax"))
    else:  # very old bills: no per-line net; price includes GST, no discount spread
        net = d(line.get("price")) * d(line.get("quantity"))
        tax = net * rate / (100 + rate) if rate else ZERO
    return rate, net - tax, tax


async def collect(db, business: dict, month: str) -> Month:
    """Read the month's bills (and the products and categories they name) once, streaming, into a Month."""
    start, end = month_range(month)
    bid = business["id"]
    gstin = (business.get("gstin") or "").strip().upper()
    out = Month(gstin=gstin, state=state_of(gstin) or str(business.get("state_code") or ""), month=month)
    cats = {c["id"]: c async for c in db.categories.find({"business_id": bid},
                                                         {"_id": 0, "id": 1, "name": 1, "hsn": 1, "tax_class": 1})}
    products: dict[str, dict] = {}
    no_hsn = without_gst = 0
    late_cancel: list[str] = []
    proj = {"_id": 0, "invoice_number": 1, "date_key": 1, "status": 1, "cancelled_at": 1, "items": 1,
            "grand_total": 1, "customer_gstin": 1, "customer_name": 1, "place_of_supply": 1, "gst_removed": 1}
    cursor = db.invoices.find({"business_id": bid, "date_key": {"$gte": start, "$lte": end}}, proj).sort("date_key", 1)
    async for inv in cursor.batch_size(500):
        prefix, n = series(inv.get("invoice_number", ""))
        doc = out.docs.setdefault(prefix, {"first": None, "last": None, "total": 0, "cancel": 0})
        doc["total"] += 1
        if doc["first"] is None or n < series(doc["first"])[1]:
            doc["first"] = inv.get("invoice_number", "")
        if doc["last"] is None or n > series(doc["last"])[1]:
            doc["last"] = inv.get("invoice_number", "")
        if inv.get("status") == "Cancelled":
            doc["cancel"] += 1
            if (inv.get("cancelled_at") or "")[:7] > month:
                late_cancel.append(inv.get("invoice_number", ""))
            continue
        if inv.get("gst_removed"):
            without_gst += 1
        missing = [i["product_id"] for i in inv.get("items", []) if i.get("product_id") and i["product_id"] not in products]
        if missing:
            async for p in db.products.find({"business_id": bid, "id": {"$in": missing}},
                                            {"_id": 0, "id": 1, "hsn": 1, "tax_class": 1, "category_id": 1, "name": 1}):
                products[p["id"]] = p
        no_hsn += _add_bill(out, inv, products, cats)
    if no_hsn:
        out.warnings.append(warning("hsn_missing", "{{n}} bill lines have no HSN code. Set HSN codes on your "
                                    "categories before filing.", n=no_hsn))
    if without_gst:
        out.warnings.append(warning("bill_without_gst", "{{n}} bills were made without GST and are shown as exempt "
                                    "supplies. Ask your CA about them.", n=without_gst))
    if late_cancel:
        out.warnings.append(warning("cancelled_after_month", "{{n}} bills of this month were cancelled after the month "
                                    "ended. If the return is already filed, ask your CA about an amendment.",
                                    n=len(late_cancel)) | {"bills": late_cancel[:50]})
    return out


def warning(code: str, template: str, **params) -> dict:
    """A note for the shop about the return, in the AppError shape (English `message`, `key` + `params`)."""
    return {"code": code, "message": render(template, params), "key": template, "params": params}


def _add_bill(out: Month, inv: dict, products: dict, cats: dict) -> int:
    """Adds one live bill; returns how many of its lines had no HSN."""
    ctin = (inv.get("customer_gstin") or "").strip().upper()
    if ctin and not GSTIN.match(ctin):
        ctin = ""
    pos = str(inv.get("place_of_supply") or "") or out.state
    inter = bool(out.state) and pos != out.state
    bill = Bill(number=inv.get("invoice_number", ""), day=inv.get("date_key", ""), value=d(inv.get("grand_total")),
                pos=pos, inter=inter, ctin=ctin, name=inv.get("customer_name", ""))
    kind = "b2b" if ctin else "b2c"
    no_hsn = 0
    for line in inv.get("items", []):
        p = products.get(line.get("product_id") or "") or {}
        cat = cats.get(p.get("category_id") or "") or {}
        rate, txval, tax = _line_tax(line)
        unit = unit_of(line.get("unit"))
        hsn = str(line.get("hsn") or p.get("hsn") or cat.get("hsn") or "").strip()
        no_hsn += not hsn
        key = (hsn, UQC.get(unit, "OTH"), rate)
        if hsn and key not in out.hsn_desc:
            out.hsn_desc[key] = cat.get("name") or p.get("name") or line.get("name", "")
        part = Tax(txval=txval, qty=d(line.get("quantity")), val=txval + tax, **(split_tax(tax, inter) if rate else {}))
        out.hsn[kind][key].add(part)
        if not rate:
            klass = line.get("tax_class") or p.get("tax_class") or cat.get("tax_class") or DEFAULT_ZERO_CLASS
            sply = ("INTR" if inter else "INTRA") + ("B2B" if ctin else "B2C")  # GSTN: INTR = inter-state
            out.nil[(sply, klass if klass in TAX_CLASSES else DEFAULT_ZERO_CLASS)].add(Tax(txval=txval))
            continue
        t = bill.rates.setdefault(rate, Tax())
        t.add(Tax(txval=txval, **split_tax(tax, inter)))
    out.bills.append(bill)
    return no_hsn


def _items(bill: Bill) -> list[dict]:
    items = []
    for i, (rate, t) in enumerate(sorted(bill.rates.items()), start=1):
        det = {"txval": num(t.txval), "rt": rate_num(rate)}
        det |= {"iamt": num(t.iamt)} if bill.inter else {"camt": num(t.camt), "samt": num(t.samt)}
        items.append({"num": i, "itm_det": det | {"csamt": num(t.csamt)}})
    return items


def _idt(day: str) -> str:
    return f"{day[8:10]}-{day[5:7]}-{day[:4]}"


def _is_b2cl(bill: Bill) -> bool:
    return not bill.ctin and bill.inter and bill.value > B2CL_LIMIT


def gstr1_json(m: Month) -> dict:
    b2b: dict[str, list] = defaultdict(list)
    b2cl: dict[str, list] = defaultdict(list)
    b2cs: dict[tuple, Tax] = defaultdict(Tax)
    for bill in m.bills:
        if bill.ctin:
            if bill.rates:
                b2b[bill.ctin].append({"inum": bill.number, "idt": _idt(bill.day), "val": num(bill.value),
                                       "pos": bill.pos, "rchrg": "N", "inv_typ": "R", "itms": _items(bill)})
        elif _is_b2cl(bill):
            if bill.rates:
                b2cl[bill.pos].append({"inum": bill.number, "idt": _idt(bill.day), "val": num(bill.value),
                                       "itms": _items(bill)})
        else:
            for rate, t in bill.rates.items():
                b2cs[("INTER" if bill.inter else "INTRA", bill.pos, rate)].add(t)
    out = {"gstin": m.gstin, "fp": fp(m.month), "version": JSON_VERSION, "hash": "hash"}
    if b2b:
        out["b2b"] = [{"ctin": c, "inv": invs} for c, invs in sorted(b2b.items())]
    if b2cl:
        out["b2cl"] = [{"pos": p, "inv": invs} for p, invs in sorted(b2cl.items())]
    if b2cs:
        rows = []
        for (sply, pos, rate), t in sorted(b2cs.items()):
            row = {"sply_ty": sply, "pos": pos, "typ": "OE", "rt": rate_num(rate), "txval": num(t.txval)}
            row |= {"iamt": num(t.iamt)} if sply == "INTER" else {"camt": num(t.camt), "samt": num(t.samt)}
            rows.append(row | {"csamt": num(t.csamt)})
        out["b2cs"] = rows
    out["cdnr"], out["cdnur"] = [], []  # credit notes: Wave 2
    if m.nil:
        by_sply: dict[str, dict] = {}
        for (sply, klass), t in sorted(m.nil.items()):
            row = by_sply.setdefault(sply, {"sply_ty": sply, "expt_amt": 0.0, "nil_amt": 0.0, "ngsup_amt": 0.0})
            row[{"exempt": "expt_amt", "nil": "nil_amt", "non_gst": "ngsup_amt"}[klass]] = num(t.txval)
        out["nil"] = {"inv": list(by_sply.values())}
    out["hsn"] = {f"hsn_{kind}": _hsn_rows(m, kind) for kind in ("b2b", "b2c")}
    out["doc_issue"] = {"doc_det": [{"doc_num": 1, "docs": [
        {"num": i, "from": doc["first"], "to": doc["last"], "totnum": doc["total"], "cancel": doc["cancel"],
         "net_issue": doc["total"] - doc["cancel"]}
        for i, (_, doc) in enumerate(sorted(m.docs.items()), start=1)]}]} if m.docs else {"doc_det": []}
    return out


def _hsn_rows(m: Month, kind: str) -> list[dict]:
    rows = []
    for i, ((hsn, uqc, rate), t) in enumerate(sorted(m.hsn[kind].items(), key=lambda kv: kv[0]), 1):
        qty = ZERO if hsn.startswith("99") else t.qty  # services: quantity 0
        rows.append({"hsn_sc": hsn, "num": i, "desc": (m.hsn_desc.get((hsn, uqc, rate)) or "")[:30], "uqc": uqc,
                     "qty": float(qty.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)), "rt": rate_num(rate),
                     **t.amounts()})
    return rows


def gstr3b(m: Month) -> dict:
    """GSTR-3B outward supplies: 3.1(a) taxable, 3.1(c) nil/exempt, 3.1(e) non-GST, and 3.2 inter-state supplies to
    unregistered persons per place of supply. Input tax credit (table 4) comes from GSTR-2B, not from this app."""
    taxable, inter_unreg = Tax(), defaultdict(Tax)
    for bill in m.bills:
        for t in bill.rates.values():
            taxable.add(t)
            if bill.inter and not bill.ctin:
                inter_unreg[bill.pos].add(t)
    nil_exempt = sum((t.txval for (_, k), t in m.nil.items() if k != "non_gst"), ZERO)
    non_gst = sum((t.txval for (_, k), t in m.nil.items() if k == "non_gst"), ZERO)
    return {
        "gstin": m.gstin, "ret_period": fp(m.month),
        "sup_details": {
            "osup_det": taxable.amounts(),
            "osup_zero": {"txval": 0.0, "iamt": 0.0, "csamt": 0.0},
            "osup_nil_exmp": {"txval": num(nil_exempt)},
            "isup_rev": {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0},
            "osup_nongst": {"txval": num(non_gst)},
        },
        "inter_sup": {"unreg_details": [{"pos": p, "txval": num(t.txval), "iamt": num(t.iamt)}
                                        for p, t in sorted(inter_unreg.items())],
                      "comp_details": [], "uin_details": []},
        "itc_elg": None,  # from GSTR-2B on the portal
        "warnings": m.warnings,
    }


# ---- CSV: the GSTN offline tool's sheets ----

def _pos_name(code: str) -> str:
    return f"{code}-{STATES.get(code, '')}" if code else ""


def _csv_date(day: str) -> str:
    return date.fromisoformat(day).strftime("%d-%b-%Y") if day else ""


def _uqc_name(code: str) -> str:
    return f"{code}-{UQC_NAMES.get(code, 'OTHERS')}"


SHEETS = ("b2b", "b2cl", "b2cs", "cdnr", "cdnur", "exemp", "hsn_b2b", "hsn_b2c", "docs")


def gstr1_csv(m: Month) -> dict[str, str]:
    """One CSV per offline-tool sheet, header row as in the tool's template."""
    rows: dict[str, list[list]] = {s: [] for s in SHEETS}
    heads = {
        "b2b": ["GSTIN/UIN of Recipient", "Receiver Name", "Invoice Number", "Invoice date", "Invoice Value",
                "Place Of Supply", "Reverse Charge", "Applicable % of Tax Rate", "Invoice Type", "E-Commerce GSTIN",
                "Rate", "Taxable Value", "Cess Amount"],
        "b2cl": ["Invoice Number", "Invoice date", "Invoice Value", "Place Of Supply", "Applicable % of Tax Rate",
                 "Rate", "Taxable Value", "Cess Amount", "E-Commerce GSTIN"],
        "b2cs": ["Type", "Place Of Supply", "Applicable % of Tax Rate", "Rate", "Taxable Value", "Cess Amount",
                 "E-Commerce GSTIN"],
        "cdnr": ["GSTIN/UIN of Recipient", "Receiver Name", "Note Number", "Note Date", "Note Type", "Place Of Supply",
                 "Reverse Charge", "Note Supply Type", "Note Value", "Applicable % of Tax Rate", "Rate", "Taxable Value",
                 "Cess Amount"],
        "cdnur": ["UR Type", "Note Number", "Note Date", "Note Type", "Place Of Supply", "Note Value",
                  "Applicable % of Tax Rate", "Rate", "Taxable Value", "Cess Amount"],
        "exemp": ["Description", "Nil Rated Supplies", "Exempted(other than nil rated/non GST supply)", "Non-GST Supplies"],
        "hsn_b2b": ["HSN", "Description", "UQC", "Total Quantity", "Total Value", "Rate", "Taxable Value",
                    "Integrated Tax Amount", "Central Tax Amount", "State/UT Tax Amount", "Cess Amount"],
        "docs": ["Nature of Document", "Sr. No. From", "Sr. No. To", "Total Number", "Cancelled"],
    }
    heads["hsn_b2c"] = heads["hsn_b2b"]
    b2cs: dict[tuple, Tax] = defaultdict(Tax)
    for bill in m.bills:
        for rate, t in sorted(bill.rates.items()):
            if bill.ctin:
                rows["b2b"].append([bill.ctin, bill.name, bill.number, _csv_date(bill.day), num(bill.value),
                                    _pos_name(bill.pos), "N", "", "Regular B2B", "", rate_num(rate), num(t.txval), 0])
            elif _is_b2cl(bill):
                rows["b2cl"].append([bill.number, _csv_date(bill.day), num(bill.value), _pos_name(bill.pos), "",
                                     rate_num(rate), num(t.txval), 0, ""])
            else:
                b2cs[(bill.pos, rate)].add(t)
    for (pos, rate), t in sorted(b2cs.items()):
        rows["b2cs"].append(["OE", _pos_name(pos), "", rate_num(rate), num(t.txval), 0, ""])
    labels = {"INTRB2B": "Inter-State supplies to registered persons", "INTRAB2B": "Intra-State supplies to registered persons",
              "INTRB2C": "Inter-State supplies to unregistered persons", "INTRAB2C": "Intra-State supplies to unregistered persons"}
    for sply, label in labels.items():
        amounts = {k: num(m.nil[(sply, k)].txval) if (sply, k) in m.nil else 0.0 for k in TAX_CLASSES}
        rows["exemp"].append([label, amounts["nil"], amounts["exempt"], amounts["non_gst"]])
    for kind in ("b2b", "b2c"):
        for r in _hsn_rows(m, kind):
            val = r["txval"] + r["iamt"] + r["camt"] + r["samt"] + r["csamt"]
            rows[f"hsn_{kind}"].append([r["hsn_sc"], r["desc"], _uqc_name(r["uqc"]), r["qty"], round(val, 2), r["rt"],
                                        r["txval"], r["iamt"], r["camt"], r["samt"], r["csamt"]])
    for _, doc in sorted(m.docs.items()):
        rows["docs"].append(["Invoices for outward supply", doc["first"], doc["last"], doc["total"], doc["cancel"]])
    out = {}
    for sheet in SHEETS:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(heads[sheet])
        w.writerows(rows[sheet])
        out[sheet] = buf.getvalue()
    return out
