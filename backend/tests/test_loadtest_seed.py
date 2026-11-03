"""WP-11 load test data: shops written by loadtest/seed.py can be billed through the API exactly as the k6 and
locust scenarios do it. A schema change elsewhere that the seeder does not follow fails here, not mid-load-test."""
import importlib.util
import uuid
from pathlib import Path

import pytest

from conftest import MOCK, TEST_DB

pytestmark = pytest.mark.skipif(MOCK, reason="writes with PyMongo into the real test database")
SEED = Path(__file__).resolve().parents[2] / "loadtest" / "seed.py"


def _seed_module():
    spec = importlib.util.spec_from_file_location("loadtest_seed", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeded_shop_bills_like_the_load_test(client, mongo):
    seed = _seed_module()
    data = seed.seed(mongo[TEST_DB], shops=2, products=45, customers=30, tokens=True, log=lambda *_: None)
    assert len(data["shops"]) == 2 and all(s["token"] for s in data["shops"])
    assert mongo[TEST_DB].products.count_documents({"loadtest": True}) == 90
    assert len({p["name"] for p in mongo[TEST_DB].products.find({"business_id": data["shops"][0]["business_id"]})}) == 45

    shop, other = data["shops"]
    h = {"Authorization": f"Bearer {shop['token']}"}
    # the shared password works too (the load test logs in when shops.json has no tokens)
    login = client.post("/api/auth/login", json={"phone": shop["phone"], "password": data["password"]})
    assert login.status_code == 200, login.text

    recent = client.get("/api/customers/find?q=", headers=h)
    assert recent.status_code == 200 and len(recent.json()) == 20
    found = client.get("/api/customers/find?q=" + data["customer_terms"][0], headers=h).json()
    assert found, "a customer search term finds nobody"
    customer = found[0]
    assert client.get(f"/api/orders/cart?customer_id={customer['id']}", headers=h).status_code == 200

    hits = client.get("/api/products?search=mon", headers=h).json()
    assert hits and hits[0]["name"].startswith("Money Plant")
    p = hits[0]
    line = {"product_id": p["id"], "name": p["name"], "quantity": 2, "price": p["selling_price"],
            "list_price": p["selling_price"], "gst_percent": p["gst_percent"], "image_path": p["image_path"],
            "stock": p["stock"], "unit": p["unit"]}
    r = client.put("/api/orders/cart", headers=h, json={"customer_id": customer["id"], "items": [line], "discount": 0,
                                                        "gst_removed": False})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/products?ids={p['id']}", headers=h).status_code == 200

    bill = {"items": [{"product_id": p["id"], "name": p["name"], "quantity": 2, "price": p["selling_price"],
                       "unit": None}], "customer_id": customer["id"], "discount": 0, "payment_method": "UPI",
            "gst_removed": False}
    r = client.post("/api/invoices", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=bill)
    assert r.status_code == 200, r.text
    invoice = r.json()
    assert invoice["invoice_number"] and invoice["grand_total"] == 2 * p["selling_price"]

    assert client.get(f"/api/invoices/{invoice['id']}", headers=h).status_code == 200
    assert client.get("/api/dashboard/today", headers=h).status_code == 200
    share = client.get(f"/api/invoices/{invoice['id']}/share", headers=h).json()
    assert client.get(f"/i/{share['token']}").status_code == 200
    # the other load-test shop sees none of it
    oh = {"Authorization": f"Bearer {other['token']}"}
    assert client.get(f"/api/invoices/{invoice['id']}", headers=oh).status_code == 404


def test_seed_cli_refuses_a_real_database(monkeypatch):
    seed = _seed_module()
    monkeypatch.setattr("sys.argv", ["seed.py", "--db", "nurserypos"])
    with pytest.raises(SystemExit, match="must contain 'loadtest'"):
        seed.main()
