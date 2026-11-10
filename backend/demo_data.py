"""Demo data for a showcase shop, so the app always looks like a busy nursery.

  python demo_data.py seed --phone 9654346434      # once: 25 plants, 15 other products, customers, 30 days of orders
  python demo_data.py refresh --phone 9654346434   # daily at 5:30 am (cron): every date moves forward so the
                                                    # newest day is today again; nothing ever looks old
  python demo_data.py remove --phone 9654346434    # take the demo products, customers and orders out again

Orders follow a fixed plan per day, with shop hours 9 am to 8:30 pm and busier weekends; every order has a
customer. Seeding and removing touch only rows marked demo=True. Seeding marks the shop itself as a demo shop
(`demo: true` on the business; only this script and migration 1002 set it, the app can't). Refresh moves the
dates of everything in the shop (its own test bills too), so it runs only on a shop marked demo.
Production (owner, 11 Nov 2026: the demo shop 9654346434 on production too, refreshed daily): bills there are tax
records, so on production seeding goes only into a new, empty shop (nothing of its own: no products or bills that
aren't demo rows), never into a shop that sells; the refresh then runs only on that marked shop, as everywhere.
Removing demo rows is allowed everywhere.
"""
import argparse
import asyncio
import random
import secrets
import sys
from datetime import date, datetime, timedelta, timezone

from pymongo import ReturnDocument

import rollups
from common import IST, date_key, new_id, normalize_in_phone, now_ist
from db import get_db
from routers import customers, library
from routers.business import SUGGESTED_CATEGORIES, _create_category
from routers.products import context, insert_product
from routers.sales import _next_number, price_lines
from settings import get_settings

DAYS_BACK = 30

# name, botanical, category, price, cost, usual stock, popularity
PLANTS = [
    ("Money Plant", "Epipremnum aureum", "Indoor Plants", 199, 80, 40, 10),
    ("Snake Plant", "Sansevieria trifasciata", "Indoor Plants", 349, 150, 25, 8),
    ("Areca Palm", "Dypsis lutescens", "Indoor Plants", 599, 280, 15, 6),
    ("Peace Lily", "Spathiphyllum wallisii", "Indoor Plants", 399, 180, 20, 7),
    ("ZZ Plant", "Zamioculcas zamiifolia", "Indoor Plants", 549, 240, 12, 4),
    ("Rubber Plant", "Ficus elastica", "Indoor Plants", 449, 200, 15, 5),
    ("Spider Plant", "Chlorophytum comosum", "Indoor Plants", 149, 60, 30, 6),
    ("Syngonium", "Syngonium podophyllum", "Indoor Plants", 179, 70, 25, 5),
    ("Fiddle Leaf Fig", "Ficus lyrata", "Indoor Plants", 899, 420, 8, 2),
    ("Philodendron Birkin", "Philodendron 'Birkin'", "Indoor Plants", 499, 220, 10, 3),
    ("Jade Plant", "Crassula ovata", "Succulents", 249, 100, 25, 6),
    ("Aloe Vera", "Aloe barbadensis miller", "Succulents", 149, 55, 30, 7),
    ("Echeveria", "Echeveria elegans", "Succulents", 129, 50, 25, 4),
    ("Haworthia", "Haworthiopsis attenuata", "Succulents", 159, 60, 20, 3),
    ("Rose", "Rosa hybrida", "Flowering Plants", 179, 70, 35, 9),
    ("Hibiscus", "Hibiscus rosa-sinensis", "Flowering Plants", 229, 90, 25, 6),
    ("Bougainvillea", "Bougainvillea glabra", "Flowering Plants", 249, 100, 20, 5),
    ("Mogra (Jasmine)", "Jasminum sambac", "Flowering Plants", 199, 80, 25, 6),
    ("Marigold", "Tagetes erecta", "Flowering Plants", 49, 15, 80, 8),
    ("Adenium (Desert Rose)", "Adenium obesum", "Flowering Plants", 399, 170, 12, 3),
    ("Tulsi", "Ocimum tenuiflorum", "Herbs", 79, 25, 50, 9),
    ("Curry Leaf", "Murraya koenigii", "Herbs", 129, 45, 30, 5),
    ("Mint", "Mentha spicata", "Herbs", 69, 20, 40, 5),
    ("Ixora", "Ixora coccinea", "Outdoor Plants", 199, 80, 20, 4),
    ("Croton", "Codiaeum variegatum", "Outdoor Plants", 179, 70, 20, 4),
]

