"""What each Claude call cost, and the app-wide monthly AI budget.

Every call (answered or failed) leaves one `ai_calls` document: kind (plant / invoice / names), the model that
answered (`resp.model`, which differs from the requested one after a server-side fallback), tokens including
cache writes and reads, the estimated cost in USD, latency, outcome, our request_id and Anthropic's, and the
shop and user of the request. They expire after AI_CALLS_KEEP_DAYS (migration 0601).

The month's total cost is also kept in one `ai_spend` document per month (_id "2026-11", India time), so the
budget check is one read by _id. AI_MONTHLY_BUDGET_USD (unset or 0 = no limit) stops new calls once the month's
cost reaches it; ai_client turns that into a clear "paused" message and the scan is given back. The Anthropic
workspace's own spend limit (decision D4) stays the hard stop; this one is ours, and reacts within one call.

Costs are estimates from list prices (PRICES, per million tokens; cache writes 1.25x and cache reads 0.1x the
input price). A model not in the list is priced like the most expensive one, so the budget errs on stopping early.
"""
import logging
import os
from datetime import datetime, timezone

from common import date_key, new_id
from db import get_db
from observability import request_context

log = logging.getLogger(__name__)

# USD per million tokens (input, output), checked against the Claude API reference dated 2026-10-06.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100k tokens; ours are far smaller
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10
_warned: set[str] = set()


def price(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens; the longest known prefix (dated ids), else the dearest."""
    for known in sorted(PRICES, key=len, reverse=True):
        if model == known or model.startswith(known + "-"):
            return PRICES[known]
    if model not in _warned:
        _warned.add(model)
        log.warning("no price for model %s; counting it at the highest known price", model)
    return max(PRICES.values())


def cost_usd(model: str, usage) -> float:
    inp, out = price(model)
    tokens = (usage.input_tokens * inp + (usage.cache_creation_input_tokens or 0) * inp * CACHE_WRITE
              + (usage.cache_read_input_tokens or 0) * inp * CACHE_READ + usage.output_tokens * out)
    return round(tokens / 1_000_000, 6)


def month() -> str:
    return date_key()[:7]


def monthly_budget() -> float:
    try:
        return max(0.0, float(os.environ.get("AI_MONTHLY_BUDGET_USD") or 0))
    except ValueError:
        log.error("AI_MONTHLY_BUDGET_USD is not a number; no AI budget is applied")
        return 0.0


async def over_budget(db=None) -> bool:
    """True when this month's AI cost has reached AI_MONTHLY_BUDGET_USD."""
    budget = monthly_budget()
    if not budget:
        return False
    db = db if db is not None else get_db()
    doc = await db.ai_spend.find_one({"_id": month()}, {"cost_usd": 1})
    return bool(doc) and doc.get("cost_usd", 0) >= budget


async def record(kind: str, requested_model: str, resp, outcome: str, latency_ms: float, images: int = 0,
                 error: str | None = None, db=None) -> dict:
    """Write the call's ai_calls document and add its cost to the month. Never raises: a failed write is logged."""
    ctx = request_context()
    usage = getattr(resp, "usage", None)
    model = getattr(resp, "model", None) or requested_model
    doc = {
        "id": new_id(), "at": datetime.now(timezone.utc), "kind": kind, "model": model,
        "requested_model": requested_model, "outcome": outcome, "stop_reason": getattr(resp, "stop_reason", None),
        "input_tokens": usage.input_tokens if usage else 0, "output_tokens": usage.output_tokens if usage else 0,
        "cache_creation_input_tokens": (usage.cache_creation_input_tokens or 0) if usage else 0,
        "cache_read_input_tokens": (usage.cache_read_input_tokens or 0) if usage else 0,
        "cost_usd": cost_usd(model, usage) if usage else 0.0, "latency_ms": round(latency_ms, 1), "images": images,
        "request_id": ctx.get("request_id"), "anthropic_request_id": getattr(resp, "_request_id", None),
        "business_id": ctx.get("business_id"), "user_id": ctx.get("user_id"), "error": error,
    }
    log.info("claude call kind=%s model=%s outcome=%s in=%s out=%s cache_read=%s cache_write=%s cost=%s ms=%s",
             kind, model, outcome, doc["input_tokens"], doc["output_tokens"], doc["cache_read_input_tokens"],
             doc["cache_creation_input_tokens"], doc["cost_usd"], doc["latency_ms"])
    try:
        db = db if db is not None else get_db()
        await db.ai_calls.insert_one(dict(doc))
        if doc["cost_usd"]:
            await db.ai_spend.update_one({"_id": month()}, {"$inc": {"cost_usd": doc["cost_usd"], "calls": 1},
                                                            "$set": {"updated_at": doc["at"]}}, upsert=True)
    except Exception:
        log.exception("could not record a Claude call")
    return doc
