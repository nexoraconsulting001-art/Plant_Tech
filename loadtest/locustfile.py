"""Locust load test: the same bill scenario as k6/bill.js, for when Python is handier than k6.

  MULT=1 locust -f loadtest/locustfile.py --host https://staging-api.example --headless --processes -1 \\
      --csv loadtest/results/locust-1x --html loadtest/results/locust-1x.html
  (MULT=3 and MULT=5 for 42 and 70 bills/s; locust is in backend/requirements-dev.txt)

Each simulated user makes one bill every PACE seconds (constant pacing), so bills/s = users / PACE and the shape
below starts PEAK x MULT x PACE users. Environment: MULT (1/3/5), PEAK (default 14 bills/s), PACE (default 20 s:
longer than one bill takes, or the rate falls short), DURATION and RAMP (seconds, default 600 and 60), THINK
(multiplies the pauses, default 1), SHOPS_FILE (default loadtest/shops.json from seed.py).
Pass/fail: locust exits 1 if more than 1% of requests failed or the p95 of POST /api/invoices is over 500 ms.
Never point this at production: it creates real bills.
"""
import itertools
import json
import math
import os
import random
import time
import uuid
from pathlib import Path

from locust import FastHttpUser, LoadTestShape, constant_pacing, events, task

MULT = float(os.environ.get("MULT", "1"))
PEAK = float(os.environ.get("PEAK", "14"))
PACE = float(os.environ.get("PACE", "20"))
THINK = float(os.environ.get("THINK", "1"))
DURATION = int(os.environ.get("DURATION", "600"))
RAMP = int(os.environ.get("RAMP", "60"))
DATA = json.loads(Path(os.environ.get("SHOPS_FILE") or Path(__file__).parent / "shops.json").read_text(encoding="utf-8"))
SHOPS = DATA["shops"]
_next_shop = itertools.count(os.getpid() * 7919)  # each --processes worker starts at a different shop


def pause(seconds: float):
    if THINK > 0:
        time.sleep(seconds * THINK * random.uniform(0.7, 1.3))  # gevent-patched: yields to other users


class BillUser(FastHttpUser):
    wait_time = constant_pacing(PACE)

    def on_start(self):
        self.shop = SHOPS[next(_next_shop) % len(SHOPS)]
        token = self.shop.get("token")
        if not token:
            r = self.client.post("/api/auth/login", json={"phone": self.shop["phone"], "password": DATA["password"]},
                                 name="POST /api/auth/login")
            token = r.json()["token"] if r.status_code == 200 else None
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str, name: str):
        with self.client.get(path, headers=self.headers, name=name, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"{r.status_code}")
                return None
            return r.json()

    def type_search(self, term: str, url: str, name: str):
        """One request per kept keystroke from the 2nd letter, as the app's debounced search does."""
        last = None
        for k in range(2, len(term) + 1):
            if k < len(term) and random.random() < 0.5:
                continue
            last = self.get(url + term[:k], name)
            pause(0.35)
        return last

    @task
    def bill(self):
        found = self.get("/api/customers/find?q=", "GET /api/customers/find") or []
        pause(1)
        found = self.type_search(random.choice(DATA["customer_terms"]), "/api/customers/find?q=",
                                 "GET /api/customers/find") or found
        if not found:
            return
        customer = found[0]
        self.get(f"/api/orders/cart?customer_id={customer['id']}", "GET /api/orders/cart")

        lines = []
        for _ in range(random.randint(2, 5)):
            pause(1.5)
            products = self.type_search(random.choice(DATA["product_terms"]), "/api/products?search=",
                                        "GET /api/products?search") or []
            p = next((x for x in products if all(line["product_id"] != x["id"] for line in lines)), None)
            if not p:
                continue
            lines.append({"product_id": p["id"], "name": p["name"], "quantity": random.randint(1, 3),
                          "price": p["selling_price"], "list_price": p["selling_price"],
                          "gst_percent": p.get("gst_percent", 0), "image_path": p.get("image_path"),
                          "stock": p.get("stock"), "unit": p.get("unit")})
            pause(0.6)
            self.client.put("/api/orders/cart", headers=self.headers, name="PUT /api/orders/cart",
                            json={"customer_id": customer["id"], "items": lines, "discount": 0, "gst_removed": False})
        if not lines:
            return

        pause(2)
        self.get("/api/products?ids=" + ",".join(line["product_id"] for line in lines), "GET /api/products?ids")
        bill = {"items": [{"product_id": line["product_id"], "name": line["name"], "quantity": line["quantity"],
                           "price": line["price"], "unit": None} for line in lines],
                "customer_id": customer["id"], "discount": 0, "gst_removed": False,
                "payment_method": random.choice(["Cash", "Cash", "UPI", "Card"])}
        headers = {**self.headers, "Idempotency-Key": str(uuid.uuid4())}
        invoice = None
        for attempt in range(2):  # retried once with the same key if the answer never arrived
            with self.client.post("/api/invoices", json=bill, headers=headers, name="POST /api/invoices",
                                  catch_response=True) as r:
                if r.status_code == 200:
                    invoice = r.json()
                    break
                r.failure(f"{r.status_code}")
                if r.status_code and r.status_code < 500:
                    break
            time.sleep(1)
        if not invoice:
            return

        self.get(f"/api/invoices/{invoice['id']}", "GET /api/invoices/{id}")
        self.get("/api/dashboard/today", "GET /api/dashboard/today")
        if random.random() < 0.5:
            share = self.get(f"/api/invoices/{invoice['id']}/share", "GET /api/invoices/{id}/share")
            if share:
                self.client.get(f"/i/{share['token']}", name="GET /i/{token}")


class BillRate(LoadTestShape):
    """Ramp to PEAK x MULT bills/s (users = rate x PACE) over RAMP seconds, hold for DURATION, then stop."""

    def tick(self):
        users = math.ceil(PEAK * MULT * PACE)
        run = self.get_run_time()
        if run > RAMP + DURATION:
            return None
        return users, max(1.0, users / max(RAMP, 1))


@events.quitting.add_listener
def _verdict(environment, **_):
    stats = environment.stats
    invoices = stats.get("POST /api/invoices", "POST")
    p95 = invoices.get_response_time_percentile(0.95) if invoices.num_requests else 0
    if stats.total.fail_ratio > 0.01 or p95 > 500:
        print(f"FAIL: error ratio {stats.total.fail_ratio:.2%}, POST /api/invoices p95 {p95:.0f} ms")
        environment.process_exit_code = 1