# name, category, price, cost, usual stock, popularity
PRODUCTS = [
    ("Plastic Pot 6 inch", "Pots", 49, 20, 60, 8),
    ("Plastic Pot 10 inch", "Pots", 99, 40, 40, 6),
    ("Terracotta Pot 8 inch", "Pots", 149, 60, 30, 6),
    ("Ceramic Pot 10 inch", "Pots", 449, 200, 15, 3),
    ("Hanging Basket", "Pots", 199, 80, 20, 3),
    ("Self-Watering Pot", "Pots", 299, 130, 20, 3),
    ("Potting Mix 5 kg", "Soil", 199, 90, 30, 6),
    ("Cocopeat Block 5 kg", "Soil", 149, 65, 30, 4),
    ("Vermicompost 2 kg", "Soil", 99, 40, 40, 6),
    ("Organic Plant Food 500 g", "Fertilizers", 149, 60, 30, 5),
    ("NPK 19:19:19 1 kg", "Fertilizers", 249, 120, 20, 3),
    ("Neem Oil Spray 250 ml", "Fertilizers", 199, 80, 20, 3),
    ("Hand Trowel", "Garden Tools", 149, 60, 15, 2),
    ("Pruning Shears", "Garden Tools", 349, 160, 12, 2),
    ("Watering Can 5 L", "Garden Tools", 249, 110, 12, 2),
]

# Kept low or empty every day so the Low stock / Out of stock badges always have something to show.
SHOWCASE_STOCK = {"Fiddle Leaf Fig": 0, "ZZ Plant": 3, "Ceramic Pot 10 inch": 2, "Neem Oil Spray 250 ml": 4}

# Customers (name, how often they come). Numbers are dummies in one block (+91 90000 001xx); don't message them.
CUSTOMERS = [
    ("Anita Sharma", 6), ("Rajesh Gupta", 5), ("Priya Verma", 4), ("Amit Kumar", 4), ("Sunita Yadav", 3),
    ("Vikram Singh", 3), ("Neha Agarwal", 3), ("Rohit Mehta", 2), ("Kavita Joshi", 2), ("Sanjay Malhotra", 2),
    ("Pooja Bansal", 2), ("Arjun Reddy", 2), ("Meena Iyer", 2), ("Deepak Chauhan", 1), ("Ritu Kapoor", 1),
    ("Manoj Tiwari", 1), ("Shalini Nair", 1), ("Karan Arora", 1),
] + [(n, 1) for n in (
    "Suresh Patel", "Geeta Rawat", "Harish Bhatia", "Nisha Saxena", "Prakash Jain", "Rekha Pandey", "Ashok Mishra",
    "Swati Kulkarni", "Gaurav Sethi", "Anjali Desai", "Mohit Khanna", "Seema Chawla", "Rahul Dubey", "Pallavi Rao",
    "Naveen Bhalla", "Kiran Thakur", "Vandana Goel", "Yogesh Sood", "Bhavna Mittal", "Tarun Ahuja", "Lata Menon",
    "Sachin Garg", "Dimple Bedi", "Alok Srivastava", "Komal Arya", "Rakesh Dhawan", "Sneha Kohli")]
CUSTOMER_PHONE = {name: f"+91900000{101 + i:04d}" for i, (name, _) in enumerate(CUSTOMERS)}

