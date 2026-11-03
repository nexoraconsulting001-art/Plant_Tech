"""GST rules for the tax invoice: GSTIN check, HSN codes, the CGST/SGST/IGST split and the bill's title.

Template and defaults are pending a CA's sign-off (docs/GST-INVOICE.md); nothing here is printed as "pending".

- **Tax profile of a shop:** not registered (`gst_registered` false), "regular" or "composition" (`gst_scheme`,
  regular when missing). The shop's state is the first two digits of its GSTIN.
- **GSTIN:** 15 characters, state code + PAN + entity number + "Z" + a mod-36 check character (`gstin_problem`).
  A valid one is needed to turn GST on or to change it; a shop saved before this check keeps working and gets a
  warning instead (`gstin_warning`, see routers/business.py).
- **HSN/SAC:** on the category (with a suggested default from its name, e.g. 0602 for live plants), and a product
  may override it (`hsn` on the product). A category can be marked exempt (then its GST is 0%).
- **split():** prices are tax-inclusive, so a line's value (after its share of the discount) is split into taxable
  value + tax with Decimal and ROUND_HALF_UP. The lines always add up to the bill's total, and the per-rate and
  per-HSN totals are sums of the lines, so everything reconciles to the paisa. A new bill is split once, when it
  is made (sales.price_lines saves each line's value, tax and CGST/SGST/IGST); the print, the app, the exports
  and GSTR-1 then read those saved amounts (saved_split), so they never differ by a paisa.
- **Composition shops** collect no tax: their bills are saved with every rate at 0 (sales.py) and print as a
  Bill of Supply. They file CMP-08/GSTR-4, not GSTR-1, so the GSTR exports refuse them (routers/exports.py).
- **"Bill without GST" (`gst_removed`, decision D6 still open):** left as it is. Such a bill has every rate at 0
  and prints like a bill of an unregistered shop (title "Bill", no tax). Remove or change it only once the owner
  decides D6 (docs/PRODUCTION-PLAN.md section 4, docs/GST-INVOICE.md).
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

PAISA = Decimal("0.01")
ZERO = Decimal(0)

# ---- GSTIN ----

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Normal taxpayer: 2-digit state, PAN (5 letters, 4 digits, 1 letter), entity number 1-9/A-Z, "Z", check character.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# GST state codes (the first two digits of a GSTIN). 25 and 28 are old codes some registrations still carry.
STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "28": "Andhra Pradesh (old)",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}


def clean_gstin(value: str | None) -> str:
    """'06 abcde1234f1z4 ' -> '06ABCDE1234F1Z4'."""
    return re.sub(r"\s+", "", value or "").upper()


def check_char(first14: str) -> str:
    """The GSTIN check character: mod-36 over the first 14 characters, every second one weighted 2."""
    total = 0
    for i, ch in enumerate(first14):
        product = CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36]


def gstin_problem(value: str | None) -> str | None:
    """None when the GSTIN is valid, else "missing", "format", "state" or "checksum"."""
    g = clean_gstin(value)
    if not g:
        return "missing"
    if not GSTIN_RE.match(g):
        return "format"
    if g[:2] not in STATES:
        return "state"
    if check_char(g[:14]) != g[14]:
        return "checksum"
    return None


def valid_gstin(value: str | None) -> bool:
    return gstin_problem(value) is None


def state_of(gstin: str | None) -> str:
    """State code from a GSTIN ('06' for Haryana), empty when it has none."""
    g = clean_gstin(gstin)
    return g[:2] if len(g) >= 2 and g[:2] in STATES else ""


def state_label(code: str) -> str:
    """'06' -> 'Haryana (06)'."""
    return f"{STATES[code]} ({code})" if code in STATES else ""


# ---- Shop tax profile ----

SCHEMES = ("regular", "composition")


def scheme(biz: dict) -> str:
    """"unregistered", "regular" or "composition"."""
    if not biz.get("gst_registered"):
        return "unregistered"
    return biz.get("gst_scheme") if biz.get("gst_scheme") in SCHEMES else "regular"


# ---- HSN / SAC ----

HSN_RE = re.compile(r"^[0-9]{4}([0-9]{2}){0,2}$")  # 4, 6 or 8 digits (SAC codes are 6 digits starting 99)
# Suggested codes by words in a category's name, first match wins. The owner confirms them with their CA.
HSN_SUGGESTIONS = (
    (("seed",), "1209", True),  # seeds for sowing: exempt
    (("pot", "planter", "gamla"), "6914", False),  # ceramic / clay pots (plastic ones are 3924)
    (("fertili", "manure", "compost", "khad"), "3101", False),
    (("soil", "mitti", "cocopeat", "potting mix", "media"), "2530", False),
    (("tool",), "8201", False),  # hand tools for horticulture
    (("plant", "tree", "shrub", "herb", "succulent", "cact", "flower", "bonsai", "sapling", "creeper", "palm",
      "grass", "fern", "bulb"), "0602", True),  # live plants: exempt
)


def check_hsn(value: str) -> bool:
    return bool(HSN_RE.match(value))


def suggest_hsn(category_name: str | None) -> tuple[str, bool]:
    """Suggested (HSN, exempt) for a category name; ("", False) when nothing fits."""
    name = (category_name or "").lower()
    for words, code, exempt in HSN_SUGGESTIONS:
        if any(w in name for w in words):
            return code, exempt
    return "", False


def category_hsn(cat: dict | None) -> str:
    """The category's HSN, or the suggestion for its name when the owner has not set one."""
    if not cat:
        return ""
    if cat.get("hsn") is not None:
        return cat["hsn"]
    return suggest_hsn(cat.get("name"))[0]


