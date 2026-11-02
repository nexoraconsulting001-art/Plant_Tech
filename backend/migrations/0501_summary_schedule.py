"""Daily summary schedule: the index the worker's due-shops query uses, and a row for every shop with a phone.

`summary_schedule` (notify.py) has one row per shop whose owner gets the daily summary; the worker finds the
due ones with {due: {$lte: now}}. Shops that already have an owner's phone signed up get today's row here
(an existing row is left as it is), so the first run after the deploy sends today's summary as before.
"""
import notify


async def up(db):
    await db.summary_schedule.create_index("due")
    for business_id in await db.push_tokens.distinct("business_id"):
        if business_id:
            await notify.schedule(db, business_id, keep=True)
