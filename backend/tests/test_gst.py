"""GST: GSTIN check, HSN codes, the CGST/SGST/IGST split (golden bills and rounding), and the tax invoice."""
import re
import importlib
import random
from decimal import Decimal

import pytest

import db as db_module
import gst
import invoice_html
from conftest import Shop, photo_b64, run

HR = "06AAACH0000A1Z"  # a Haryana GSTIN without its check character
HARYANA = HR + gst.check_char(HR)
MH = "27AAPFU0939F1ZV"  # Maharashtra, a published example
D = Decimal


# ---- GSTIN ----

@pytest.mark.parametrize("gstin", ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4", "24AAACC1206D1ZM", HARYANA, " 27aapfu0939f1zv "])
def test_valid_gstins(gstin):
    assert gst.gstin_problem(gstin) is None and gst.valid_gstin(gstin)


@pytest.mark.parametrize("gstin, problem", [
    ("", "missing"), (None, "missing"),
    ("27AAPFU0939F1ZW", "checksum"), ("06ABCDE1234F1Z5", "checksum"),  # one character off
    ("27AAPFU0939F1Z", "format"), ("27AAPFU0939F0ZV", "format"), ("27AAPF10939F1ZV", "format"),
    ("40AAPFU0939F1ZV", "state"), ("00AAPFU0939F1ZV", "state"),
])
def test_invalid_gstins(gstin, problem):
    assert gst.gstin_problem(gstin) == problem


def test_state_from_gstin():
    assert gst.state_of(MH) == "27" and gst.state_label("27") == "Maharashtra (27)"
    assert gst.state_of(HARYANA) == "06" and gst.state_of("") == "" and gst.state_label("") == ""


# ---- HSN ----

def test_hsn_suggestions():
    assert gst.suggest_hsn("Indoor Plants") == ("0602", True)
    assert gst.suggest_hsn("Seeds") == ("1209", True)
    assert gst.suggest_hsn("Pots") == ("6914", False)
    assert gst.suggest_hsn("Fertilizers") == ("3101", False)
    assert gst.suggest_hsn("Garden Tools") == ("8201", False)
    assert gst.suggest_hsn("Gift cards") == ("", False)
    assert gst.check_hsn("0602") and gst.check_hsn("060290") and gst.check_hsn("06029010") and gst.check_hsn("998719")
    assert not gst.check_hsn("602") and not gst.check_hsn("06029") and not gst.check_hsn("06A2")
    # the product's own code wins over the category's; a category without one shows its suggestion
    cat = {"name": "Indoor Plants"}
    assert gst.line_hsn({"hsn": "06029090"}, cat) == "06029090" and gst.line_hsn({}, cat) == "0602"
    assert gst.line_hsn({}, {"name": "Indoor Plants", "hsn": ""}) == ""  # the owner cleared it


# ---- The split: golden bills ----

def _line(price, qty, rate, hsn="", exempt=False):
    return {"price": price, "quantity": qty, "gst_percent": rate, "hsn": hsn, "exempt": exempt}


def test_intra_state_golden():
    s = gst.split([_line(118, 1, 18, "6914"), _line(105, 2, 5, "3101")])
    pot, manure = s.lines
    assert (pot.value, pot.taxable, pot.cgst, pot.sgst, pot.igst) == (D("118.00"), D("100.00"), D("9.00"), D("9.00"), 0)
    assert (manure.value, manure.taxable, manure.cgst, manure.sgst) == (D("210.00"), D("200.00"), D("5.00"), D("5.00"))
    assert [(r["rate"], r["taxable"], r["tax"]) for r in s.by_rate] == [(5, D("200.00"), D("10.00")), (18, D("100.00"), D("18.00"))]
    assert s.value == D("328.00") and s.tax == D("28.00") and not s.inter_state
    assert gst.title("regular", s) == gst.TAX_INVOICE


def test_inter_state_golden():
    s = gst.split([_line(118, 1, 18, "6914"), _line(105, 2, 5, "3101")], inter_state=True)
    assert [(l.igst, l.cgst, l.sgst) for l in s.lines] == [(D("18.00"), 0, 0), (D("10.00"), 0, 0)]
    assert s.total("igst") == D("28.00") and s.taxable == D("300.00")


def test_exempt_and_taxable_mixed_golden():
    s = gst.split([_line(250, 2, 0, "0602", exempt=True), _line(118, 1, 18, "6914")], discount=18)
    plant, pot = s.lines
    # the ₹18 discount is spread 500 : 118
    assert (plant.value, plant.taxable, plant.tax, plant.exempt) == (D("485.44"), D("485.44"), 0, True)
    assert (pot.value, pot.taxable, pot.cgst, pot.sgst) == (D("114.56"), D("97.08"), D("8.74"), D("8.74"))
    assert s.value == D("600.00")
    assert [(r["hsn"], r["rate"], r["taxable"], r["tax"]) for r in s.by_hsn] == [
        ("0602", 0, D("485.44"), 0), ("6914", 18, D("97.08"), D("17.48"))]
    assert gst.title("regular", s) == gst.BOTH
    assert gst.title("regular", gst.split([_line(250, 1, 0, "0602", exempt=True)])) == gst.BILL_OF_SUPPLY


def test_composition_golden():
    s = gst.split([_line(118, 1, 18, "6914"), _line(250, 1, 0, "0602", exempt=True)], composition=True)
    assert s.tax == 0 and s.taxable == D("368.00") and [l.rate for l in s.lines] == [0, 0]
    assert gst.title("composition", s) == gst.BILL_OF_SUPPLY


def test_titles_unregistered_and_bill_without_gst():
    s = gst.split([_line(118, 1, 18)])
    assert gst.title("unregistered", s) == gst.PLAIN
    # "Bill without GST" (D6 open): still a plain bill
    assert gst.title("regular", gst.split([_line(118, 1, 0)]), gst_removed=True) == gst.PLAIN


def test_bill_total_never_changes():
    """Decimal quantities and a discount: the lines still add up to the bill's own total."""
    lines = [_line(99.99, 0.333, 5), _line(10.01, 3, 18), _line(0.15, 0.333, 12), _line(2.5, 0.125, 5)]
    total = round(sum(l["price"] * l["quantity"] for l in lines) - 1.11 + 1e-9, 2)  # as sales.py saves it
    assert gst.split(lines, discount=1.11, total=total).value == D(str(total))


def test_rounding_reconciles_to_the_paisa():
    """Random bills: every line is taxable + tax, CGST = SGST, and line, rate and HSN totals all agree."""
    rng = random.Random(46)
    for _ in range(400):
        lines = [_line(round(rng.uniform(1, 5000), 2), rng.choice([1, 2, 3, 0.5, 1.25, 0.333, 12]),
                       rng.choice([0, 0.25, 3, 5, 12, 18, 28, 40]), rng.choice(["0602", "6914", "3101", ""]))
                 for _ in range(rng.randint(1, 8))]
        subtotal = sum(l["price"] * l["quantity"] for l in lines)
        discount = round(rng.uniform(0, subtotal / 3), 2) if rng.random() < 0.5 else 0
        total = round(subtotal - discount + 1e-9, 2)  # what sales.py saves as the grand total
        for inter in (False, True):
            s = gst.split(lines, discount, total=total, inter_state=inter)
            assert s.value == D(str(total))
            for l in s.lines:
                assert l.taxable + l.tax == l.value and l.cgst == l.sgst
                assert l.value == l.value.quantize(gst.PAISA) and l.tax == l.tax.quantize(gst.PAISA)
            for groups in (s.by_rate, s.by_hsn):
                for amount in gst.AMOUNTS:
                    assert sum((g[amount] for g in groups), D(0)) == s.total(amount)
            assert s.taxable + s.tax == s.value


def test_split_as_json():
    d = gst.split([_line(118, 1, 18, "6914")]).as_dict()
    assert d["lines"][0]["cgst"] == 9.0 and d["by_hsn"][0]["hsn"] == "6914" and d["tax"] == 18.0


# ---- The shop's tax profile (API) ----

def _owner(client, phone="9811122334"):
    r = client.post("/api/auth/register", json={"name": "Suresh", "phone": phone, "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _setup(client, h, gstin=HARYANA, **extra):
    r = client.put("/api/business", headers=h, json={"name": "Green House", "address": "Sector 5, Gurugram",
                                                     "phone": "9811122334", "gst_registered": bool(gstin),
                                                     "gstin": gstin, "upi_id": "shop@upi", "onboarding_done": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _audit(action):
    return run(db_module.state.db.audit_log.find({"action": action}, {"_id": 0}).to_list(50))


def test_gstin_needed_to_turn_gst_on(client):
    h = _owner(client)
    r = client.put("/api/business", headers=h, json={"name": "Green House", "gst_registered": True, "gstin": ""})
    assert r.status_code == 400 and r.json()["code"] == "gstin_missing"
    r = client.put("/api/business", headers=h, json={"gst_registered": True, "gstin": "06abcde1234f1z5"})
    assert r.status_code == 400 and r.json()["code"] == "bad_gstin"
    assert r.json()["params"] == {"gstin": "06ABCDE1234F1Z5"} and r.json()["detail"].startswith("06ABCDE1234F1Z5 is not")
    biz = _setup(client, h, gstin=HARYANA.lower())
    assert biz["gstin"] == HARYANA and biz["gstin_status"] == "ok" and biz["gst_warning"] == ""
    assert (biz["gst_scheme"], biz["gst_state_code"], biz["gst_state"]) == ("regular", "06", "Haryana")
    assert _audit("business.tax_profile")[0]["details"]["to"] == {"gst_registered": True, "gstin": HARYANA}
    # an unregistered shop needs no GSTIN
    other = _owner(client, "9811100001")
    assert _setup(client, other, gstin="")["gstin_status"] == "not_registered"


def test_shop_with_an_old_invalid_gstin_is_not_locked_out(client):
    h = _owner(client)
    _setup(client, h)
    db = db_module.state.db
    me = client.get("/api/auth/me", headers=h).json()
    run(db.businesses.update_one({"id": me["business"]["id"]}, {"$set": {"gstin": "06ABCDE1234F1Z5"}}))  # saved long ago
    biz = client.get("/api/business", headers=h).json()
    assert biz["gstin_status"] == "invalid" and "not valid" in biz["gst_warning"]
    # the app's Shop details form sends everything again, the old GSTIN included: still saved
    r = client.put("/api/business", headers=h, json={"name": "Green House 2", "gst_registered": True,
                                                     "gstin": "06ABCDE1234F1Z5", "footer_note": "Thanks"})
    assert r.status_code == 200 and r.json()["name"] == "Green House 2"
    # but a new GSTIN must be valid
    assert client.put("/api/business", headers=h, json={"gstin": "06ABCDE1234F1Z6"}).status_code == 400
    assert client.put("/api/business", headers=h, json={"gstin": MH}).json()["gstin_status"] == "ok"
    # registered with no GSTIN at all (from before the check): warned, other edits save
    run(db.businesses.update_one({"id": me["business"]["id"]}, {"$set": {"gstin": ""}}))
    r = client.put("/api/business", headers=h, json={"upi_id": "new@upi", "gst_registered": True, "gstin": ""})
    assert r.status_code == 200 and r.json()["gstin_status"] == "missing" and "no GST number" in r.json()["gst_warning"]


def test_composition_scheme(client):
    h = _owner(client)
    biz = _setup(client, h, gst_scheme="composition")
    assert biz["gst_scheme"] == "composition"
    assert client.put("/api/business", headers=h, json={"gst_scheme": "flat"}).status_code == 422
    assert client.put("/api/business", headers=h, json={"gst_scheme": "regular"}).json()["gst_scheme"] == "regular"


def test_logo_must_be_the_shops_own_upload(client):
    a, b = _owner(client), _owner(client, "9811100001")
    _setup(client, a)
    _setup(client, b)
    theirs = client.post("/api/upload-base64", headers=b, json={"image_base64": photo_b64()}).json()["path"]
    mine = client.post("/api/upload-base64", headers=a, json={"image_base64": photo_b64()}).json()["path"]
    r = client.put("/api/business", headers=a, json={"logo_path": theirs})
    assert r.status_code == 400 and r.json()["detail"] == "Please upload the logo again."
    assert client.put("/api/business", headers=a, json={"logo_path": "nurserypos/shops/x/made-up.jpg"}).status_code == 400
    assert client.put("/api/business", headers=a, json={"logo_path": mine}).json()["logo_path"] == mine
    assert client.put("/api/business", headers=a, json={"logo_path": mine, "name": "Again"}).status_code == 200
    assert client.put("/api/business", headers=a, json={"logo_path": ""}).json()["logo_path"] == ""


# ---- Categories and products ----

def test_categories_have_hsn_and_exempt(client):
    h = _owner(client)
    _setup(client, h)
    sugg = {s["name"]: s for s in client.get("/api/categories/suggestions", headers=h).json()}
    assert (sugg["Indoor Plants"]["hsn"], sugg["Indoor Plants"]["tax_exempt"]) == ("0602", True)
    assert (sugg["Pots"]["hsn"], sugg["Pots"]["tax_exempt"], sugg["Pots"]["gst_percent"]) == ("6914", False, 18)
    # an older app sends only names and rates: the codes are filled in
    cats = client.post("/api/categories/bulk", headers=h, json={"items": [
        {"name": "Indoor Plants", "gst_percent": 0}, {"name": "Pots", "gst_percent": 18},
        {"name": "Gift wrap", "gst_percent": 12, "hsn": "4823 90"}]}).json()
    cat = {c["name"]: c for c in cats}
    assert (cat["Indoor Plants"]["hsn"], cat["Indoor Plants"]["tax_exempt"]) == ("0602", True)
    assert (cat["Pots"]["hsn"], cat["Pots"]["tax_exempt"]) == ("6914", False)
    assert cat["Gift wrap"]["hsn"] == "482390"
    pots = cat["Pots"]["id"]
    r = client.patch(f"/api/categories/{pots}", headers=h, json={"hsn": "39"})
    assert r.status_code == 400 and r.json()["code"] == "bad_hsn"
    r = client.patch(f"/api/categories/{pots}", headers=h, json={"tax_exempt": True})
    assert r.status_code == 400 and "0% GST" in r.json()["detail"]
    assert client.patch(f"/api/categories/{pots}", headers=h, json={"hsn": "3924"}).json()["hsn"] == "3924"
    # a GST rate on an exempt category ends the exemption (older apps only know the rate)
    plants = cat["Indoor Plants"]["id"]
    got = client.patch(f"/api/categories/{plants}", headers=h, json={"gst_percent": 5}).json()
    assert (got["gst_percent"], got["tax_exempt"]) == (5, False)
    got = client.patch(f"/api/categories/{plants}", headers=h, json={"gst_percent": 0, "tax_exempt": True}).json()
    assert (got["gst_percent"], got["tax_exempt"]) == (0, True)
    changes = _audit("category.tax")
    assert {"hsn": "3924"} in [c["details"]["to"] for c in changes] and len(changes) == 3


def test_category_list_counts_products_in_one_query(client):
    h = _owner(client)
    _setup(client, h)
    cats = {c["name"]: c["id"] for c in client.post("/api/categories/bulk", headers=h, json={"items": [
        {"name": "Indoor Plants", "gst_percent": 0}, {"name": "Pots", "gst_percent": 18},
        {"name": "Seeds", "gst_percent": 0}]}).json()}
    ids = [client.post("/api/products", headers=h, json={"name": f"Plant {i}", "selling_price": 100, "stock": 1,
                                                         "category_id": cats["Indoor Plants"]}).json()["id"] for i in range(3)]
    client.post("/api/products", headers=h, json={"name": "Pot", "selling_price": 118, "stock": 1, "category_id": cats["Pots"]})
    assert client.delete(f"/api/products/{ids[0]}", headers=h).status_code == 200
    other = _owner(client, "9811100001")  # another shop's products never count
    _setup(client, other)
    run(db_module.state.db.products.insert_one({"id": "x", "business_id": "other", "category_id": cats["Pots"],
                                                "name": "Theirs", "deleted_at": None}))
    listed = client.get("/api/categories", headers=h).json()
    assert [(c["name"], c["product_count"], c["hsn"]) for c in listed] == [
        ("Indoor Plants", 2, "0602"), ("Pots", 1, "6914"), ("Seeds", 0, "1209")]
    assert all("_id" not in c and "n" not in c for c in listed)


def test_product_hsn_override(client):
    h = _owner(client)
    _setup(client, h)
    cats = {c["name"]: c["id"] for c in client.post("/api/categories/bulk", headers=h, json={"items": [
        {"name": "Indoor Plants", "gst_percent": 0}]}).json()}
    p = client.post("/api/products", headers=h, json={"name": "Bonsai", "selling_price": 900, "stock": 2,
                                                      "category_id": cats["Indoor Plants"]}).json()
    got = client.get(f"/api/products/{p['id']}/tax", headers=h).json()
    assert (got["hsn"], got["category_hsn"], got["effective_hsn"], got["tax_exempt"]) == ("", "0602", "0602", True)
    got = client.put(f"/api/products/{p['id']}/tax", headers=h, json={"hsn": "06029090"}).json()
    assert (got["hsn"], got["effective_hsn"]) == ("06029090", "06029090")
    assert client.put(f"/api/products/{p['id']}/tax", headers=h, json={"hsn": "abc"}).status_code == 400
    assert client.put(f"/api/products/{p['id']}/tax", headers=h, json={"hsn": ""}).json()["effective_hsn"] == "0602"
    assert len(_audit("product.hsn")) == 2
    other = _owner(client, "9811100001")
    _setup(client, other)
    assert client.get(f"/api/products/{p['id']}/tax", headers=other).status_code == 404
    # employees may read it, only the owner sets it
    r = client.post("/api/staff", headers=h, json={"name": "Ramesh", "phone": "9822233445", "password": "482913",
                                                   "permissions": {"sell": True, "stock": True}})
    assert r.status_code == 200, r.text
    worker = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"phone": "9822233445",
                                                                                 "password": "482913"}).json()["token"]}
    assert client.get(f"/api/products/{p['id']}/tax", headers=worker).status_code == 200
    assert client.put(f"/api/products/{p['id']}/tax", headers=worker, json={"hsn": "0602"}).status_code == 403


def test_migration_fills_category_codes_once(client):
    db = db_module.state.db
    run(db.categories.insert_many([
        {"id": "c1", "business_id": "b", "name": "Outdoor Plants", "gst_percent": 0},
        {"id": "c2", "business_id": "b", "name": "Pots", "gst_percent": 18},
        {"id": "c3", "business_id": "b", "name": "Flowering plants", "gst_percent": 5},  # taxed: not exempt
        {"id": "c4", "business_id": "b", "name": "Misc", "gst_percent": 0},
        {"id": "c5", "business_id": "b", "name": "Seeds", "gst_percent": 0, "hsn": "1209", "tax_exempt": False},
    ]))
    migration = importlib.import_module("migrations.0901_category_hsn")
    run(migration.up(db))
    got = {c["id"]: (c["hsn"], c["tax_exempt"]) for c in run(db.categories.find({"business_id": "b"}).to_list(10))}
    assert got == {"c1": ("0602", True), "c2": ("6914", False), "c3": ("0602", False), "c4": ("", False),
                   "c5": ("1209", False)}  # the owner's own setting is kept
    run(db.categories.update_one({"id": "c4"}, {"$set": {"hsn": "9999"}}))
    run(migration.up(db))  # a second run changes nothing
    assert run(db.categories.find_one({"id": "c4"}))["hsn"] == "9999"


# ---- The bill (rendering) ----

BIZ = {"name": "Green House", "address": "Sector 5, Gurugram", "phone": "9811122334", "gst_registered": True,
       "gstin": HARYANA}


def _bill(items, **extra):
    subtotal = sum(i["price"] * i["quantity"] for i in items)
    discount = extra.pop("discount", 0)
    return {"id": "i1", "business_id": "b", "invoice_number": "INV-0001", "items": items, "subtotal": subtotal,
            "discount": discount, "grand_total": round(subtotal - discount, 2), "payment_method": "Cash",
            "status": "Paid", "customer_name": "Meena", "customer_phone": "+919811100066",
            "customer_address": "12 Mall Road", "created_at": "2026-11-02T10:00:00+00:00", **extra}


def _item(name, price, qty, rate, **extra):
    return {"product_id": None, "name": name, "quantity": qty, "unit": "pcs", "price": price, "gst_percent": rate, **extra}


def test_tax_invoice_page():
    inv = _bill([_item("Clay pot", 118, 1, 18, hsn="6914"), _item("Snake Plant", 250, 2, 0, hsn="0602", exempt=True)])
    page = invoice_html.render(inv, BIZ, public=False)
    assert "INVOICE-CUM-BILL OF SUPPLY" in page and f"GSTIN {HARYANA} · Haryana (06)" in page
    assert "HSN 6914 · Taxable ₹100 · GST 18% ₹18" in page and "HSN 0602 · Exempt" in page
    assert "Place of supply Haryana (06)" in page and "Reverse charge: No" in page
    assert "<th class=\"r\">CGST</th><th class=\"r\">SGST</th>" in page and "IGST" not in page
    assert '<td class="r">₹9</td><td class="r">₹9</td>' in page  # CGST and SGST of the pot
    assert "Includes GST 18%" in page and "Authorised signatory" in page and "For Green House" in page
    assert "+919811100066" in page and "12 Mall Road" in page  # the shop's own print: in full
    only_pots = invoice_html.render(_bill([_item("Clay pot", 118, 1, 18, hsn="6914")]), BIZ, public=False)
    assert "TAX INVOICE" in only_pots and "BILL OF SUPPLY" not in only_pots


def test_b2b_inter_state_page():
    inv = _bill([_item("Clay pot", 118, 1, 18, hsn="6914")], customer_gstin=MH)
    page = invoice_html.render(inv, BIZ)
    assert "Place of supply Maharashtra (27)" in page and '<th class="r">IGST</th>' in page and "CGST" not in page
    assert f"GSTIN {MH}" in page and "12 Mall Road" in page  # a B2B bill shows the buyer's address
    assert "+91 XXXXXX0066" in page and "9811100066" not in page


def test_public_page_hides_the_customers_details():
    small = _bill([_item("Clay pot", 118, 1, 18)])
    page = invoice_html.render(small, BIZ)
    assert "+91 XXXXXX0066" in page and "9811100066" not in page and "12 Mall Road" not in page
    big = _bill([_item("Banyan bonsai", 50_000, 1, 0)])
    assert "12 Mall Road" in invoice_html.render(big, BIZ)  # ₹50,000 or more: the address is needed
    assert invoice_html.mask_phone("+919811122334") == "+91 XXXXXX2334" and invoice_html.mask_phone("") == ""


def test_composition_and_plain_bills():
    inv = _bill([_item("Clay pot", 118, 1, 18, hsn="6914")])
    page = invoice_html.render(inv, {**BIZ, "gst_scheme": "composition"}, public=False)
    assert "BILL OF SUPPLY" in page and gst.COMPOSITION_NOTE in page and "HSN 6914" in page
    assert "Includes GST" not in page and "Taxable" not in page and "CGST" not in page
    unregistered = invoice_html.render(inv, {**BIZ, "gst_registered": False}, public=False)
    assert "INVOICE" not in unregistered and "BILL OF SUPPLY" not in unregistered and "HSN" not in unregistered
    # "Bill without GST" (D6 open): printed as before
    removed = invoice_html.render(_bill([_item("Clay pot", 118, 1, 0)], gst_removed=True), BIZ, public=False)
    assert "INVOICE" not in removed and "BILL OF SUPPLY" not in removed and "Authorised signatory" not in removed


def test_bill_page_rounding_matches_the_total():
    inv = _bill([_item("Pot", 99.99, 3, 18, hsn="6914"), _item("Manure", 45.5, 1.25, 5, hsn="3101")], discount=7.77)
    s = gst.split_bill(inv, BIZ)
    assert s.value == D(str(inv["grand_total"]))
    page = invoice_html.render(inv, BIZ, public=False)
    assert f'<td class="r">{invoice_html.inr(float(s.taxable))}</td>' in page


# ---- The bill (through the API) ----

def test_printed_and_public_bill_from_the_app(client):
    h = _owner(client)
    _setup(client, h)
    cats = {c["name"]: c["id"] for c in client.post("/api/categories/bulk", headers=h, json={"items": [
        {"name": "Indoor Plants", "gst_percent": 0}, {"name": "Pots", "gst_percent": 18}]}).json()}
    pot = client.post("/api/products", headers=h, json={"name": "Clay pot", "selling_price": 118, "stock": 5,
                                                        "category_id": cats["Pots"]}).json()
    plant = client.post("/api/products", headers=h, json={"name": "Snake Plant", "selling_price": 250, "stock": 5,
                                                          "category_id": cats["Indoor Plants"]}).json()
    customer = client.post("/api/customers", headers=h, json={"name": "Meena", "phone": "9811100066",
                                                              "address": "12 Mall Road"}).json()
    inv = client.post("/api/invoices", headers=h, json={"customer_id": customer["id"], "items": [
        {"product_id": pot["id"], "name": "Clay pot", "quantity": 1, "price": 118},
        {"product_id": plant["id"], "name": "Snake Plant", "quantity": 2, "price": 250}]}).json()
    assert inv["grand_total"] == 618 and inv["gst_total"] == 18  # the bill itself is unchanged
    page = client.get(f"/api/invoices/{inv['id']}/html", headers=h).text
    assert "INVOICE-CUM-BILL OF SUPPLY" in page and "Taxable ₹100 · GST 18% ₹18" in page
    assert "Reverse charge: No" in page and "12 Mall Road" in page and "+919811100066" in page
    # sales.py freezes each line's HSN on the bill, so the printed bill has the codes at once
    assert "HSN 6914 · Taxable ₹100" in page and "HSN 0602 · Exempt" in page
    assert run(gst.bill_codes(db_module.state.db, inv)) == {}  # nothing left to look up
    # a bill saved before that: today's codes (gst.bill_codes, passed in by sales.py)
    old = {**inv, "items": [{k: v for k, v in it.items() if k not in ("hsn", "exempt")} for it in inv["items"]]}
    codes = run(gst.bill_codes(db_module.state.db, old))
    assert codes == {pot["id"]: {"hsn": "6914", "exempt": False}, plant["id"]: {"hsn": "0602", "exempt": True}}
    biz = run(db_module.state.db.businesses.find_one({"id": inv["business_id"]}, {"_id": 0}))
    with_codes = invoice_html.render(old, biz, public=False, codes=codes)
    assert "HSN 6914 · Taxable ₹100" in with_codes and "HSN 0602 · Exempt" in with_codes
    token = client.get(f"/api/invoices/{inv['id']}/share", headers=h).json()["token"]
    public = client.get(f"/i/{token}").text
    assert "+91 XXXXXX0066" in public and "9811100066" not in public and "12 Mall Road" not in public
    # a link preview in WhatsApp: the shop and amount, the bill number and date (no phone or address)
    title = re.search(r'<meta property="og:title" content="([^"]*)">', public).group(1)
    describe = re.search(r'<meta property="og:description" content="([^"]*)">', public).group(1)
    assert " · ₹" in title and inv["invoice_number"] in describe and "0066" not in title + describe


def test_bills_made_in_the_app_carry_hsn_per_line(client):
    """sales.py freezes each line's HSN and exempt flag on the bill (docs/GST-INVOICE.md, Wave 2 hooks 1 and 2);
    bills saved before that print today's codes through gst.bill_codes."""
    shop = Shop(client)
    plant, pot = shop.product("Snake Plant", 250, 5), shop.product("Clay pot", 118, 5, category="Pots")
    r = shop.post("/api/invoices", {"items": [
        {"product_id": plant["id"], "name": "Snake Plant", "quantity": 2, "price": 250},
        {"product_id": pot["id"], "name": "Clay pot", "quantity": 1, "price": 118},
        {"product_id": None, "name": "Gift wrap", "quantity": 1, "price": 20}],
        "customer_phone": "9811100099", "customer_name": "Anita"})
    assert r.status_code == 200, r.text
    inv = r.json()
    lines = [(it.get("hsn"), it.get("exempt")) for it in inv["items"]]
    assert lines == [("0602", True), ("6914", False), (None, None)]
    page = shop.get(f"/api/invoices/{inv['id']}/html").text
    assert "0602" in page and "6914" in page and "Exempt" in page
    db = db_module.state.db
    run(db.invoices.update_one({"id": inv["id"]}, {"$unset": {"items.$[].hsn": "", "items.$[].exempt": ""}}))
    page = shop.get(f"/api/invoices/{inv['id']}/html").text  # an older bill: today's codes
    assert "0602" in page and "6914" in page and "Exempt" in page


def test_saved_split_reads_what_the_bill_saved():
    """Review fix: printing and GSTR-1 read the amounts saved with the bill instead of working them out again."""
    from routers.sales import price_lines

    lines = [{"product_id": "p1", "price": 63, "quantity": 1, "gst_percent": 12, "hsn": "6914"},
             {"product_id": "p2", "price": 250, "quantity": 1, "gst_percent": 0, "hsn": "0602", "exempt": True}]
    inv = {"items": lines, **price_lines(lines, 0)}
    assert (lines[0]["net"], lines[0]["tax"], lines[0]["cgst"], lines[0]["sgst"]) == (63, 6.76, 3.38, 3.38)
    s = gst.saved_split(inv, inter_state=False)
    assert (s.lines[0].taxable, s.lines[0].cgst, s.lines[0].sgst) == (D("56.24"), D("3.38"), D("3.38"))
    assert s.lines[1].exempt and s.lines[1].tax == 0 and s.value == D("313")
    # The place of supply says another state: the same saved tax, as IGST.
    s = gst.saved_split(inv, inter_state=True)
    assert (s.lines[0].igst, s.lines[0].cgst, s.lines[0].taxable) == (D("6.76"), 0, D("56.24"))
    # A bill saved before (one tax rounded as a whole): that tax, CGST gets the odd paisa, as the app showed it.
    old = {"items": [{"price": 63, "quantity": 1, "gst_percent": 12, "net": 63.0, "tax": 6.75}], "grand_total": 63}
    s = gst.saved_split(old, inter_state=False)
    assert (s.lines[0].taxable, s.lines[0].cgst, s.lines[0].sgst) == (D("56.25"), D("3.38"), D("3.37"))
    assert gst.saved_split(old, inter_state=False, composition=True).tax == 0  # a composition shop collected none
    # Very old bills without net: split() as before.
    older = {"items": [{"price": 63, "quantity": 1, "gst_percent": 12}], "grand_total": 63, "discount": 0}
    assert gst.saved_split(older, inter_state=False).tax == D("6.76")
