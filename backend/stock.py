"""Stock changes shared by sales, supplier bills and the products screen: rounding and the stock history.

Moved from routers/products.py unchanged (products re-exports these names for older imports).
"""
import notify
from common import new_id, now_iso

STOCK_EPS = 1e-9  # "enough stock" allows for float noise in stock saved before settle_stock existed


async def settle_stock(db, product: dict) -> dict:
    """Keep stock on 3 decimals after a change ($inc). 0.7 kg less 0.4 is 0.29999999999999993 in floating point:
    unrounded, the last 0.3 kg could not be sold and selling it would leave a tiny stock below zero. Rounds only
    if nothing changed the stock since (another sale settles its own result). Returns the product as rounded."""
    raw = product.get("stock", 0)
    exact = round(raw, 3) or 0.0  # no -0.0
    if exact != raw:
        await db.products.update_one({"id": product["id"], "stock": raw}, {"$set": {"stock": exact}})
    return {**product, "stock": exact}


async def record_stock(db, user: dict, product: dict, change: float, new_stock: float, kind: str, reason: str = "",
                       bill: str = ""):
    """kind: initial, sale, adjustment, purchase, bill_cancel or bill_correct (`bill` = the bill's number for these two)."""
    await db.stock_history.insert_one({
        "id": new_id(), "business_id": user["business_id"], "product_id": product["id"],
        "product_name": product["name"], "change": change, "new_stock": new_stock, "type": kind, "reason": reason,
        **({"bill": bill} if bill else {}), "by": {"id": user["id"], "name": user.get("name", "")}, "created_at": now_iso()})
    await notify.stock_changed(db, user["business_id"], product, new_stock - change, new_stock)