def line_hsn(product: dict | None, cat: dict | None) -> str:
    """A bill line's HSN: the product's own code, else its category's."""
    return ((product or {}).get("hsn") or "") or category_hsn(cat)


async def bill_codes(db, inv: dict) -> dict[str, dict]:
    """product id -> {hsn, exempt} for bill lines saved without them (bills made before HSN codes): today's code
    of each product and whether its category is exempt. Lines that carry `hsn` keep their own (what was billed)."""
    ids = list({it["product_id"] for it in inv.get("items", []) if it.get("product_id") and "hsn" not in it})
    if not ids:
        return {}
    bid = inv["business_id"]
    products = await db.products.find({"business_id": bid, "id": {"$in": ids}},
                                      {"_id": 0, "id": 1, "hsn": 1, "category_id": 1}).to_list(len(ids))
    cat_ids = list({p.get("category_id") for p in products if p.get("category_id")})
    cats = {c["id"]: c for c in await db.categories.find(
        {"business_id": bid, "id": {"$in": cat_ids}},
        {"_id": 0, "id": 1, "name": 1, "hsn": 1, "tax_exempt": 1}).to_list(len(cat_ids) or 1)}
    out = {}
    for p in products:
        cat = cats.get(p.get("category_id"))
        out[p["id"]] = {"hsn": line_hsn(p, cat), "exempt": bool((cat or {}).get("tax_exempt"))}
    return out


# ---- The split ----

