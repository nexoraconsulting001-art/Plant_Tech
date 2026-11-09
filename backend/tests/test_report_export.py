"""Sales report export (GET /api/reports/sales/export, owner 10 Nov 2026): Excel with Summary, Bills and Items,
or CSV of the bills; owner only; typed names never run as spreadsheet formulas."""
import csv
import io
import uuid
import zipfile

import db as db_module
from conftest import Shop, run


def _bill(shop, product, customer, qty=1):
    r = shop.c.post("/api/invoices", headers={**shop.h, "Idempotency-Key": str(uuid.uuid4())}, json={
        "items": [{"product_id": product["id"], "name": product["name"], "quantity": qty,
                   "price": product["selling_price"]}], "customer_id": customer["id"], "payment_method": "Cash"})
    assert r.status_code == 200, r.text
    return r.json()


def _setup(client):
    shop = Shop(client)
    rose = shop.product("Rose", 120, 50)
    kavita = shop.post("/api/customers", {"name": "Kavita", "phone": "+919811100011"}).json()
    sneaky = shop.post("/api/customers", {"name": "=HYPERLINK(\"http://x\",\"y\")", "phone": "+919811100012"}).json()
    _bill(shop, rose, kavita, 2)
    gone = _bill(shop, rose, sneaky)
    assert shop.post(f"/api/invoices/{gone['id']}/cancel", {"reason": "mistake"}).status_code == 200
    return shop


def test_csv_lists_the_bills_with_safe_names(client):
    shop = _setup(client)
    r = shop.get("/api/reports/sales/export", params={"range": "day", "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="Sales_' in r.headers["content-disposition"] and r.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["Bill", "Date", "Time", "Customer"] and len(rows) == 3
    assert rows[1][3] == "Kavita" and rows[1][9] == "240.0" and rows[1][10] == "Paid" and rows[1][11] == "Suresh"
    assert rows[2][3].startswith("'=HYPERLINK") and rows[2][10] == "Cancelled"


def test_excel_has_summary_bills_and_items_and_no_formulas(client):
    shop = _setup(client)
    r = shop.get("/api/reports/sales/export", params={"range": "month"})  # Excel by default
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    book = z.read("xl/workbook.xml").decode()
    assert all(f'name="{n}"' in book for n in ("Summary", "Bills", "Items"))
    sheets = [z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
    assert not any("<f>" in s for s in sheets)  # nothing written as a formula
    shared = z.read("xl/sharedStrings.xml").decode()
    assert "=HYPERLINK" in shared and "Kavita" in shared and "Rose" in shared and "Paid by Cash" in shared
    entry = run(db_module.state.db.audit_log.find_one({"action": "export.sales"}))
    assert entry and entry["details"]["format"] == "xlsx" and entry["details"]["bills"] == 2


def test_only_the_owner_exports(client):
    shop = Shop(client)
    _, worker = shop.worker(reports=True)
    assert client.get("/api/reports/sales/export", headers=worker).status_code == 403
    assert client.get("/api/reports/sales", headers=worker).status_code == 200  # the report itself stays theirs
    assert shop.get("/api/reports/sales/export", params={"format": "pdf"}).status_code == 422
    empty = shop.get("/api/reports/sales/export", params={"range": "week", "format": "csv"})
    assert empty.status_code == 200 and len(list(csv.reader(io.StringIO(empty.content.decode("utf-8-sig"))))) == 1
