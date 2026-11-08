"""Orders (bills): create with stock deduction and tax-inclusive GST, list, share link. Totals and reports: reports.py.

Workers see only the orders they made; owners (and workers with the reports permission) see all.

Selling more than the stock on hand: only the owner may, and only when the app says so (`allow_short`, sent
after the owner saw "Stock will go to -2" and went ahead). The stock then goes below zero and each such line
keeps `short_qty`, how much of it was not in stock. Everyone else, and app versions that don't send
`allow_short`, get the usual 400 out_of_stock.

UPI bills keep the UPI ID the money was paid to (`upi_id`): the one the app showed in the QR when it is one of the
shop's, else the shop's default (also for app versions that don't send it).

Cancelled bills (see "Correcting or cancelling a bill" below) stay in the Orders list and the customer's bills,
marked, but count nowhere: not in sales, reports, the employees report or the customer's totals.

One bill, one transaction: making, cancelling or correcting a bill changes the stock, the stock history, the
customer (new, and its totals), the bill number, the bill, the saved order and the daily rollups together
(db.in_transaction), or nothing at all. MongoDB may run the transaction again after a write conflict (two phones
selling the last plant), so nothing in it may act outside the database; the owner's stock alerts go out after
the commit. Everything that can be checked first (products, customer, quantities) is checked before it starts.

Retries: the app may send an `Idempotency-Key` header (any unique string, e.g. a UUID per bill attempt) with
POST /invoices and /correct. The key is the first thing the bill's transaction saves; the same key again returns the
bill it made (header `Idempotent-Replayed: true`) instead of a second bill, also when the two arrive at once. The
same key with a changed order is refused (409 idempotency_mismatch, naming the bill that was made). Without the
header (app 1.1.0) nothing changes.

Bill numbers: INV-0001 onwards, one series per shop (decision D7 may change it to one per financial year). The
counter moves inside the bill's transaction, so a failed bill takes its number back and the series has no gaps;
a unique index on {business_id, invoice_number} (migration 0101) makes a duplicate impossible.
"""
import base64
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import ai_client
import auth
import gst
import idempotency
import invoice_html
import rollups
import storage_client
from common import date_key, money, new_id, normalize_in_phone, now_iso
from db import get_db, in_transaction
from errors import AppError
from fast_json import respond
from routers import carts, customers
from routers.business import shop_upis
from routers.products import context
from settings import get_settings
from stock import STOCK_EPS, history_row, notify_after, settle_stock, write_history
from units import check_quantity, count, unit_of

router = APIRouter(prefix="/api", tags=["sales"])
public_router = APIRouter(tags=["public"])

# Input limits: far above any real nursery bill, low enough that totals stay exact in floating point.
MAX_LINES = 200
MAX_QTY = carts.MAX_QTY
MAX_PRICE = carts.MAX_PRICE  # one unit
MAX_DISCOUNT = MAX_PRICE * 100


class LineIn(BaseModel):
    product_id: str | None = Field(None, max_length=64)  # None = "bill without stock" (a loose item)
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=MAX_QTY, allow_inf_nan=False)
    price: float = Field(ge=0, le=MAX_PRICE, allow_inf_nan=False)
    unit: str | None = Field(None, max_length=12)  # loose items only; a product's own unit is used for the rest


class InvoiceIn(BaseModel):
    items: list[LineIn] = Field(min_length=1, max_length=MAX_LINES)
    customer_id: str | None = Field(None, max_length=64)  # a saved customer; or give the phone (+ name for a new one)
    customer_name: str = Field("", max_length=80)
    customer_phone: str = Field("", max_length=20)
    discount: float = Field(0, ge=0, le=MAX_DISCOUNT, allow_inf_nan=False)
    payment_method: Literal["Cash", "UPI", "Card"] = "Cash"
    gst_removed: bool = False  # owner only: same total, no GST on this bill (BILL_WITHOUT_GST, decision D6)
    allow_short: bool = False  # owner only: bill even if stock goes below zero (ignored for employees)
    upi_id: str | None = Field(None, max_length=80)  # UPI only: the shop's UPI ID shown in the QR


IdempotencyKey = idempotency.IdempotencyKey  # optional header (see idempotency.py); 1.1.0 never sends it
KEY_KEEP_DAYS = idempotency.KEEP_DAYS  # how long a key is remembered (TTL index on idempotency_keys.created_at, 0103)