def _d(x) -> Decimal:
    """float or str -> Decimal through its shortest text, so 0.1 stays 0.1."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _paise(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass
class TaxLine:
    index: int
    hsn: str
    rate: Decimal  # GST % charged (0 for exempt, composition or "Bill without GST")
    exempt: bool
    value: Decimal  # what the customer pays for the line, after its share of the discount (tax-inclusive)
    taxable: Decimal
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO

    @property
    def tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst

    @property
    def taxed(self) -> bool:
        return self.rate > 0


@dataclass
class Split:
    lines: list[TaxLine]
    inter_state: bool
    by_rate: list[dict] = field(default_factory=list)
    by_hsn: list[dict] = field(default_factory=list)

    def total(self, name: str) -> Decimal:
        return sum((getattr(l, name) for l in self.lines), ZERO)

    @property
    def value(self) -> Decimal:
        return self.total("value")

    @property
    def taxable(self) -> Decimal:
        return self.total("taxable")

    @property
    def tax(self) -> Decimal:
        return self.total("tax")

    def as_dict(self) -> dict:
        """Plain numbers (float rupees) for JSON, e.g. a GSTR-1 export."""
        f = float
        return {"inter_state": self.inter_state, "value": f(self.value), "taxable": f(self.taxable), "tax": f(self.tax),
                "lines": [{"index": l.index, "hsn": l.hsn, "rate": f(l.rate), "exempt": l.exempt, "value": f(l.value),
                           "taxable": f(l.taxable), "cgst": f(l.cgst), "sgst": f(l.sgst), "igst": f(l.igst),
                           "tax": f(l.tax)} for l in self.lines],
                "by_rate": [{k: (f(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in self.by_rate],
                "by_hsn": [{k: (f(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in self.by_hsn]}


AMOUNTS = ("value", "taxable", "cgst", "sgst", "igst", "tax")


def _totals(lines: list[TaxLine], *keys: str) -> list[dict]:
    """Sums of the lines grouped by `keys` (e.g. "rate", or "hsn" and "rate"), in key order."""
    groups: dict[tuple, dict] = {}
    for l in lines:
        k = tuple(getattr(l, name) for name in keys)
        g = groups.setdefault(k, {**dict(zip(keys, k)), **{a: ZERO for a in AMOUNTS}})
        for a in AMOUNTS:
            g[a] += getattr(l, a)
    return [groups[k] for k in sorted(groups)]


def split(lines: Iterable[dict], discount: float = 0, *, total: float | None = None, inter_state: bool = False,
          composition: bool = False, codes: dict[str, dict] | None = None) -> Split:
    """Split tax-inclusive bill lines into taxable value and CGST+SGST (same state) or IGST (another state).

    lines: {price, quantity, gst_percent, hsn?, exempt?, product_id?} as saved on a bill.
    discount: spread over the lines in proportion to their amounts.
    total: the bill's grand total; the lines are made to add up to it exactly (the last paisa of rounding goes on
      the biggest line), so the bill's total never changes. Default: amounts minus discount.
    composition: a composition shop collects no tax (bill of supply), so every line is taxable value only.
    codes: product id -> {hsn, exempt} for lines saved without them (see bill_codes).

    CGST and SGST are each half the rate on the same value, rounded alike, so they are always equal.
    """
    lines = list(lines)
    codes = codes or {}
    gross = [_paise(_d(l["price"]) * _d(l["quantity"])) for l in lines]
    subtotal = sum(gross, ZERO)
    discount_d = min(_paise(_d(discount or 0)), subtotal)
    target = _paise(_d(total)) if total is not None else subtotal - discount_d
    values = [g - (_paise(discount_d * g / subtotal) if subtotal else ZERO) for g in gross]
    if values and (rest := target - sum(values, ZERO)):
        biggest = max(range(len(values)), key=lambda i: values[i])
        values[biggest] += rest

    out = []
    for i, (l, value) in enumerate(zip(lines, values)):
        known = codes.get(l.get("product_id") or "", {})
        rate = _d(l.get("gst_percent") or 0)
        exempt = bool(l.get("exempt", known.get("exempt"))) and not rate  # a taxed line is never exempt
        rate = ZERO if composition else rate
        code = l.get("hsn") or known.get("hsn", "")
        t = TaxLine(index=i, hsn=code, rate=rate, exempt=exempt, value=value, taxable=value)
        if rate > 0:
            if inter_state:
                t.igst = _paise(value * rate / (100 + rate))
            else:
                t.cgst = t.sgst = _paise(value * (rate / 2) / (100 + rate))
            t.taxable = value - t.tax
        out.append(t)
    s = Split(lines=out, inter_state=inter_state)
    s.by_rate = _totals(out, "rate")
    s.by_hsn = _totals(out, "hsn", "rate")
    return s


# ---- The bill's title ----

TAX_INVOICE, BILL_OF_SUPPLY, BOTH, PLAIN = "Tax Invoice", "Bill of Supply", "Invoice-cum-Bill of Supply", "Bill"
COMPOSITION_NOTE = "Composition taxable person, not eligible to collect tax on supplies"


def title(shop_scheme: str, s: Split, gst_removed: bool = False) -> str:
    """Tax Invoice (all lines taxed), Bill of Supply (none taxed, or a composition shop), Invoice-cum-Bill of Supply
    (both kinds on one bill, CGST rule 46A), or plain "Bill" for an unregistered shop.
    A "Bill without GST" stays a plain "Bill" as before (decision D6 is open)."""
    if shop_scheme == "unregistered" or gst_removed:
        return PLAIN
    if shop_scheme == "composition":
        return BILL_OF_SUPPLY
    taxed = [l.taxed for l in s.lines]
    if taxed and all(taxed):
        return TAX_INVOICE
    return BOTH if any(taxed) else BILL_OF_SUPPLY


def place_of_supply(inv: dict, biz: dict) -> str:
    """State code where the supply is made: the bill's own (when the app sent one), else the buyer's GSTIN state
    (a B2B bill), else the shop's (a sale over the counter)."""
    return (inv.get("place_of_supply") or state_of(inv.get("customer_gstin")) or state_of(biz.get("gstin")) or "")


