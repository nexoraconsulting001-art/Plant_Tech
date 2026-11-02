"""Telling the owner about stock: a product sold below zero, or down to its low-stock alert level.

Every stock change goes through `stock_changed` (from products.record_stock). It works out whether the
change crossed a line worth telling the owner about and hands that to `stock_alert`, which does nothing
yet: push notifications to the owner come in a later feature and plug in there.
"""
import logging

log = logging.getLogger(__name__)


async def stock_alert(db, business_id: str, product: dict, kind: str, stock: float):
    """kind: "below_zero" (sold more than was on hand) or "low_stock" (at or below min_stock). No-op for now."""
    return None


def crossed(before: float, after: float, min_stock: float) -> str | None:
    """Which line this change crossed going down, if any. Below zero wins over low stock."""
    if after < 0 <= before:
        return "below_zero"
    if after <= min_stock < before:
        return "low_stock"
    return None


async def stock_changed(db, business_id: str, product: dict, before: float, after: float):
    kind = crossed(before, after, product.get("min_stock", 0) or 0)
    if not kind:
        return None
    try:
        await stock_alert(db, business_id, product, kind, after)
    except Exception:  # an alert must never fail a sale or a stock change
        log.exception("stock alert failed for product %s", product.get("id"))
    return kind