def price_lines(lines: list[dict], discount: float, *, inter_state: bool = False) -> dict:
    """Prices include GST. gst.split spreads any discount over the lines and splits each line's value into taxable
    value + tax (CGST + SGST, or IGST to another state; Decimal, half up), and the bill saves exactly that: each
    line's `net` (after the discount) and `tax`, plus `cgst`/`sgst`/`igst` on taxed lines, and the per-rate GST
    breakup as their sums. The printed bill, the app, the exports and GSTR-1 read these (gst.saved_split)."""
    subtotal = sum(l["price"] * l["quantity"] for l in lines)
    discount = min(discount, subtotal)
    grand_total = money(subtotal - discount)
    split = gst.split(lines, discount, total=grand_total, inter_state=inter_state)
    for l, t in zip(lines, split.lines):
        l["amount"] = money(l["price"] * l["quantity"])
        l["net"], l["tax"] = float(t.value), float(t.tax)
        if t.taxed:
            l["cgst"], l["sgst"], l["igst"] = float(t.cgst), float(t.sgst), float(t.igst)
    breakup = [{"rate": gst.number(r["rate"]), "taxable": float(r["taxable"]), "tax": float(r["tax"])}
               for r in split.by_rate if r["rate"] > 0]
    return {"subtotal": money(subtotal), "discount": money(discount), "grand_total": grand_total,
            "gst_breakup": breakup, "gst_total": float(split.tax), "inter_state": inter_state}


def _sees_all(user: dict) -> bool:
    return auth.can(user, "owner") or auth.can(user, "reports")


def _scope(user: dict) -> dict:
    q = {"business_id": user["business_id"]}
    if not _sees_all(user):
        q["created_by.id"] = user["id"]
    return q


LIVE = {"status": {"$ne": "Cancelled"}}  # what sales, reports and totals count: every bill except cancelled ones


def _summary(inv: dict) -> dict:
    return {k: inv.get(k) for k in ("id", "invoice_number", "customer_name", "customer_phone", "grand_total",
                                    "payment_method", "created_at", "created_by", "replaced_by", "replaces")} | {
        "status": inv.get("status") or "Paid", "items_count": sum(count(i) for i in inv.get("items", []))}


def _counter_id(business_id: str) -> str:
    """The counter a shop's bill numbers come from. A series per financial year (decision D7) changes only this
    key (e.g. invoice:<shop>:2627) and the format in _next_number (at most 16 characters for GST)."""
    return f"invoice:{business_id}"


async def _next_number(db, business_id: str, session=None) -> str:
    """The shop's next bill number. Call it inside the bill's transaction (session): see the module notes."""
    doc = await db.counters.find_one_and_update({"_id": _counter_id(business_id)}, {"$inc": {"seq": 1}},
                                                upsert=True, return_document=ReturnDocument.AFTER, session=session)
    return f"INV-{doc['seq']:04d}"


@dataclass
class _Bill:
    """Everything a new bill needs, checked before anything is changed (stock, customers, bill numbers)."""
    cats: dict
    gst_registered: bool
    composition: bool  # a composition shop collects no tax: every line is saved at 0% (gst.py)
    gst_removed: bool
    allow_short: bool
    products: dict
    units: list
    paid_to: str
    customer: dict | None
    new_customer: dict | None


async def _prepare(db, user: dict, body: InvoiceIn) -> _Bill:
    bid = user["business_id"]
    cats, gst_registered = await context(db, bid)
    ids = list({l.product_id for l in body.items if l.product_id})
    products = {p["id"]: p for p in await db.products.find(
        {"business_id": bid, "id": {"$in": ids}, "deleted_at": None}, {"_id": 0}).to_list(len(ids) or 1)}
    if missing := [i for i in ids if i not in products]:
        if len(missing) == 1:
            raise AppError(400, "1 item in this order is no longer in your products.", "product_missing")
        raise AppError(400, "{{n}} items in this order are no longer in your products.", "product_missing", n=len(missing))
    units = [unit_of(products[l.product_id].get("unit") if l.product_id else l.unit) for l in body.items]
    for l, unit in zip(body.items, units):
        check_quantity(unit, l.quantity, l.name.strip())

    paid_to = await _paid_to(db, bid, body.upi_id) if body.payment_method == "UPI" else ""

    # Every bill has a customer (name + phone): a saved one, or a new one made from the phone and name.
    customer, new_customer = None, None
    if body.customer_id:
        customer = await db.customers.find_one({"id": body.customer_id, "business_id": bid}, {"_id": 0})
        if not customer:
            raise HTTPException(400, "Customer not found.")
    else:
        phone = normalize_in_phone(body.customer_phone)
        if not phone:
            raise HTTPException(400, "Please choose a customer for this order.")
        customer = await db.customers.find_one({"business_id": bid, "phone": phone}, {"_id": 0})
        if not customer:
            if not body.customer_name.strip():
                raise HTTPException(400, "Please enter the customer's name.")
            new_customer = {"id": new_id(), "business_id": bid, "name": body.customer_name.strip(), "phone": phone,
                            "email": "", "address": "", "notes": "", "total_purchases": 0.0, "purchase_count": 0,
                            "created_at": now_iso(), **customers.search_fields(body.customer_name.strip(), phone)}
    # "Bill without GST" stays as it is until the owner decides (D6); BILL_WITHOUT_GST=0 turns it off for everyone.
    gst_removed = body.gst_removed and get_settings().bill_without_gst and gst_registered and auth.can(user, "owner")
    biz = await db.businesses.find_one({"id": bid}, {"_id": 0, "gst_registered": 1, "gst_scheme": 1}) or {}
    return _Bill(cats=cats, gst_registered=gst_registered, composition=gst.scheme(biz) == "composition",
                 gst_removed=gst_removed,
                 allow_short=body.allow_short and auth.can(user, "owner"),  # the role decides, never the app alone
                 products=products, units=units, paid_to=paid_to, customer=customer, new_customer=new_customer)