def is_inter_state(inv: dict, biz: dict) -> bool:
    shop_state = state_of(biz.get("gstin"))
    pos = place_of_supply(inv, biz)
    return bool(shop_state and pos and pos != shop_state)


def number(rate: Decimal) -> int | float:
    """A rate as JSON wants it: 12 rather than 12.0."""
    return int(rate) if rate == rate.to_integral_value() else float(rate)


def saved_split(inv: dict, *, inter_state: bool, composition: bool = False,
                codes: dict[str, dict] | None = None) -> Split:
    """The split of a saved bill from what was saved with it, so the printed bill, the app, the exports and GSTR-1
    all show the same amounts:
    - bills saved by sales.price_lines through split() (they have `inter_state`): each line's value (`net`) and
      its CGST, SGST and IGST as saved (a composition shop's bills are saved without tax);
    - older bills with a per-line `net` and `tax` (one tax rounded as a whole, as the app showed it): that tax,
      CGST + SGST half each (CGST gets the odd paisa) or IGST; nothing for a composition shop, which collects none.
      The same for a saved bill whose place of supply says otherwise than its `inter_state`: its tax stays;
    - very old bills without `net`: worked out again by split().
    """
    items = inv.get("items", [])
    if not items or any("net" not in l for l in items):
        return split(items, inv.get("discount", 0), total=inv.get("grand_total"), inter_state=inter_state,
                     composition=composition, codes=codes)
    if "inter_state" in inv:
        composition = False  # as billed
    saved = "inter_state" in inv and bool(inv["inter_state"]) == inter_state
    codes = codes or {}
    out = []
    for i, l in enumerate(items):
        known = codes.get(l.get("product_id") or "", {})
        rate = ZERO if composition else _d(l.get("gst_percent") or 0)
        exempt = bool(l.get("exempt", known.get("exempt"))) and not _d(l.get("gst_percent") or 0)
        value = _paise(_d(l["net"]))
        t = TaxLine(index=i, hsn=l.get("hsn") or known.get("hsn", ""), rate=rate, exempt=exempt, value=value,
                    taxable=value)
        if rate > 0:
            if saved and "cgst" in l:
                t.cgst, t.sgst, t.igst = (_paise(_d(l.get(k) or 0)) for k in ("cgst", "sgst", "igst"))
            elif inter_state:
                t.igst = _paise(_d(l.get("tax") or 0))
            else:
                tax = _paise(_d(l.get("tax") or 0))
                t.cgst = _paise(tax / 2)
                t.sgst = tax - t.cgst
            t.taxable = value - t.tax
        out.append(t)
    s = Split(lines=out, inter_state=inter_state)
    s.by_rate = _totals(out, "rate")
    s.by_hsn = _totals(out, "hsn", "rate")
    return s


def split_bill(inv: dict, biz: dict, codes: dict[str, dict] | None = None) -> Split:
    """The split of a saved bill, as it is printed (saved_split)."""
    return saved_split(inv, inter_state=is_inter_state(inv, biz), composition=scheme(biz) == "composition",
                       codes=codes)
