"""Customers: editing one, the customer's address on their bills, search, pages, one customer per number, erasing."""
import asyncio
import re
from pathlib import Path

import pytest

import db as db_module
from conftest import MOCK, Shop, run
from routers import customers

BACKEND = Path(__file__).resolve().parent.parent
needs_mongo = pytest.mark.skipif(MOCK, reason="needs a real MongoDB")


def _bill(shop, customer_id):
    r = shop.post("/api/invoices", {"items": [{"product_id": None, "name": "Rose", "quantity": 1, "price": 100}],
                                    "customer_id": customer_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_edit_customer(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    shop.post("/api/customers", {"name": "Rajesh", "phone": "9811155667"})
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h,
                     json={"name": " Meena Gupta ", "phone": "+91 98111 00077", "address": "12 Mall Road\nSector 5"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["phone"], r.json()["address"]) == ("Meena Gupta", "+919811100077", "12 Mall Road\nSector 5")
    got = shop.get(f"/api/customers/{c['id']}").json()
    assert (got["name"], got["phone"], got["address"]) == ("Meena Gupta", "+919811100077", "12 Mall Road\nSector 5")

    # Only the fields sent change; clearing a field is allowed.
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"address": ""})
    assert r.json()["address"] == "" and r.json()["name"] == "Meena Gupta"
    # Saving its own number again is fine; another customer's number is not.
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "9811100077"}).status_code == 200
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "98111 55667"})
    assert r.status_code == 409
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "12345"}).status_code == 400
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "  "}).status_code == 400
    assert client.patch("/api/customers/nope", headers=shop.h, json={"name": "X"}).status_code == 404


def test_edit_customer_is_per_shop_and_needs_customers_permission(client):
    shop = Shop(client)
    other = Shop(client, phone="9811199887", name="Kamal")
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    # The same number in another shop is not a clash, and another shop can't edit this customer.
    other.post("/api/customers", {"name": "Someone", "phone": "9811100088"})
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "9811100088"}).status_code == 200
    assert client.patch(f"/api/customers/{c['id']}", headers=other.h, json={"name": "Hacked"}).status_code == 404
    assert shop.get(f"/api/customers/{c['id']}").json()["name"] == "Meena"

    _, seller = shop.worker(customers=False)
    assert client.patch(f"/api/customers/{c['id']}", headers=seller, json={"name": "X"}).status_code == 403
    _, helper = shop.worker(phone="9822233446", sell=False)
    assert client.patch(f"/api/customers/{c['id']}", headers=helper, json={"name": "Meena G"}).status_code == 200


def test_bill_keeps_the_customer_address_of_the_day(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066", "address": "12 Mall Road <b>"}).json()
    inv = _bill(shop, c["id"])
    assert inv["customer_address"] == "12 Mall Road <b>"
    got = shop.get(f"/api/invoices/{inv['id']}").json()
    assert got["shop"] == {"name": "Suresh's Nursery", "address": "Sector 5", "phone": "9811122334"}
    page = shop.get(f"/api/invoices/{inv['id']}/html").text
    assert "Bill to" in page and "12 Mall Road &lt;b&gt;" in page and "Sector 5" in page

    # Editing the customer later leaves the bill as it was.
    client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "Meena Gupta", "address": "New Colony"})
    got = shop.get(f"/api/invoices/{inv['id']}").json()
    assert (got["customer_name"], got["customer_address"]) == ("Meena", "12 Mall Road <b>")
    url = shop.get(f"/api/invoices/{inv['id']}/share").json()["url"]
    public = client.get(url.replace("https://pos.test", "")).text
    assert "12 Mall Road &lt;b&gt;" in public and "New Colony" not in public
    assert _bill(shop, c["id"])["customer_address"] == "New Colony"