def _quantities(lines: list) -> dict[str, float]:
    """How much of each product these bill lines take (loose lines take none). Lines from the app or a saved bill."""
    out: dict[str, float] = {}
    for l in lines:
        pid, qty = (l.product_id, l.quantity) if isinstance(l, LineIn) else (l.get("product_id"), l["quantity"])
        if pid:
            out[pid] = round(out.get(pid, 0) + qty, 3)
    return out


class StockMove:
    """The stock a bill takes, or a cancelled bill gives back, or both at once for a correction (then only the
    difference moves), inside the bill's transaction. take() raises out_of_stock before changing anything when a
    product is short (so a laptop without transactions also stays as it was); each $inc is still conditional, so
    two phones selling the last plant at once cannot both get it. rows() and changes() are the stock history and
    the owner's alerts for what moved."""

    def __init__(self, db, user: dict, session=None):
        self.db, self.user, self.session = db, user, session
        self.done: list[tuple[dict, float]] = []  # (the product before, how much was taken; negative = given back)

    def _short(self, pid: str, stock: float, back: float, names: dict | None) -> AppError:
        return AppError(400, "Only {{n}} {{name}} left in stock.", "out_of_stock",
                        n=f"{round(stock + back, 3):g}", name=(names or {}).get(pid, ""))

    @staticmethod
    def _deltas(wanted: dict[str, float], give_back: dict[str, float]) -> dict[str, float]:
        return {pid: round(wanted.get(pid, 0) - give_back.get(pid, 0), 3)
                for pid in [*wanted, *(p for p in give_back if p not in wanted)]}

    async def check(self, wanted: dict[str, float], allow_short: bool, give_back: dict[str, float] | None = None,
                    names: dict[str, str] | None = None):
        """Raises out_of_stock if a product has less than this move takes from it. Changes nothing."""
        give_back = give_back or {}
        deltas = self._deltas(wanted, give_back)
        if need := [pid for pid, d in deltas.items() if d > 0 and not allow_short]:
            have = {p["id"]: p.get("stock", 0) for p in await self.db.products.find(
                {"business_id": self.user["business_id"], "id": {"$in": need}}, {"_id": 0, "id": 1, "stock": 1},
                session=self.session).to_list(len(need))}
            for pid in need:
                if have.get(pid, 0) < deltas[pid] - STOCK_EPS:
                    raise self._short(pid, have.get(pid, 0), give_back.get(pid, 0), names)

    async def take(self, wanted: dict[str, float], allow_short: bool, give_back: dict[str, float] | None = None,
                   names: dict[str, str] | None = None) -> dict[str, float]:
        """Returns product -> how much of its new quantity was not in stock (counting what is given back first)."""
        give_back = give_back or {}
        bid = self.user["business_id"]
        deltas = self._deltas(wanted, give_back)
        pids = list(deltas)
        await self.check(wanted, allow_short, give_back, names)
        short: dict[str, float] = {}
        for pid in pids:
            qty, back, delta = wanted.get(pid, 0), give_back.get(pid, 0), deltas[pid]
            q = {"id": pid, "business_id": bid}
            if delta > 0 and not allow_short:
                q["stock"] = {"$gte": delta - STOCK_EPS}
            # BEFORE: the filter is checked on the stock before the sale (mongomock re-checks it on AFTER)
            found = await self.db.products.find_one_and_update(q, {"$inc": {"stock": -delta}}, projection={"_id": 0},
                                                               return_document=ReturnDocument.BEFORE,
                                                               session=self.session)
            if found is None:
                if not qty:
                    continue  # given back to a product that no longer exists: nothing to return it to
                have = await self.db.products.find_one({"id": pid, "business_id": bid}, {"stock": 1},
                                                       session=self.session) or {}
                raise self._short(pid, have.get("stock", 0), back, names)
            self.done.append((found, delta))
            await settle_stock(self.db, {"id": pid, "business_id": bid, "stock": found.get("stock", 0) - delta},
                               session=self.session)  # what $inc left
            before = found.get("stock", 0) + back  # as if this bill's old quantity were back on the shelf first
            if qty and (missing_qty := round(qty - min(max(before, 0), qty), 3)):
                short[pid] = missing_qty
        return short

    def _moved(self):
        for found, delta in self.done:
            if delta:
                yield found, delta, round(found.get("stock", 0) - delta, 3) or 0.0

    def rows(self, kind: str, reason: str = "", bill: str = "") -> list[dict]:
        return [history_row(self.user, {**found, "stock": after}, -delta, after, kind, reason, bill=bill)
                for found, delta, after in self._moved()]

    def changes(self) -> list[tuple[dict, float, float]]:
        return [({**found, "stock": after}, found.get("stock", 0), after) for found, delta, after in self._moved()]


