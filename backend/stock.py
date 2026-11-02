"""Stock changes shared by sales, supplier bills and the products screen: rounding and the stock history.

Bills, corrections, cancellations and supplier bills change stock inside one transaction (db.in_transaction):
they collect their history rows with `history_row`, write them with one insert_many in that transaction
(`write_history`) and tell the owner about low stock only after the commit (`notify_after`), so no alert goes
out for a sale that was rolled back. `record_stock` (one row, then the alert) stays for single changes made
outside a transaction (the products screen; routers/products.py re-exports these names for older imports).
"""
import notify
from common import new_id, now_iso

STOCK_EPS = 1e-9  # "enough stock" allows for float noise in stock saved before settle_stock existed


async def settle_stock(db, product: dict, session=None) -> dict:
    """Keep stock on 3 decimals after a change ($inc). 0.7 kg less 0.4 is 0.29999999999999993 in floating point:
    unrounded, the last 0.3 kg could not be sold and selling it would leave a tiny stock below zero. Rounds only
    if nothing changed the stock since (another sale settles its own result). Returns the product as rounded."""
    raw = product.get("stock", 0)
    exact = round(raw, 3) or 0.0  # no -0.0
    if exact != raw:
        q = {"id": product["id"], "stock": raw}
        if product.get("business_id"):
            q["business_id"] = product["business_id"]
        await db.products.update_one(q, {"$set": {"stock": exact}}, session=session)
    return {**product, "stock": exact}


def history_row(user: dict, product: dict, change: float, new_stock: float, kind: str, reason: str = "",
                bill: str = "") -> dict:
    """kind: initial, sale, adjustment, purchase, bill_cancel or bill_correct (`bill` = the bill's number for these two)."""
    return {"id": new_id(), "business_id": user["business_id"], "product_id": product["id"],
            "product_name": product["name"], "change": change, "new_stock": new_stock, "type": kind, "reason": reason,
            **({"bill": bill} if bill else {}), "by": {"id": user["id"], "name": user.get("name", "")},
            "created_at": now_iso()}


async def write_history(db, rows: list[dict], session=None):
    if rows:
        await db.stock_history.insert_many(rows, session=session)


async def notify_after(db, business_id: str, changes: list[tuple[dict, float, float]]):
    """After the commit: (product, stock before, stock after) for each product the change moved."""
    for product, before, after in changes:
        await notify.stock_changed(db, business_id, product, before, after)


async def record_stock(db, user: dict, product: dict, change: float, new_stock: float, kind: str, reason: str = "",
                       bill: str = ""):
    """One change outside a transaction: its history row, then the owner's alert if it crossed a line."""
    await db.stock_history.insert_one(history_row(user, product, change, new_stock, kind, reason, bill))
    await notify.stock_changed(db, user["business_id"], product, new_stock - change, new_stock)
