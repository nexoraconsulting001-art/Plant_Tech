"""Report indexes: a shop's bills of a day newest first (Home, totals, reports), and products by stock (low stock)."""


async def up(db):
    # Home's recent bills, the totals and reports read from the bills, and the rollups rebuild: shop + day(s).
    await db.invoices.create_index([("business_id", 1), ("date_key", 1), ("created_at", -1)])
    # Home's low-stock and out-of-stock counts and the "sold below stock" card (routers/reports.py).
    await db.products.create_index([("business_id", 1), ("deleted_at", 1), ("stock", 1)])