def _lines(body: InvoiceIn, bill: _Bill, short: dict[str, float]) -> list[dict]:
    short = dict(short)
    lines = []
    for l, unit in zip(body.items, bill.units):
        p = bill.products.get(l.product_id) if l.product_id else None
        cat = bill.cats.get(p.get("category_id")) if p else None
        line = {"product_id": l.product_id, "name": l.name.strip(), "quantity": l.quantity, "unit": unit, "price": l.price,
                "gst_percent": (cat or {}).get("gst_percent", 0) if bill.gst_registered and not bill.gst_removed
                and not bill.composition else 0,
                "cost_price": (p or {}).get("cost_price", 0)}
        if p:  # HSN and the exempt flag as on the day, like the rate (gst.py; printed and in GSTR-1)
            line["hsn"] = gst.line_hsn(p, cat)
            line["exempt"] = bool((cat or {}).get("tax_exempt")) and not line["gst_percent"]
        if p and money(l.price) != money(p.get("selling_price", 0)):
            line["list_price"] = p.get("selling_price", 0)  # sold at a different price than the shop price
        lines.append(line)
    for line in reversed(lines):  # the short amount goes on the product's last lines (usually its only one)
        if left := short.get(line["product_id"] or ""):
            line["short_qty"] = min(left, line["quantity"])
            short[line["product_id"]] = round(left - line["short_qty"], 3)
    return lines


async def _customer_for(db, session, bill: _Bill) -> dict:
    """The bill's customer. A new one is made with an upsert on the shop's phone (unique, migration 0102): if
    another phone made the same customer a moment ago, this bill goes to that one instead of a duplicate."""
    if not bill.new_customer:
        return bill.customer
    new = bill.new_customer
    return await db.customers.find_one_and_update(
        {"business_id": new["business_id"], "phone": new["phone"]},
        {"$setOnInsert": {k: v for k, v in new.items() if k not in ("business_id", "phone")}},
        upsert=True, projection={"_id": 0}, return_document=ReturnDocument.AFTER, session=session)


async def _save(db, session, user: dict, body: InvoiceIn, bill: _Bill, short: dict[str, float], invoice_id: str,
                **extra) -> dict:
    """Write the bill (and its new customer, if any) and add it to the customer's totals."""
    lines = _lines(body, bill, short)
    totals = price_lines(lines, body.discount)  # same state: bills carry no place of supply yet (gst.is_inter_state)
    customer = await _customer_for(db, session, bill)
    customer_id, customer_name, phone = customer["id"], customer["name"], customer.get("phone", "")
    bid = user["business_id"]
    invoice = {
        "id": invoice_id, "business_id": bid, "invoice_number": await _next_number(db, bid, session), "items": lines,
        **totals, "payment_method": body.payment_method, "status": "Paid", "gst_removed": bill.gst_removed,
        "customer_id": customer_id, "customer_name": customer_name, "customer_phone": phone or "",
        "customer_address": customer.get("address", ""),  # as it was on the day: editing the customer later leaves bills as they were
        **({"upi_id": bill.paid_to} if bill.paid_to else {}),
        "created_by": {"id": user["id"], "name": user.get("name", "")},
        "public_token": secrets.token_urlsafe(16), "date_key": date_key(), "created_at": now_iso(), **extra,
    }
    await db.invoices.insert_one(invoice, session=session)
    invoice.pop("_id", None)
    if customer_id:
        await db.customers.update_one({"id": customer_id, "business_id": bid},
                                      {"$inc": {"total_purchases": invoice["grand_total"], "purchase_count": 1},
                                       "$set": {"last_purchase_at": invoice["created_at"]}}, session=session)
    return invoice