POTS = [p[0] for p in PRODUCTS if p[1] == "Pots"]
CARE = [p[0] for p in PRODUCTS if p[1] in ("Soil", "Fertilizers")]
SMALL = {"Marigold", "Tulsi", "Mint", "Aloe Vera", "Echeveria", "Haworthia", "Plastic Pot 6 inch"}
HOUR_WEIGHTS = {9: 1, 10: 3, 11: 3, 12: 2, 13: 1, 14: 1, 15: 1, 16: 2, 17: 3, 18: 3, 19: 2, 20: 1}


def day_plan(business_id: str, day: date) -> list[dict]:
    """The orders of one day: always the same for the same shop and day."""
    rng = random.Random(f"{business_id}:{day.isoformat()}")
    weekday = day.weekday()
    count = rng.randint(16, 22) if weekday == 6 else rng.randint(14, 19) if weekday == 5 else rng.randint(9, 14)
    sellable_plants = [p for p in PLANTS if p[0] not in SHOWCASE_STOCK]
    sellable_other = [p for p in PRODUCTS if p[0] not in SHOWCASE_STOCK]
    hours, weights = list(HOUR_WEIGHTS), list(HOUR_WEIGHTS.values())
    orders = []
    for _ in range(count):
        hour = rng.choices(hours, weights)[0]
        minute = rng.randint(0, 30 if hour == 20 else 59)
        lines: dict[str, int] = {}

        def add(name: str, qty: int):
            lines[name] = lines.get(name, 0) + qty

        n_items = rng.choices([1, 2, 3, 4], [40, 32, 18, 10])[0]
        for _ in range(n_items):
            if not lines or rng.random() < 0.75:
                p = rng.choices(sellable_plants, [x[6] for x in sellable_plants])[0][0]
                qty = rng.randint(2, 8) if p == "Marigold" else rng.randint(1, 3) if p in SMALL else rng.choices([1, 2, 3], [80, 15, 5])[0]
            else:
                p = rng.choices(sellable_other, [x[5] for x in sellable_other])[0][0]
                qty = rng.choices([1, 2], [80, 20])[0]
            add(p, qty)
        if rng.random() < 0.35:
            pot = rng.choice([x for x in POTS if x not in SHOWCASE_STOCK])
            add(pot, max(1, min(3, sum(lines.values()) // 2)))
        if rng.random() < 0.2:
            add(rng.choice([x for x in CARE if x not in SHOWCASE_STOCK]), 1)
        customer = rng.choices(CUSTOMERS, [w for _, w in CUSTOMERS])[0][0]
        orders.append({
            "minute": hour * 60 + minute,
            "lines": lines,
            "customer": customer,
            "method": rng.choices(["Cash", "UPI", "Card"], [40, 50, 10])[0],
            "discount": rng.random() < 0.15,
        })
    orders.sort(key=lambda o: o["minute"])
    for i, o in enumerate(orders):
        o["key"] = f"{day.isoformat()}#{i:02d}"
        o["at"] = datetime(day.year, day.month, day.day, o["minute"] // 60, o["minute"] % 60, tzinfo=IST)
    return orders


class Shop:
    def __init__(self, db, owner: dict):
        self.db = db
        self.owner = owner
        self.bid = owner["business_id"]
        self.by = {"id": owner["id"], "name": owner.get("name", "")}

    async def products(self) -> dict[str, dict]:
        docs = await self.db.products.find({"business_id": self.bid, "demo": True, "deleted_at": None},
                                           {"_id": 0}).to_list(500)
        return {p["name"]: p for p in docs}

    async def stock_entry(self, product: dict, change: float, new_stock: float, kind: str, reason: str, at: datetime):
        await self.db.stock_history.insert_one({
            "id": new_id(), "business_id": self.bid, "product_id": product["id"], "product_name": product["name"],
            "change": change, "new_stock": new_stock, "type": kind, "reason": reason, "by": self.by,
            "created_at": at.astimezone(timezone.utc).isoformat(), "demo": True})

    async def set_stock(self, product: dict, target: float, kind: str, reason: str, at: datetime):
        current = (await self.db.products.find_one({"id": product["id"]}, {"stock": 1}))["stock"]
        if current == target:
            return
        await self.db.products.update_one({"id": product["id"]}, {"$set": {"stock": target}})
        await self.stock_entry(product, target - current, target, kind, reason, at)

    async def restock(self, at: datetime):
        """A supplier delivery: anything running low goes back to its usual stock; showcase items stay as they are."""
        usual = {p[0]: p[5] for p in PLANTS} | {p[0]: p[4] for p in PRODUCTS}
        for name, p in (await self.products()).items():
            if name in SHOWCASE_STOCK:
                await self.set_stock(p, SHOWCASE_STOCK[name], "adjustment", "Stock count", at)
            elif p["stock"] < usual.get(name, 20) * 0.4:
                await self.set_stock(p, usual.get(name, 20), "purchase", "Supplier bill", at)

    async def create_order(self, plan: dict, products: dict[str, dict], cats: dict, gst_registered: bool) -> bool:
        db, at = self.db, plan["at"]
        lines = []
        for name, qty in plan["lines"].items():
            p = products.get(name)
            if not p:
                continue
            updated = await db.products.find_one_and_update(
                {"id": p["id"], "stock": {"$gte": qty}}, {"$inc": {"stock": -qty}}, projection={"_id": 0},
                return_document=ReturnDocument.AFTER)
            if not updated:
                continue  # sold out today: leave it off this order
            await self.stock_entry(updated, -qty, updated["stock"], "sale", "", at)
            cat = cats.get(p.get("category_id")) or {}
            lines.append({"product_id": p["id"], "name": p["name"], "quantity": qty, "price": p["selling_price"],
                          "gst_percent": cat.get("gst_percent", 0) if gst_registered else 0,
                          "cost_price": p.get("cost_price", 0)})
        if not lines:
            return False
        subtotal = sum(l["price"] * l["quantity"] for l in lines)
        discount = min(100, round(subtotal * 0.05 / 10) * 10) if plan["discount"] and subtotal >= 600 else 0
        totals = price_lines(lines, discount)

        customer_id, customer_name, phone = None, "Walk-in customer", ""
        if plan["customer"]:
            phone = CUSTOMER_PHONE[plan["customer"]]
            c = await db.customers.find_one({"business_id": self.bid, "phone": phone}, {"_id": 0})
            if c:
                customer_id, customer_name = c["id"], c["name"]
        created = at.astimezone(timezone.utc).isoformat()
        invoice = {
            "id": new_id(), "business_id": self.bid, "invoice_number": await _next_number(db, self.bid), "items": lines,
            **totals, "payment_method": plan["method"], "status": "Paid",
            "customer_id": customer_id, "customer_name": customer_name, "customer_phone": phone if customer_id else "",
            "created_by": self.by, "public_token": secrets.token_urlsafe(16), "date_key": date_key(at),
            "created_at": created, "demo": True, "demo_key": plan["key"],
        }
        await db.invoices.insert_one(invoice)
        if customer_id:
            await db.customers.update_one({"id": customer_id}, {
                "$inc": {"total_purchases": invoice["grand_total"], "purchase_count": 1},
                "$max": {"last_purchase_at": created}})
        return True

    async def orders_until(self, day: date, until: datetime) -> int:
        """Make the day's planned orders whose time has come and that don't exist yet."""
        done = set(await self.db.invoices.distinct("demo_key", {"business_id": self.bid, "demo": True,
                                                                 "date_key": day.isoformat()}))
        todo = [o for o in day_plan(self.bid, day) if o["at"] <= until and o["key"] not in done]
        if not todo:
            return 0
        products = await self.products()
        cats, gst_registered = await context(self.db, self.bid)
        made = 0
        for o in todo:
            made += await self.create_order(o, products, cats, gst_registered)
        return made


class NotAllowed(RuntimeError):
    """The demo tools refuse this shop or this server."""


async def check_new_shop_on_production(db, business_id: str):
    """On production, demo data goes only into a new, empty shop: one with products or bills of its own is a real
    shop (its bills are tax records the daily refresh would move), so it is refused there."""
    if not get_settings().is_production:
        return
    own = {"business_id": business_id, "demo": {"$ne": True}}
    if await db.products.find_one(own, {"_id": 1}) or await db.invoices.find_one(own, {"_id": 1}):
        raise NotAllowed("on production, demo data goes only into a new, empty shop: this shop has products or "
                         "bills of its own")


async def find_owner(db, phone: str) -> dict:
    owner = await db.users.find_one({"phone": normalize_in_phone(phone), "role": "owner"}, {"_id": 0})
    if not owner:
        raise SystemExit(f"No shop owner with phone {phone}")
    return owner


async def remove_demo(db, business_id: str):
    for coll in ("invoices", "stock_history", "products", "customers"):
        await db[coll].delete_many({"business_id": business_id, "demo": True})
    await db.demo_state.delete_many({"business_id": business_id})  # left by older versions of this script
    await rollups.rebuild(db, business_id)  # the shop's own bills stay in the totals, the demo ones leave


async def seed(db, phone: str, now: datetime | None = None, force: bool = False) -> dict:
    now = (now or now_ist()).astimezone(IST)
    shop = Shop(db, await find_owner(db, phone))
    bid = shop.bid
    await check_new_shop_on_production(db, bid)
    if await db.products.find_one({"business_id": bid, "demo": True}):
        if not force:
            return {"seeded": False, "reason": "already has demo data (use --force to start again)"}
        await remove_demo(db, bid)
    await db.businesses.update_one({"id": bid}, {"$set": {"demo": True}})

    start = now - timedelta(days=DAYS_BACK)
    start_day = start.date()
    gst = dict(SUGGESTED_CATEGORIES)
    cat_ids = {}
    for cat in sorted({p[2] for p in PLANTS} | {p[1] for p in PRODUCTS}):
        c = await _create_category(db, bid, cat, gst.get(cat, 0))
        cat_ids[cat] = c["id"]

    opening = datetime(start_day.year, start_day.month, start_day.day, 8, 30, tzinfo=IST)
    items = [(n, b, c, pr, co, st) for n, b, c, pr, co, st, _ in PLANTS] + [(n, "", c, pr, co, st) for n, c, pr, co, st, _ in PRODUCTS]
    for name, botanical, cat, price, cost, stock in items:
        library_id = await library.entry_for_new_product(db, name, cat, bid)
        doc = {
            "id": new_id(), "business_id": bid, "name": name, "botanical_name": botanical,
            "category_id": cat_ids[cat], "library_id": library_id,
            "photo_path": None, "selling_price": price, "cost_price": cost, "stock": stock, "min_stock": 5,
            "supplier": "Green Valley Growers" if cat.endswith("Plants") or cat in ("Succulents", "Herbs") else "Garden Mart",
            "notes": "", "deleted_at": None, "created_at": opening.astimezone(timezone.utc).isoformat(),
            "created_by": shop.owner["id"], "demo": True,
        }
        entry = await db.library.find_one({"id": library_id}, library.FIELDS) if library_id else None
        doc = await insert_product(db, doc, entry)  # the shop's next product code and the search keys
        await shop.stock_entry(doc, stock, stock, "initial", "Opening stock", opening)

    for name, _ in CUSTOMERS:
        await db.customers.insert_one({
            "id": new_id(), "business_id": bid, "name": name, "phone": CUSTOMER_PHONE[name], "email": "", "address": "",
            "notes": "", "total_purchases": 0.0, "purchase_count": 0,
            "created_at": opening.astimezone(timezone.utc).isoformat(), "demo": True,
            **customers.search_fields(name, CUSTOMER_PHONE[name])})

    made = 0
    day = start_day
    while day <= now.date():
        if day.weekday() == 0 or day == now.date():  # deliveries on Mondays, and before today's trade
            await shop.restock(datetime(day.year, day.month, day.day, 8, 45, tzinfo=IST))
        # Whole days, today included: the daily refresh then always shows a full "today".
        made += await shop.orders_until(day, datetime(day.year, day.month, day.day, 23, 59, tzinfo=IST))
        day += timedelta(days=1)
    await rollups.rebuild(db, bid)  # the bills above were written without rollups.apply()
    return {"seeded": True, "products": len(items), "customers": len(CUSTOMERS), "orders": made}


def _later(iso: str | None, days: int) -> str | None:
    return (datetime.fromisoformat(iso) + timedelta(days=days)).isoformat() if iso else iso


async def shift_dates(db, business_id: str, days: int) -> int:
    """Move every date in the shop `days` forward: bills, stock history, customers, products, supplier bills."""
    moved = 0
    times = ("created_at", "first_billed_at", "cancelled_at")  # the last two: corrected or cancelled bills
    async for inv in db.invoices.find({"business_id": business_id}, {"_id": 0, "id": 1, "date_key": 1, **{f: 1 for f in times}}):
        day = (date.fromisoformat(inv["date_key"]) + timedelta(days=days)).isoformat()
        await db.invoices.update_one({"id": inv["id"]}, {"$set": {"date_key": day, **{f: _later(inv[f], days) for f in times if inv.get(f)}}})
        moved += 1
    for coll, fields in (("stock_history", ("created_at",)), ("customers", ("created_at", "last_purchase_at")),
                         ("products", ("created_at", "updated_at")), ("purchases", ("created_at",))):
        async for doc in db[coll].find({"business_id": business_id}, {"_id": 0, "id": 1, **{f: 1 for f in fields}}):
            changes = {f: _later(doc[f], days) for f in fields if doc.get(f)}
            if changes:
                await db[coll].update_one({"id": doc["id"], "business_id": business_id}, {"$set": changes})
    return moved


async def refresh(db, phone: str, today: date | None = None) -> dict:
    """Daily: move all the shop's dates forward so its newest day of orders is today. Only on a shop marked demo
    (production included: there only a new, empty shop can have been seeded and marked)."""
    today = today or now_ist().date()
    shop = Shop(db, await find_owner(db, phone))
    if not (await db.businesses.find_one({"id": shop.bid}, {"_id": 0, "demo": 1}) or {}).get("demo"):
        raise NotAllowed("not a demo shop (the business is not marked demo): nothing moved")
    newest = await db.invoices.find({"business_id": shop.bid}, {"_id": 0, "date_key": 1}).sort("date_key", -1).limit(1).to_list(1)
    if not newest:
        return {"shifted_days": 0, "note": "no orders"}
    days = (today - date.fromisoformat(newest[0]["date_key"])).days
    if days <= 0:
        return {"shifted_days": 0}
    bills = await shift_dates(db, shop.bid, days)
    await rollups.rebuild(db, shop.bid)  # the daily totals follow their bills to the new days
    for name, p in (await shop.products()).items():  # keep the Low / Out of stock badges showing
        if name in SHOWCASE_STOCK:
            await shop.set_stock(p, SHOWCASE_STOCK[name], "adjustment", "Stock count", now_ist())
    return {"shifted_days": days, "bills": bills}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["seed", "refresh", "remove"])
    ap.add_argument("--phone", required=True, help="the demo shop owner's phone number")
    ap.add_argument("--force", action="store_true", help="seed: remove the old demo data and start again")
    args = ap.parse_args()

    async def run():
        db = get_db()
        if args.action == "seed":
            return await seed(db, args.phone, force=args.force)
        if args.action == "refresh":
            return await refresh(db, args.phone)
        await remove_demo(db, (await find_owner(db, args.phone))["business_id"])
        return {"removed": True}

    try:
        result = asyncio.run(run())
    except NotAllowed as e:
        print(datetime.now(IST).strftime("%Y-%m-%d %H:%M"), args.action, "refused:", e, file=sys.stderr, flush=True)
        return 3
    print(datetime.now(IST).strftime("%Y-%m-%d %H:%M"), args.action, result, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
