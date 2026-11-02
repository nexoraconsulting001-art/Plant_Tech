"""Index on purchases {business_id, supplier_id, created_at: -1}: a supplier's bills newest first, and the bill
count, total and last bill of each supplier (routers/suppliers.py `_stats`, an aggregation)."""


async def up(db):
    await db.purchases.create_index([("business_id", 1), ("supplier_id", 1), ("created_at", -1)])