# ---- Idempotency-Key (see the module notes and idempotency.py)

def _fingerprint(body: InvoiceIn) -> str:
    """What the bill is: its lines, customer, discount, payment method and GST choice. Not allow_short or upi_id:
    "Bill anyway" resends the same order with allow_short, and that must still find the bill."""
    return idempotency.fingerprint(body, {"items", "customer_id", "customer_name", "customer_phone", "discount",
                                          "payment_method", "gst_removed"})


def _keyed(db, user: dict, key: str | None, route: str, invoice_id: str, fingerprint: str, work):
    """work(session) behind the Idempotency-Key, which is the first write of the bill's transaction: a second
    request with the same key while the first is still running write-conflicts on it and is retried, and once the
    first has committed it gets a duplicate key and the first one's bill (_replayed), never out_of_stock or
    bill_cancelled from what the first one changed."""
    return idempotency.keyed(db, user, key, route, invoice_id, fingerprint, work, id_field="invoice_id")


async def _replayed(db, user: dict, key: str | None, route: str, response: Response,
                    fingerprint: str | None = None) -> dict | None:
    """The bill an earlier request with this key made, or None (a new key, or no key). The same key with a
    different order (changed after a try that got no answer) is a 409 `idempotency_mismatch` naming the bill that
    was made, so the app can show it instead of taking it for the changed order."""
    k = await idempotency.previous(db, user, key, route)
    if not k:
        return None
    inv = await db.invoices.find_one({"business_id": user["business_id"], "id": k["invoice_id"]}, {"_id": 0})
    if not inv:  # the bill was removed since (demo data): the key stays used
        raise idempotency.conflict()
    if idempotency.changed(k, fingerprint):
        raise AppError(409, "This order was already billed as {{number}} before your last changes. Those changes "
                            "are not on that bill.", "idempotency_mismatch", number=inv["invoice_number"],
                       invoice_id=inv["id"])
    response.headers["Idempotent-Replayed"] = "true"
    return _shown(inv)


_is_key_clash = idempotency.is_key_clash


@router.post("/invoices")
async def create_invoice(body: InvoiceIn, response: Response, user: dict = Depends(auth.require("sell")),
                         idempotency_key: str | None = IdempotencyKey):
    db = get_db()
    fingerprint = _fingerprint(body)
    if done := await _replayed(db, user, idempotency_key, "create", response, fingerprint):
        return done
    bill = await _prepare(db, user, body)
    names = {pid: p["name"] for pid, p in bill.products.items()}
    invoice_id = new_id()

    async def sell(session):
        move = StockMove(db, user, session)
        short = await move.take(_quantities(body.items), bill.allow_short, names=names)
        invoice = await _save(db, session, user, body, bill, short, invoice_id)
        await write_history(db, move.rows("sale"), session)
        await carts.clear(db, user["business_id"], invoice["customer_id"], session)  # billed: this customer's saved order is done
        await rollups.apply(session, db, invoice, +1)
        return invoice, move.changes()

    try:
        invoice, changes = await in_transaction(
            _keyed(db, user, idempotency_key, "create", invoice_id, fingerprint, sell), db)
    except DuplicateKeyError as e:
        if _is_key_clash(e) and (done := await _replayed(db, user, idempotency_key, "create", response, fingerprint)):
            return done
        raise
    await notify_after(db, user["business_id"], changes)
    return _shown(invoice)  # not through fast_json: one per bill, ~0.1 ms of encoding next to its transaction


# ---- Correcting or cancelling a bill (the owner only) ----
# A bill is never changed in place (GST). Cancelling marks it Cancelled, gives its stock back and takes it out of
# the customer's totals and every report. Correcting does that and makes the new bill, with a new number, in one
# call; the two are linked both ways (replaced_by / replaces). Both are allowed for CORRECT_DAYS after the bill was
# first made: a replacement keeps the first bill's time (first_billed_at), so correcting again never adds time.
# It also keeps the day of the sale (date_key): a bill made on Monday and corrected on Thursday still counts in
# Monday's sales, reports and summary; its created_at is when the corrected bill was made.
CORRECT_DAYS = 7


