"""Daily sales rollups (daily_sales, daily_item_sales) worked out from every existing bill (rollups.rebuild).

Safe to repeat and to run while the old version is still billing: each shop-day is rebuilt in its own
transaction from the bills. Reports read the rollups only once REPORTS_FROM_ROLLUPS is on (see rollups.py).
"""
import rollups


async def up(db):
    await rollups.rebuild(db)
