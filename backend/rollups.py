"""Daily sales rollups (stub; WP-2 implements it).

The plan: `daily_sales` (one document per shop and day: orders, sales, by method, by user, by hour) and
`daily_item_sales`, kept in step with the bills by `$inc` inside the bill's transaction, so reports and the
daily summary read a few small documents instead of every bill.

    await rollups.apply(session, db, invoice, +1)   # in sales.py, inside the transaction that saves the bill
    await rollups.apply(session, db, invoice, -1)   # ... and the one that cancels it
    day = await rollups.read_day(db, business_id, "2026-11-02")   # None: no rollup, fall back to the bills

Until WP-2 lands, apply() does nothing and read_day() returns None, so callers keep their current behaviour.
"""


async def apply(session, db, invoice: dict, sign: int) -> None:
    """Add (sign=+1) or take away (sign=-1) one bill's totals. No-op for now."""
    return None


async def read_day(db, business_id: str, day: str) -> dict | None:
    """The shop's totals for one day ({orders, sales, by_method, ...}), or None when not available."""
    return None