class CancelIn(BaseModel):
    reason: str = Field("", max_length=200)


class CorrectIn(InvoiceIn):
    reason: str = Field("", max_length=200)  # optional: why the bill was corrected


def _first_billed(inv: dict) -> str:
    return inv.get("first_billed_at") or inv["created_at"]


def _open_until(inv: dict) -> datetime:
    return datetime.fromisoformat(_first_billed(inv)) + timedelta(days=CORRECT_DAYS)


async def _open_bill(user: dict, invoice_id: str) -> dict:
    inv = await _get_invoice(user, invoice_id)
    if inv.get("status") == "Cancelled":
        raise AppError(400, "This bill is already cancelled.", "bill_cancelled")
    if datetime.now(timezone.utc) > _open_until(inv):
        raise AppError(400, "A bill can be corrected or cancelled only within {{n}} days.", "bill_locked", n=CORRECT_DAYS)
    return inv


async def _mark_cancelled(db, session, inv: dict, user: dict, reason: str):
    """Claims the bill: only one cancel or correction wins, even when two phones try at once."""
    r = await db.invoices.update_one({"id": inv["id"], "business_id": inv["business_id"], **LIVE}, {"$set": {
        "status": "Cancelled", "cancelled_at": now_iso(), "cancelled_by": {"id": user["id"], "name": user.get("name", "")},
        "cancel_reason": reason}}, session=session)
    if not r.modified_count:
        raise AppError(400, "This bill is already cancelled.", "bill_cancelled")


async def _take_off_customer(db, session, inv: dict):
    if inv.get("customer_id"):
        await db.customers.update_one({"id": inv["customer_id"], "business_id": inv["business_id"]},
                                      {"$inc": {"total_purchases": -inv["grand_total"], "purchase_count": -1}},
                                      session=session)