def test_old_bill_without_address_still_renders(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066", "address": "12 Mall Road"}).json()
    inv = _bill(shop, c["id"])
    db = db_module.state.db  # a bill made before addresses were kept has no such field
    run(db.invoices.update_one({"id": inv["id"]}, {"$unset": {"customer_address": ""}}))
    assert "customer_address" not in shop.get(f"/api/invoices/{inv['id']}").json()
    page = shop.get(f"/api/invoices/{inv['id']}/html").text
    assert "Meena" in page and "12 Mall Road" not in page and 'class="m addr"' not in page


def _names(rows):
    return [c["name"] for c in rows]


def test_search_from_any_word_and_either_end_of_the_number(client):
    shop = Shop(client)
    shop.post("/api/customers", {"name": "Meena Gupta", "phone": "9811100066"})
    shop.post("/api/customers", {"name": "Anita Sharma", "phone": "9822233445"})
    shop.post("/api/customers", {"name": "गीता देवी", "phone": "7011122233"})

    def find(q):
        return _names(shop.get("/api/customers/find", params={"q": q}).json())

    def search(q):
        return _names(shop.get("/api/customers", params={"search": q}).json())

    assert find("gup") == ["Meena Gupta"] and find("MEENA g") == ["Meena Gupta"] and find("sharma") == ["Anita Sharma"]
    assert find("देवी") == ["गीता देवी"]
    assert find("98111") == ["Meena Gupta"] and find("0066") == ["Meena Gupta"]  # first or last digits
    assert find("+91 98222") == ["Anita Sharma"] and find("098222 33445") == ["Anita Sharma"]
    assert find("eena") == [] and find("1110") == []  # only from the start of a word or either end of the number
    assert search("gupta") == ["Meena Gupta"] and search("+9198111") == ["Meena Gupta"] and search("445") == ["Anita Sharma"]
    assert search("(") == [] and find("*") == []
    assert len(shop.get("/api/customers").json()) == 3
    # the search fields stay out of every response
    for row in [*shop.get("/api/customers").json(), *shop.get("/api/customers/find", params={"q": "a"}).json()]:
        assert not set(customers.SEARCH_FIELDS) & set(row)


def test_editing_a_customer_updates_the_search(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "Meena Gupta", "phone": "9811100077"})
    assert _names(shop.get("/api/customers/find", params={"q": "gupta"}).json()) == ["Meena Gupta"]
    assert _names(shop.get("/api/customers/find", params={"q": "0077"}).json()) == ["Meena Gupta"]
    assert shop.get("/api/customers/find", params={"q": "0066"}).json() == []
    # a change of name or number goes to the audit log, without the number
    entries = run(db_module.state.db.audit_log.find({"action": "customer.update"}, {"_id": 0}).to_list(10))
    assert len(entries) == 1 and entries[0]["details"] == {"fields": ["name", "phone"]}
    assert "9811100077" not in str(entries[0])


def test_customers_saved_without_search_fields_are_found(client):
    """Customers saved before the search fields existed (older bills, older demo data) are filled in and found."""
    shop = Shop(client)
    r = shop.post("/api/invoices", {"items": [{"product_id": None, "name": "Rose", "quantity": 1, "price": 100}],
                                    "customer_phone": "9811100066", "customer_name": "Kavita Rao"})
    assert r.status_code == 200, r.text
    db = db_module.state.db
    run(db.customers.update_many({}, {"$unset": {f: "" for f in customers.SEARCH_FIELDS}}))
    assert _names(shop.get("/api/customers/find", params={"q": "rao"}).json()) == ["Kavita Rao"]
    assert run(db.customers.find_one({}, {"_id": 0}))["phone10_rev"] == "6600011189"
    run(db.customers.update_many({}, {"$unset": {f: "" for f in customers.SEARCH_FIELDS}}))
    assert shop.get("/api/customers", params={"limit": 5}).json()["total"] == 1


def test_pages_reach_every_customer_and_the_old_list_stays(client):
    shop = Shop(client)
    for i, name in enumerate(["Zoya", "amit", "Bina", "Bina", "Chetan", "Deepak", "esha"]):
        shop.post("/api/customers", {"name": name, "phone": f"98111000{i:02d}"})
    old = shop.get("/api/customers").json()
    assert isinstance(old, list) and _names(old) == ["amit", "Bina", "Bina", "Chetan", "Deepak", "esha", "Zoya"]  # case aside

    seen, cursor, pages = [], None, []
    while True:
        page = shop.get("/api/customers", params={"limit": 3, **({"cursor": cursor} if cursor else {})}).json()
        pages.append(page)
        seen += page["items"]
        if not (cursor := page["next_cursor"]):
            break
    assert [len(p["items"]) for p in pages] == [3, 3, 1] and ["total" in p for p in pages] == [True, False, False]
    assert pages[0]["total"] == 7 and [c["id"] for c in seen] == [c["id"] for c in old]
    assert not set(customers.SEARCH_FIELDS) & set(seen[0])

    page = shop.get("/api/customers", params={"limit": 1, "search": "bina"}).json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["next_cursor"]
    rest = shop.get("/api/customers", params={"limit": 1, "search": "bina", "cursor": page["next_cursor"]}).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None and rest["items"][0]["id"] != page["items"][0]["id"]
    assert shop.get("/api/customers", params={"cursor": "not-a-cursor"}).status_code == 400
    assert shop.get("/api/customers", params={"limit": 0}).status_code == 422
    assert shop.get("/api/customers", params={"limit": 3, "search": "("}).json() == {"items": [], "next_cursor": None, "total": 0}


