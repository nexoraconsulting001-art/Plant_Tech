"""Saved orders by shop and time (routers/carts.py, GET /api/orders/cart/today): New order lists the orders saved
since midnight, newest first, so this index keeps that one indexed range read."""


async def up(db):
    await db.carts.create_index([("business_id", 1), ("saved_at", -1)], name="business_saved_at")
