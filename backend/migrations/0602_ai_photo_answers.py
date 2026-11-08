"""Plant photo answers remembered for a while (routers/ai.py, AI_PHOTO_CACHE_HOURS): gone once expires_at passes.

The same photo sent again (a retry after no answer, the same picture picked again) is answered from memory with the
same result and uses no scan. MongoDB's TTL monitor removes an answer about a minute after its expires_at; the
route also checks the time itself.
"""


async def up(db):
    await db.ai_photo_answers.create_index("expires_at", name="expires_at_ttl", expireAfterSeconds=0)