def test_adding_a_saved_number_returns_that_customer(client):
    shop = Shop(client)
    first = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066", "address": "Mall Road"}).json()
    assert first["existing"] is False and first["name"] == "Meena" and not set(customers.SEARCH_FIELDS) & set(first)
    again = shop.post("/api/customers", {"name": "Meena Gupta", "phone": "+91 98111 00066", "address": "New"})
    assert again.status_code == 200
    assert again.json()["id"] == first["id"] and again.json()["existing"] is True
    assert (again.json()["name"], again.json()["address"]) == ("Meena", "Mall Road")  # what was saved is kept
    assert len(shop.get("/api/customers").json()) == 1
    other = Shop(client, phone="9811199887", name="Kamal")  # the same number in another shop is that shop's own
    assert other.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()["existing"] is False


@needs_mongo
def test_many_phones_adding_one_number_at_once_make_one_customer(client):
    Shop(client)
    db = db_module.state.db
    owner = run(db.users.find_one({"phone": "+919811122334"}, {"_id": 0}))
    # the database decides the race through the unique {business_id, phone} index (migration 0102, WP-1)
    assert any(ix.get("unique") and [k for k, _ in ix["key"]] == ["business_id", "phone"]
               for ix in run(db.customers.index_information()).values())
    body = customers.CustomerIn(name="Meena", phone="9811100066")

    async def together():
        return await asyncio.gather(*(customers.create_customer(body, owner) for _ in range(8)))

    made = run(together())
    assert len({c["id"] for c in made}) == 1 and sum(not c["existing"] for c in made) == 1
    assert run(db.customers.count_documents({"phone": "+919811100066"})) == 1


def test_erase_customer(client):
    shop = Shop(client)
    db = db_module.state.db
    c = shop.post("/api/customers", {"name": "Meena Gupta", "phone": "9811100066", "address": "12 Mall Road",
                                     "email": "meena@example.com", "notes": "Likes roses"}).json()
    bills = [_bill(shop, c["id"]) for _ in range(3)]
    run(db.invoices.update_one({"id": bills[2]["id"]}, {"$set": {"customer_gstin": "06ABCDE1234F1Z5"}}))  # a B2B bill
    before = {b["id"]: b["grand_total"] for b in bills}
    summary = shop.get("/api/invoices/summary").json()
    totals = shop.get(f"/api/customers/{c['id']}").json()

    # owner only, own shop only
    _, helper = shop.worker()
    assert client.post(f"/api/customers/{c['id']}/erase", headers=helper).status_code == 403
    other = Shop(client, phone="9811199887", name="Kamal")
    assert other.post(f"/api/customers/{c['id']}/erase").status_code == 404

    r = shop.post(f"/api/customers/{c['id']}/erase", {"reason": "Customer asked"})
    assert r.status_code == 200, r.text
    got = shop.get(f"/api/customers/{c['id']}").json()
    assert (got["name"], got["phone"], got["email"], got["address"], got["notes"]) == ("Erased customer", "", "", "", "")
    assert got["erased_at"] and (got["total_purchases"], got["purchase_count"]) == (totals["total_purchases"], 3)
    assert len(got["invoices"]) == 3

    for b in bills[:2]:  # B2C bills: no phone or address left, on the bill or on its public page
        inv = shop.get(f"/api/invoices/{b['id']}").json()
        assert (inv["customer_phone"], inv["customer_address"]) == ("", "")
        url = shop.get(f"/api/invoices/{b['id']}/share").json()["url"]
        page = client.get(url.replace("https://pos.test", "")).text
        assert "9811100066" not in page and "Mall Road" not in page
    b2b = shop.get(f"/api/invoices/{bills[2]['id']}").json()
    assert (b2b["customer_phone"], b2b["customer_address"]) == ("+919811100066", "12 Mall Road")
    # the totals are unchanged everywhere
    assert {b["id"]: shop.get(f"/api/invoices/{b['id']}").json()["grand_total"] for b in bills} == before
    assert shop.get("/api/invoices/summary").json() == summary

    entry = run(db.audit_log.find_one({"action": "customer.erase"}, {"_id": 0}))
    assert entry["target"] == {"type": "customer", "id": c["id"]} and entry["details"] == {"bills": 2, "reason": "Customer asked"}
    assert "9811100066" not in str(entry) and "Meena" not in str(entry)

    # Erasing again changes nothing; the erased customer can't be found, edited or matched by number any more.
    assert shop.post(f"/api/customers/{c['id']}/erase").status_code == 200
    assert run(db.audit_log.count_documents({"action": "customer.erase"})) == 1
    assert shop.get("/api/customers/find").json() == [] and shop.get("/api/customers/find", params={"q": "meena"}).json() == []
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "X"}).status_code == 400
    assert shop.get("/api/customers/lookup", params={"phone": "9811100066"}).json()["found"] is False
    again = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    assert again["existing"] is False and again["id"] != c["id"]
    assert shop.post("/api/customers/nope/erase").status_code == 404