@router.post("/invoices/{invoice_id}/cancel")
async def cancel_invoice(invoice_id: str, body: CancelIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(400, "Please give a reason for cancelling this bill.")
    inv = await _open_bill(owner, invoice_id)
    no = inv["invoice_number"]

    async def cancel(session):
        await _mark_cancelled(db, session, inv, owner, reason)
        move = StockMove(db, owner, session)
        await move.take({}, True, give_back=_quantities(inv.get("items", [])))
        await write_history(db, move.rows("bill_cancel", f"Bill {no} cancelled", bill=no), session)
        await _take_off_customer(db, session, inv)
        await rollups.apply(session, db, inv, -1)
        return await db.invoices.find_one({"id": inv["id"], "business_id": inv["business_id"]}, {"_id": 0},
                                          session=session), move.changes()

    cancelled, changes = await in_transaction(cancel, db)
    await notify_after(db, owner["business_id"], changes)
    return _shown(cancelled)


@router.post("/invoices/{invoice_id}/correct")
async def correct_invoice(invoice_id: str, body: CorrectIn, response: Response,
                          owner: dict = Depends(auth.require("owner")), idempotency_key: str | None = IdempotencyKey):
    """The corrected bill in full (like POST /invoices). The old bill is cancelled and the new one made with a new
    number; only the difference in each product's stock moves. If anything fails, everything stays as it was.
    The new bill keeps who made the first one (`created_by`); `corrected_by` is the owner."""
    db = get_db()
    route = f"correct:{invoice_id}"
    fingerprint = _fingerprint(body)
    if done := await _replayed(db, owner, idempotency_key, route, response, fingerprint):
        return done
    try:
        old = await _open_bill(owner, invoice_id)
    except AppError:  # a try with this key may have corrected it a moment ago: that answer, not bill_cancelled
        if done := await _replayed(db, owner, idempotency_key, route, response, fingerprint):
            return done
        raise
    bill = await _prepare(db, owner, body)
    names = {pid: p["name"] for pid, p in bill.products.items()}
    me = {"id": owner["id"], "name": owner.get("name", "")}
    invoice_id_new = new_id()

    async def correct(session):
        move = StockMove(db, owner, session)
        wanted, back = _quantities(body.items), _quantities(old.get("items", []))
        await move.check(wanted, bill.allow_short, back, names)  # before the old bill is touched (laptops: no transaction)
        await _mark_cancelled(db, session, old, owner, body.reason.strip())
        short = await move.take(wanted, bill.allow_short, give_back=back, names=names)
        invoice = await _save(db, session, owner, body, bill, short, invoice_id_new,
                              replaces={"id": old["id"], "invoice_number": old["invoice_number"]},
                              first_billed_at=_first_billed(old), date_key=old.get("date_key") or date_key(),
                              corrected_by=me, created_by=old.get("created_by") or me)
        await db.invoices.update_one({"id": old["id"], "business_id": old["business_id"]}, {"$set": {
            "replaced_by": {"id": invoice["id"], "invoice_number": invoice["invoice_number"]}}}, session=session)
        await write_history(db, move.rows("bill_correct", f"Bill {old['invoice_number']} corrected to "
                                                          f"{invoice['invoice_number']}", bill=invoice["invoice_number"]),
                            session)
        await _take_off_customer(db, session, old)
        await rollups.apply(session, db, old, -1)
        await rollups.apply(session, db, invoice, +1)
        return invoice, move.changes()

    try:
        invoice, changes = await in_transaction(
            _keyed(db, owner, idempotency_key, route, invoice_id_new, fingerprint, correct), db)
    except DuplicateKeyError as e:
        if _is_key_clash(e) and (done := await _replayed(db, owner, idempotency_key, route, response, fingerprint)):
            return done
        raise
    await notify_after(db, owner["business_id"], changes)
    return _shown(invoice)


async def _paid_to(db, bid: str, sent: str | None) -> str:
    """The shop's UPI ID this bill was paid to: the one the app sent if it is the shop's, else the default."""
    upis = shop_upis(await db.businesses.find_one({"id": bid}, {"_id": 0, "upi_id": 1, "upi_ids": 1, "default_upi": 1}) or {})
    sent = (sent or "").strip().lower()
    return next((e["upi_id"] for e in upis["upi_ids"] if e["upi_id"].lower() == sent), upis["upi_id"])


def _shown(inv: dict) -> dict:
    """A bill as the app gets it: without the purchase cost of each line (that is only for the profit report).
    `correct_until`: until when the owner may correct or cancel it."""
    return {**inv, "items": [{k: v for k, v in it.items() if k != "cost_price"} for it in inv.get("items", [])],
            "correct_until": _open_until(inv).isoformat()}


DATE = r"\d{4}-\d{2}-\d{2}"


def _filters(user: dict, day: str, date_from: str, date_to: str, method: str, made_by: str, search: str) -> dict:
    """Orders filter shared by the list and its totals. Workers only ever see their own bills."""
    q = _scope(user)
    if day:
        q["date_key"] = day
    elif date_from or date_to:
        q["date_key"] = {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    if method:
        q["payment_method"] = method
    if made_by and _sees_all(user):
        q["created_by.id"] = made_by
    if search.strip():
        text = re.escape(search.strip())
        digits = re.sub(r"\D", "", search)
        ors = [{f: {"$regex": text, "$options": "i"}} for f in ("invoice_number", "customer_name")]
        if len(digits) >= 3:
            ors.append({"customer_phone": {"$regex": digits[-10:]}})
        q["$or"] = ors
    return q


def date_q():  # a fresh Query per parameter: FastAPI must not share one between parameters
    return Query("", pattern=f"^({DATE})?$")


def method_q():
    return Query("", pattern="^(Cash|UPI|Card)?$")


@router.get("/invoices")
async def list_invoices(day: str = date_q(), date_from: str = date_q(), date_to: str = date_q(),
                        method: str = method_q(), made_by: str = Query("", max_length=64),
                        search: str = Query("", max_length=60), before: str = Query("", max_length=40),
                        limit: int = Query(50, ge=1, le=200), user: dict = Depends(auth.require("sell"))):
    """Newest first. For the next page pass `before` = created_at of the last bill you got."""
    q = _filters(user, day, date_from, date_to, method, made_by, search)
    if before:
        q["created_at"] = {"$lt": before}
    invs = await get_db().invoices.find(q, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return [_summary(i) for i in invs]


async def _get_invoice(user: dict, invoice_id: str) -> dict:
    inv = await get_db().invoices.find_one({**_scope(user), "id": invoice_id}, {"_id": 0})
    if not inv:
        raise HTTPException(404, "Bill not found.")
    return inv


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user: dict = Depends(auth.require("sell"))):
    """`shop` is the shop's name, address and phone today, for the top of the in-app bill (like the printed one)."""
    inv = await _get_invoice(user, invoice_id)
    biz = await get_db().businesses.find_one({"id": user["business_id"]}, {"_id": 0, "name": 1, "address": 1, "phone": 1}) or {}
    return respond({**_shown(inv), "shop": {k: biz.get(k) or "" for k in ("name", "address", "phone")}})


# ---- The customer's bill link (/i/{token}, sent on WhatsApp; no login)
# The token is 128 random bits. A link works for BILL_LINK_DAYS after it was made (0 = no limit); sharing the bill
# again after that makes a new link. "Stop link" (DELETE .../share) or "New link" (POST .../share/rotate) ends the
# old one at once, e.g. after the bill went to the wrong number. Bills made before links expired count from the bill.
TOKEN_MIN, TOKEN_MAX = 16, 64
PUBLIC_HEADERS = {"X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"}


def _link_days() -> int:
    try:
        return max(0, int(os.environ.get("BILL_LINK_DAYS", "180")))
    except ValueError:
        return 180


def _link_expired(inv: dict) -> bool:
    days = _link_days()
    if not days:
        return False
    made = datetime.fromisoformat(inv.get("public_token_at") or inv["created_at"])
    return datetime.now(timezone.utc) > made + timedelta(days=days)


def _link(request: Request, token: str) -> dict:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return {"token": token, "url": f"{base}/i/{token}"}


async def _new_token(inv: dict) -> str:
    token = secrets.token_urlsafe(16)
    await get_db().invoices.update_one({"id": inv["id"], "business_id": inv["business_id"]},
                                       {"$set": {"public_token": token, "public_token_at": now_iso()}})
    return token


@router.get("/invoices/{invoice_id}/share")
async def share_invoice(invoice_id: str, request: Request, user: dict = Depends(auth.require("sell"))):
    """Link to the customer's bill page (sent on WhatsApp). Needs no login; the token can't be guessed."""
    inv = await _get_invoice(user, invoice_id)
    token = inv.get("public_token")
    if not token or _link_expired(inv):
        token = await _new_token(inv)
    return _link(request, token)


@router.post("/invoices/{invoice_id}/share/rotate")
async def rotate_share_link(invoice_id: str, request: Request, user: dict = Depends(auth.require("sell"))):
    """A new link for the bill; the old one stops working at once."""
    return _link(request, await _new_token(await _get_invoice(user, invoice_id)))


@router.delete("/invoices/{invoice_id}/share")
async def stop_share_link(invoice_id: str, user: dict = Depends(auth.require("sell"))):
    """The bill's link stops working; sharing the bill again makes a new one."""
    inv = await _get_invoice(user, invoice_id)
    await get_db().invoices.update_one({"id": inv["id"], "business_id": inv["business_id"]},
                                       {"$unset": {"public_token": "", "public_token_at": ""}})
    return {"ok": True}


_logo_cache: dict[str, str] = {}


async def _logo_src(biz: dict) -> str | None:
    """The shop's logo as a small inline image, so it shows in the bill page and in saved PDFs (even offline)."""
    path = biz.get("logo_path")
    if not path:
        return None
    if path not in _logo_cache:
        try:
            data, _ = await run_in_threadpool(storage_client.get_object, path)
            small = await run_in_threadpool(ai_client.fit_thumbnail, data)
        except Exception:
            return None
        if len(_logo_cache) > 500:
            _logo_cache.clear()
        _logo_cache[path] = "data:image/jpeg;base64," + base64.b64encode(small).decode()
    return _logo_cache[path]


@router.get("/invoices/{invoice_id}/html", response_class=HTMLResponse)
async def invoice_page_for_print(invoice_id: str, user: dict = Depends(auth.require("sell"))):
    inv = await _get_invoice(user, invoice_id)
    db = get_db()
    biz = await db.businesses.find_one({"id": user["business_id"]}, {"_id": 0}) or {}
    return HTMLResponse(invoice_html.render(inv, biz, public=False, logo_src=await _logo_src(biz),
                                            codes=await gst.bill_codes(db, inv)))


@public_router.get("/i/{token}", response_class=HTMLResponse, include_in_schema=False)
async def public_bill(token: str):
    db = get_db()
    inv = await db.invoices.find_one({"public_token": token}, {"_id": 0}) if TOKEN_MIN <= len(token) <= TOKEN_MAX else None
    if not inv:
        return HTMLResponse("<h1>Bill not found</h1>", status_code=404, headers=PUBLIC_HEADERS)
    if _link_expired(inv):
        return HTMLResponse("<h1>This bill link has expired</h1><p>Please ask the shop to send the bill again.</p>",
                            status_code=410, headers=PUBLIC_HEADERS)
    biz = await db.businesses.find_one({"id": inv["business_id"]}, {"_id": 0}) or {}
    return HTMLResponse(invoice_html.render(inv, biz, logo_src=await _logo_src(biz), codes=await gst.bill_codes(db, inv)),
                        headers=PUBLIC_HEADERS)
