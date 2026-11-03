"""Daily sales rollups (daily_sales, daily_item_sales) worked out from every existing bill (rollups.rebuild).

Safe to repeat: each shop-day is rebuilt in its own transaction from the bills. It runs while the old version is
still billing, and that version never updates the rollups, so its bills from here to the swap (and any after a
rollback) are missing until deploy/deploy.sh rebuilds those days once the new version serves (`python rollups.py
rebuild --touched-since <day>`). Reports read the rollups only once REPORTS_FROM_ROLLUPS is on (see rollups.py).
"""
import rollups


async def up(db):
    await rollups.rebuild(db)