def test_no_unanchored_regex():
    """Every $regex in the server starts with ^: a pattern matched anywhere in a value can't use an index."""
    pending = {"routers/sales.py"}  # the Orders bill search (within one shop's bills): still to be rewritten
    bad = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith("tests/") or rel in pending:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']\$regex["']""", line) and not re.search(r"""\$regex["']\s*:\s*([rf]{0,2}["']\^|["']\^["']\s*\+)""", line):
                bad.append(f"{rel}:{no}: {line.strip()}")
    assert not bad, "unanchored $regex:\n" + "\n".join(bad)


@needs_mongo
def test_customer_search_examines_at_most_twice_the_results(client):
    shop = Shop(client)
    other = Shop(client, phone="9811199887", name="Kamal")
    db = db_module.state.db
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    first = ["Meena", "Anita", "Rajesh", "Sunil", "Kavita", "Mohan", "Rekha", "Arjun", "Pooja", "Vikram"]
    last = ["Gupta", "Sharma", "Verma", "Singh", "Rao", "Jain", "Mehta", "Yadav"]
    docs = []
    for i in range(800):
        name, phone = f"{first[i % 10]} {last[(i // 10) % 8]} {i}", f"+9198{i:08d}"
        docs.append({"id": f"c{i:04d}", "business_id": bid, "name": name, "phone": phone, "purchase_count": 0,
                     "last_purchase_at": f"2026-10-{1 + i % 28:02d}", **customers.search_fields(name, phone)})
    run(db.customers.insert_many(docs))
    other.post("/api/customers", {"name": "Meena Gupta", "phone": "9800000001"})  # another shop's: never examined

    async def explain(query, sort=None, limit=0):
        cmd = {"find": "customers", "filter": query, **({"sort": dict(sort)} if sort else {}),
               **({"limit": limit} if limit else {})}
        stats = (await db.command("explain", cmd, verbosity="executionStats"))["executionStats"]
        return stats["totalDocsExamined"], stats["nReturned"]

    live = {"business_id": bid, "erased_at": None}
    for q in ("meena", "gupta", "meena gupta 1", "98000001", "0007", "+91 98000"):
        ors = customers._search(q)
        for query, sort, limit in (({**live, "$or": ors}, None, customers.FIND_MAX),  # /customers/find
                                   ({"business_id": bid, "$or": ors}, [("name_key", 1), ("id", 1)], 0)):  # /customers?search
            examined, returned = run(explain(query, sort, limit))
            assert returned and examined <= 2 * returned, (q, examined, returned)
    # the list's pages and the most recent customers come straight from their indexes
    after = {"$or": [{"name_key": {"$gt": "meena"}}, {"name_key": "meena", "id": {"$gt": ""}}]}
    for query, sort in (({"business_id": bid}, [("name_key", 1), ("id", 1)]),
                        ({"$and": [{"business_id": bid}, after]}, [("name_key", 1), ("id", 1)]),
                        (live, [("last_purchase_at", -1)])):
        examined, returned = run(explain(query, sort, 51))
        assert returned == 51 and examined <= 2 * returned, (query, examined, returned)
