"""Compliance data (WP-10): GSTR-1 / 3B, the full shop export, the audit log, retention and the demo guards."""
import csv
import gzip
import importlib
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import MOCK, TEST_DB, Shop, run

import audit
import db as db_module
import demo_data
import jobs
import retention
import server
import storage_client
from common import date_key
from routers import exports

if not any(getattr(r, "path", "") == "/api/exports/gstr1" for r in server.app.routes):
    server.app.include_router(exports.router)  # until server.py includes it (see the WP-10 report)

pytestmark = pytest.mark.skipif(MOCK, reason="needs the real replica set")
BACKEND = Path(__file__).resolve().parent.parent
MONTH = date_key()[:7]


def db():
    return db_module.state.db if db_module.state.db is not None else db_module.get_db()


def bill(shop, *lines, phone="9811100001", name="Asha", **extra):
    r = shop.post("/api/invoices", {"items": [{"product_id": p["id"], "name": p["name"], "quantity": q, "price": price}
                                              for p, q, price in lines],
                                    "customer_phone": phone, "customer_name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def set_invoice(inv, **fields):
    run(db().invoices.update_one({"id": inv["id"]}, {"$set": fields}))


@pytest.fixture
def gst_shop(client):
    """A Haryana (06) GST shop: plants at 0%, pots at 18%, one bill of each kind GSTR-1 knows."""
    shop = Shop(client)
    plant = shop.product("Money Plant", 100, 50)
    pot = shop.product("Ceramic Pot", 118, 50, category="Pots")
    fountain = shop.product("Stone Fountain", 150000, 3, category="Pots")
    a = bill(shop, (pot, 1, 118), (plant, 1, 100))  # intra B2C: 100 + 9 + 9, and 100 exempt
    b = bill(shop, (pot, 3, 100), phone="9811100002", name="Bina")  # 300 incl. 45.76 GST
    c = bill(shop, (pot, 1, 118), phone="9811100003", name="Chetan")
    assert shop.post(f"/api/invoices/{c['id']}/cancel", {"reason": "wrong item"}).status_code == 200
    d = bill(shop, (pot, 2, 118), phone="9811100004", name="Delhi Traders")  # B2B to Delhi
    set_invoice(d, customer_gstin="07AAACD1234E1Z5", place_of_supply="07")
    e = bill(shop, (fountain, 1, 150000), phone="9811100005", name="Lucknow Farm")  # B2C over 1 lakh, to UP
    set_invoice(e, place_of_supply="09")
    shop.bills = {"a": a, "b": b, "c": c, "d": d, "e": e}
    return shop


# ---- a subset of the GSTN offline tool's JSON rules (fields, types, formats) ----
DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def numbers(d: dict, keys) -> bool:
    return all(isinstance(d[k], (int, float)) for k in keys if k in d)


def tax_of(rows) -> float:
    return round(sum(x.get("iamt", 0) + x.get("camt", 0) + x.get("samt", 0) for x in rows), 2)


def check_gstr1(doc):
    assert set(doc) <= {"gstin", "fp", "version", "hash", "b2b", "b2cl", "b2cs", "cdnr", "cdnur", "nil", "hsn", "doc_issue"}
    assert GSTIN.match(doc["gstin"]) and re.match(r"^(0[1-9]|1[0-2])\d{4}$", doc["fp"])
    for entry in doc.get("b2b", []):
        assert GSTIN.match(entry["ctin"])
        for inv in entry["inv"]:
            assert {"inum", "idt", "val", "pos", "rchrg", "inv_typ", "itms"} <= set(inv) and len(inv["inum"]) <= 16
            assert DATE.match(inv["idt"]) and re.match(r"^\d{2}$", inv["pos"]) and inv["rchrg"] in "YN"
            assert all({"rt", "txval"} <= set(i["itm_det"]) and numbers(i["itm_det"], i["itm_det"]) for i in inv["itms"])
    for entry in doc.get("b2cl", []):
        for inv in entry["inv"]:
            assert {"inum", "idt", "val", "itms"} <= set(inv) and DATE.match(inv["idt"])
            assert all("camt" not in i["itm_det"] for i in inv["itms"])  # always inter-state: IGST only
    for row in doc.get("b2cs", []):
        assert {"sply_ty", "pos", "rt", "txval"} <= set(row) and row["sply_ty"] in ("INTER", "INTRA")
        assert set(row) <= {"sply_ty", "pos", "typ", "rt", "txval", "iamt", "camt", "samt", "csamt", "etin"}
    for row in doc.get("nil", {}).get("inv", []):
        assert row["sply_ty"] in ("INTRB2B", "INTRAB2B", "INTRB2C", "INTRAB2C")
    assert isinstance(doc["hsn"]["hsn_b2b"], list)
    for row in doc["hsn"]["hsn_b2b"] + doc["hsn"].get("hsn_b2c", []):
        assert next(iter(row)) == "hsn_sc"  # the offline tool wants it first
        assert {"hsn_sc", "txval", "rt", "desc", "num"} <= set(row) and numbers(row, ("txval", "qty", "iamt", "camt", "samt"))
    for det in doc["doc_issue"]["doc_det"]:
        assert set(det) == {"doc_num", "docs"}
        for d in det["docs"]:
            assert set(d) == {"num", "from", "to", "totnum", "cancel", "net_issue"} and d["net_issue"] == d["totnum"] - d["cancel"]


def test_gstr1_json(gst_shop):
    r = gst_shop.get("/api/exports/gstr1", params={"month": MONTH})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"] and "06ABCDE1234F1Z5" in r.headers["content-disposition"]
    doc = r.json()
    check_gstr1(doc)
    assert doc["gstin"] == "06ABCDE1234F1Z5" and doc["fp"] == MONTH[5:] + MONTH[:4]

    # intra-state B2C: a and b summed per rate; the cancelled bill c is not there
    assert doc["b2cs"] == [{"sply_ty": "INTRA", "pos": "06", "typ": "OE", "rt": 18, "txval": 354.24,
                            "camt": 31.88, "samt": 31.88, "csamt": 0.0}]
    # B2B to a Delhi buyer: IGST
    d = gst_shop.bills["d"]
    assert doc["b2b"] == [{"ctin": "07AAACD1234E1Z5", "inv": [{
        "inum": d["invoice_number"], "idt": f"{d['date_key'][8:]}-{d['date_key'][5:7]}-{d['date_key'][:4]}",
        "val": 236.0, "pos": "07", "rchrg": "N", "inv_typ": "R",
        "itms": [{"num": 1, "itm_det": {"txval": 200.0, "rt": 18, "iamt": 36.0, "csamt": 0.0}}]}]}]
    # B2C over 1 lakh to another state: b2cl, one by one
    assert doc["b2cl"] == [{"pos": "09", "inv": [{
        "inum": gst_shop.bills["e"]["invoice_number"], "idt": doc["b2b"][0]["inv"][0]["idt"], "val": 150000.0,
        "itms": [{"num": 1, "itm_det": {"txval": 127118.64, "rt": 18, "iamt": 22881.36, "csamt": 0.0}}]}]}]
    assert doc["cdnr"] == [] and doc["cdnur"] == []
    assert doc["nil"] == {"inv": [{"sply_ty": "INTRAB2C", "expt_amt": 100.0, "nil_amt": 0.0, "ngsup_amt": 0.0}]}
    assert doc["doc_issue"]["doc_det"] == [{"doc_num": 1, "docs": [
        {"num": 1, "from": "INV-0001", "to": "INV-0005", "totnum": 5, "cancel": 1, "net_issue": 4}]}]

    # No HSN codes yet: one row each under "", flagged; B2B and B2C apart
    hsn = doc["hsn"]
    assert [(h["hsn_sc"], h["rt"], h["txval"]) for h in hsn["hsn_b2b"]] == [("", 18, 200.0)]
    assert {(h["hsn_sc"], h["rt"]) for h in hsn["hsn_b2c"]} == {("", 0), ("", 18)}
    # rounding reconciles: the HSN table's tax equals the invoice tables' tax to the paisa
    lines = [i["itm_det"] for e in doc["b2b"] + doc["b2cl"] for inv in e["inv"] for i in inv["itms"]]
    assert tax_of(hsn["hsn_b2b"] + hsn["hsn_b2c"]) == tax_of(lines + doc["b2cs"])


def test_gstr1_hsn_and_warnings(gst_shop):
    cats = {c["name"]: c["id"] for c in gst_shop.get("/api/categories").json()}
    run(db().categories.update_one({"id": cats["Indoor Plants"]}, {"$set": {"hsn": "0602"}}))
    r = gst_shop.get("/api/exports/gstr3b", params={"month": MONTH}).json()
    assert {w["code"] for w in r["warnings"]} == {"hsn_missing"}
    warn = r["warnings"][0]
    assert warn["params"] == {"n": 4} and warn["key"].startswith("{{n}} bill lines have no HSN code")
    run(db().categories.update_one({"id": cats["Pots"]}, {"$set": {"hsn": "6914"}}))
    doc = gst_shop.get("/api/exports/gstr1", params={"month": MONTH}).json()
    b2c = {(h["hsn_sc"], h["rt"]): h for h in doc["hsn"]["hsn_b2c"]}
    assert b2c[("0602", 0)] == {"num": 1, "hsn_sc": "0602", "desc": "Indoor Plants", "uqc": "NOS", "qty": 1.0,
                                "rt": 0, "txval": 100.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
    assert b2c[("6914", 18)]["qty"] == 5.0  # a's 1 pot, b's 3, e's fountain; not the cancelled bill
    assert "warnings" not in doc  # the portal file holds nothing but the return

    # A bill made "without GST" (decision D6 still open: still allowed) is reported as exempt and flagged.
    pot = next(p for p in gst_shop.get("/api/products").json() if p["name"] == "Ceramic Pot")
    bill(gst_shop, (pot, 1, 118), phone="9811100009", name="Gita", gst_removed=True)
    r = gst_shop.get("/api/exports/gstr3b", params={"month": MONTH}).json()
    assert [w["params"]["n"] for w in r["warnings"] if w["code"] == "bill_without_gst"] == [1]
    assert r["sup_details"]["osup_nil_exmp"] == {"txval": 218.0}


def test_gstr3b(gst_shop):
    r = gst_shop.get("/api/exports/gstr3b", params={"month": MONTH})
    assert r.status_code == 200
    s = r.json()["sup_details"]
    assert s["osup_det"] == {"txval": 127672.88, "iamt": 22917.36, "camt": 31.88, "samt": 31.88, "csamt": 0.0}
    assert s["osup_nil_exmp"] == {"txval": 100.0} and s["osup_nongst"] == {"txval": 0.0}
    assert r.json()["inter_sup"]["unreg_details"] == [{"pos": "09", "txval": 127118.64, "iamt": 22881.36}]


def test_gstr1_csv(gst_shop):
    r = gst_shop.get("/api/exports/gstr1", params={"month": MONTH, "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == sorted(f"{s}.csv" for s in ("b2b", "b2cl", "b2cs", "cdnr", "cdnur", "exemp",
                                                                 "hsn_b2b", "hsn_b2c", "docs"))
    b2cs = list(csv.reader(io.StringIO(zf.read("b2cs.csv").decode())))
    assert b2cs == [["Type", "Place Of Supply", "Applicable % of Tax Rate", "Rate", "Taxable Value", "Cess Amount",
                     "E-Commerce GSTIN"], ["OE", "06-Haryana", "", "18", "354.24", "0", ""]]
    docs = list(csv.reader(io.StringIO(zf.read("docs.csv").decode())))
    assert docs[1] == ["Invoices for outward supply", "INV-0001", "INV-0005", "5", "1"]
    exemp = {row[0]: row for row in csv.reader(io.StringIO(zf.read("exemp.csv").decode()))}
    assert exemp["Intra-State supplies to unregistered persons"][2] == "100.0"
    one = gst_shop.get("/api/exports/gstr1", params={"month": MONTH, "format": "csv", "section": "b2b"})
    assert one.headers["content-type"].startswith("text/csv")
    row = list(csv.reader(io.StringIO(one.text)))[1]
    assert row[0] == "07AAACD1234E1Z5" and row[5] == "07-Delhi" and row[8] == "Regular B2B"
    assert re.match(r"^\d{2}-[A-Z][a-z]{2}-\d{4}$", row[3])


def test_gst_exports_owner_only_and_need_gstin(gst_shop, client):
    _, worker = gst_shop.worker(reports=True)
    assert client.get("/api/exports/gstr1", params={"month": MONTH}, headers=worker).status_code == 403
    assert gst_shop.get("/api/exports/gstr1", params={"month": "2026-13"}).status_code == 422
    plain = Shop(client, phone="9811155667", name="Plain", gst=False)
    r = plain.get("/api/exports/gstr1", params={"month": MONTH})
    assert r.status_code == 400 and r.json()["code"] == "no_gstin"
    # another month: an empty but valid return
    empty = gst_shop.get("/api/exports/gstr1", params={"month": "2020-01"}).json()
    check_gstr1(empty)
    assert empty["doc_issue"] == {"doc_det": []} and empty["hsn"] == {"hsn_b2b": [], "hsn_b2c": []}


def test_month_query_uses_index(gst_shop, mongo):
    start, end = f"{MONTH}-01", f"{MONTH}-31"
    bid = gst_shop.get("/api/auth/me").json()["business"]["id"]
    plan = mongo[TEST_DB].invoices.find({"business_id": bid, "date_key": {"$gte": start, "$lte": end}}).explain()
    text = json.dumps(plan["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in text and "business_id_1_date_key_1" in text


# ---- full shop export ----

def test_shop_export(gst_shop, client):
    r = gst_shop.post("/api/exports/shop")
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["status"] == "queued"
    run(jobs.drain())
    got = gst_shop.get(f"/api/exports/shop/{started['id']}").json()
    assert got["status"] == "ready" and got["counts"]["invoices"] == 5 and got["size"] > 0
    assert "/api/exports/shop/" in got["url"] and "token=" in got["url"]

    path = got["url"].split("://", 1)[1].split("/", 1)[1]
    r = client.get("/" + path)  # the signed link needs no login header
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"business.json", "invoices.jsonl", "customers.jsonl", "users.jsonl", "audit_log.jsonl", "invoices.csv",
            "README.txt"} <= names
    users = [json.loads(line) for line in zf.read("users.jsonl").decode().splitlines()]
    assert users and all("password_hash" not in u and "token_version" not in u for u in users)
    bills = [json.loads(line) for line in zf.read("invoices.jsonl").decode().splitlines()]
    assert sorted(b["invoice_number"] for b in bills) == [f"INV-000{i}" for i in range(1, 6)]
    assert zf.read("invoices.csv").decode("utf-8-sig").splitlines()[0].startswith("invoice_number,date_key")

    # the link is for this export only, and only while signed
    assert client.get(f"/api/exports/shop/{started['id']}/file", params={"token": "x"}).status_code == 403
    other = Shop(client, phone="9811155667", name="Other")
    assert other.get(f"/api/exports/shop/{started['id']}").status_code == 404
    assert [e["id"] for e in gst_shop.get("/api/exports/shop").json()] == [started["id"]]
    assert other.get("/api/exports/shop").json() == []
    _, worker = gst_shop.worker()
    assert client.post("/api/exports/shop", headers=worker).status_code == 403

    # after its time the file goes and the export says so
    run(db().exports.update_one({"id": started["id"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}}))
    assert gst_shop.get(f"/api/exports/shop/{started['id']}").json()["status"] == "expired"
    stored = storage_client.LOCAL_DIR / run(db().exports.find_one({"id": started["id"]}))["path"]
    assert stored.is_file()
    assert run(exports.purge_exports({"db": db()})) == 1
    assert not stored.exists()
    assert run(db().exports.find_one({"id": started["id"]}))["status"] == "expired"


def test_one_export_at_a_time(gst_shop):
    run(db().exports.insert_one({"id": "busy", "business_id": gst_shop.get("/api/auth/me").json()["business"]["id"],
                                 "status": "running", "created_at": datetime.now(timezone.utc)}))
    r = gst_shop.post("/api/exports/shop")
    assert r.status_code == 409 and r.json()["code"] == "export_running"


def test_export_job_runs_once(gst_shop):
    started = gst_shop.post("/api/exports/shop").json()
    run(jobs.drain())
    assert run(exports.build_shop_export({"db": db()}, started["id"])) is None  # already done: left alone
    assert run(db().exports.find_one({"id": started["id"]}))["tries"] == 1


# ---- audit log ----

def test_audit_log_read(gst_shop, client):
    gst_shop.get("/api/exports/gstr1", params={"month": MONTH})
    gst_shop.post("/api/exports/shop")
    run(jobs.drain())
    rows = gst_shop.get("/api/audit-log").json()
    assert [r["action"] for r in rows] == ["export.shop", "export.gstr1"]
    assert rows[0]["actor"]["role"] == "owner" and rows[0]["at"].endswith("+00:00")
    assert [r["action"] for r in gst_shop.get("/api/audit-log", params={"action": "export.gstr1"}).json()] == ["export.gstr1"]
    assert len(gst_shop.get("/api/audit-log", params={"action": "export"}).json()) == 2
    page = gst_shop.get("/api/audit-log", params={"before": rows[0]["at"]}).json()
    assert [r["action"] for r in page] == ["export.gstr1"]
    assert gst_shop.get("/api/audit-log", params={"action": "x$where"}).status_code == 422
    other = Shop(client, phone="9811155667", name="Other")
    assert other.get("/api/audit-log").json() == []
    _, worker = gst_shop.worker(reports=True)
    assert client.get("/api/audit-log", headers=worker).status_code == 403


def test_audit_log_is_append_only_in_code():
    """Only audit.py touches the log, and only with insert and find."""
    writes = re.compile(r"(audit_log|audit\.collection\([^)]*\))\s*\.\s*(update|delete|replace|find_one_and|drop|bulk_write|rename)")
    found = [f"{p.relative_to(BACKEND)}:{n}" for p in BACKEND.rglob("*.py") if "tests" not in p.parts
             for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if writes.search(line)]
    assert found == []
    role = audit.role_definition("hh_audit")
    assert role["privileges"] == [{"resource": {"db": "hh_audit", "collection": "audit_log"}, "actions": ["find", "insert"]}]


def test_audit_log_in_its_own_database(client, mongo, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", f"{TEST_DB}_audit")
    try:
        shop = Shop(client)
        shop.get("/api/exports/gstr3b", params={"month": MONTH})
        assert mongo[f"{TEST_DB}_audit"].audit_log.count_documents({"action": "export.gstr3b"}) == 1
        assert mongo[TEST_DB].audit_log.count_documents({"action": "export.gstr3b"}) == 0
        assert [r["action"] for r in shop.get("/api/audit-log").json()] == ["export.gstr3b"]
    finally:
        mongo.drop_database(f"{TEST_DB}_audit")


def test_audit_daily_archive(client):
    shop = Shop(client)
    shop.get("/api/exports/gstr3b", params={"month": MONTH})
    today = datetime.now(audit.IST).date()
    rec = run(audit.archive_day(db(), today))
    assert rec["count"] == 1 and len(rec["sha256"]) == 64
    data = (storage_client.LOCAL_DIR / rec["path"]).read_bytes()
    lines = gzip.decompress(data).decode().splitlines()
    assert json.loads(lines[0])["action"] == "export.gstr3b"
    assert run(audit.archive_day(db(), today))["skipped"] is True  # once per day
    assert run(audit.archive_day(db(), today - timedelta(days=400)))["count"] == 0


# ---- retention and migrations ----

def test_retention(client):
    now = datetime.now(timezone.utc)
    run(db().carts.insert_many([
        {"business_id": "b", "customer_id": "old", "updated_at": (now - timedelta(days=8)).isoformat()},
        {"business_id": "b", "customer_id": "new", "updated_at": (now - timedelta(days=2)).isoformat()},
        {"business_id": "b", "customer_id": "old-date", "updated_at": now - timedelta(days=9)}]))
    run(db().ai_calls.insert_many([{"at": now - timedelta(days=181)}, {"at": now - timedelta(days=179)}]))
    removed = run(retention.apply(db(), now))
    assert removed["carts"] == 2 and removed["ai_calls"] == 1
    assert [c["customer_id"] for c in run(db().carts.find({}).to_list(10))] == ["new"]


def test_retention_indexes(client, mongo):
    idx = {i["name"]: i for i in mongo[TEST_DB].audit_log.list_indexes()}
    assert idx["at_1"]["expireAfterSeconds"] == audit.KEEP_DAYS * 86400
    idx = {i["name"]: i for i in mongo[TEST_DB].exports.list_indexes()}
    assert idx["delete_after_1"]["expireAfterSeconds"] == 0 and idx["id_1"]["unique"]
    assert mongo[TEST_DB].carts.index_information()["updated_at_1"]["expireAfterSeconds"] == 7 * 86400
    assert "business_id_1_date_key_1" in mongo[TEST_DB].invoices.index_information()
    # running the migration again changes nothing
    run(importlib.import_module("migrations.1001_retention").up(db()))


# ---- demo shop guards ----

def test_demo_refresh_guards(client, monkeypatch):
    shop = Shop(client)
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    with pytest.raises(demo_data.NotAllowed, match="not a demo shop"):
        run(demo_data.refresh(db(), "9811122334"))
    # shops seeded before the mark: migration 1002 marks them
    run(db().products.insert_one({"id": "p", "business_id": bid, "name": "x", "demo": True}))
    run(importlib.import_module("migrations.1002_demo_flag").up(db()))
    assert shop.get("/api/business").json()["demo"] is True
    assert run(demo_data.refresh(db(), "9811122334"))["shifted_days"] == 0  # allowed now (nothing to move)
    shop.c.put("/api/business", headers=shop.h, json={"demo": False})  # the app cannot unset or set it
    assert shop.get("/api/business").json()["demo"] is True

    monkeypatch.setattr(demo_data, "get_settings", lambda: SimpleNamespace(is_production=True))
    with pytest.raises(demo_data.NotAllowed, match="production"):
        run(demo_data.refresh(db(), "9811122334", today=date.today()))
    with pytest.raises(demo_data.NotAllowed, match="production"):
        run(demo_data.seed(db(), "9811122334"))
