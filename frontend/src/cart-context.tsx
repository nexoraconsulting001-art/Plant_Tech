// The order being built: its customer (a must), items and discount. Each customer has one saved order on
// the server, so a worker can start Kavita's order, bill Rahul meanwhile, and Kavita's items come back
// when anyone in the shop picks her again. Saved orders are never listed; billing or discarding removes them.
// product_id "loose-…" = a line billed without stock; the server gets product_id null for it.
// price = the price for this order; list_price = the shop's price when the item was added.
// gstRemoved = the owner's "Bill without GST": same total, no GST shown (saved with the order).
// unit = the product's unit when it was added (none = pieces): 1.5 is allowed for kg, litre, metre only.
// Correcting a bill (the owner): startCorrection loads the bill's lines as the order, with `correcting` set. Such an
// order is never saved on the server (the customer's own saved order stays as it is) and Pay sends it to
// /invoices/{id}/correct. The old bill's quantities count as back in stock (`returning`). Starting a new order,
// Discard or billing ends it; the old bill stays as it was unless the correction was billed.
// Keeping it safe (cart-sync.ts): the order is kept on the phone and sent to the server after each pause in changes,
// tried again until it lands ("Not saved" meanwhile). Opening a customer never waits for the previous one's save.
// A customer whose saved order can't be read (offline) starts with what is on the phone; the saved lines are
// added when it can be read, and the worker is told. A saved order that is simply not there is not an error.
// billKey: one Idempotency-Key per bill attempt, kept through retries and "Bill anyway" (order.ts).
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { onlineManager } from "@tanstack/react-query";
import * as Crypto from "expo-crypto";
import { t } from "i18next";

import { ApiError, apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { CartSync, Order } from "@/src/cart-sync";
import { notice } from "@/src/confirm";
import { plural } from "@/src/format";
import type { Invoice, Product } from "@/src/types";
import { MAX_QTY, roundQty, wholeUnit } from "@/src/units";

export type CartItem = {
  product_id: string;
  name: string;
  price: number;
  list_price?: number | null;
  quantity: number;
  gst_percent?: number;
  image_path?: string | null;
  stock?: number | null;
  unit?: string | null;
};

export type OrderCustomer = { id: string; name: string; phone: string; purchase_count?: number };

// the bill being corrected; returning = how much of each product it took (given back when the correction is billed);
// paid / paidBy = what the customer paid on it, so the pay screen can say what to give back or collect
export type Correction = { id: string; number: string; until: string; returning: Record<string, number>; paid?: number; paidBy?: string };

// The customer's saved order: none, or it couldn't be read (offline, no answer).
type Saved = { items: CartItem[]; discount: number; gstRemoved: boolean } | null | "unreadable";

type CartContextType = {
  customer: OrderCustomer | null;
  items: CartItem[];
  addItem: (item: CartItem) => void;
  setQuantity: (product_id: string, qty: number) => void;
  setPrice: (product_id: string, price: number) => void;
  removeItem: (product_id: string) => void;
  discount: number;
  setDiscount: (d: number) => void;
  count: number;
  total: number;
  gstIncluded: number; // 0 when GST is removed from this bill
  gstAvailable: number; // the GST in these prices, removed or not (decides whether the toggle shows)
  gstRemoved: boolean;
  setGstRemoved: (v: boolean) => void;
  openFor: (c: OrderCustomer) => Promise<number>; // this customer's order; returns how many items (quantity) were saved
  moveTo: (c: OrderCustomer) => Promise<number>; // the same items, for another customer (wrong one picked)
  startNew: () => void; // leave this order (it stays saved) and start fresh
  discard: () => Promise<void>; // throw this order away
  beforeBill: () => Promise<void>; // the bill carries the order: a waiting save is dropped, one on its way waited for
  billKey: () => string; // this bill attempt's Idempotency-Key (made at the first call)
  billFailed: (noAnswer: boolean) => void; // the bill wasn't made (false), or no answer came so it may have been (true)
  billed: () => void;
  notSaved: boolean; // this order is only on the phone for now (its save failed, or the phone is offline)
  saveNow: () => void; // try the waiting saves again now
  correcting: Correction | null;
  startCorrection: (inv: Invoice, products: Product[]) => Promise<string[]>; // returns the names left out (no longer a product)
  returning: (productId: string) => number; // of this product, how much the bill being corrected gives back
};

const CartContext = createContext<CartContextType | null>(null);

export const isLoose = (id: string) => id.startsWith("loose-");
// "3 items": pieces count one by one; 1.5 kg of something counts as one item
const howMany = (items: CartItem[]) => items.reduce((s, i) => s + (wholeUnit(i.unit) ? i.quantity : 1), 0);
// Never more than the server takes on one line (a refused save would be lost silently), whole for pieces.
const fitQty = (i: CartItem, qty: number) => Math.min(MAX_QTY, roundQty(qty, wholeUnit(i.unit)));
// the server gives each product line the product's unit now: a quantity saved as 1.5 of what is now
// counted in pieces is rounded, so the next save is not refused
const fitLines = (items: CartItem[]) => items.map((i) => ({ ...i, quantity: fitQty(i, i.quantity) }));
const noSync = () => () => {};

export function CartProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const uid = user?.id ?? null;
  const blank: Order = { uid, customer: null, items: [], discount: 0, gstRemoved: false, correcting: null, billKey: null, inDoubt: false,
    loaded: true };
  const [state, setState] = useState<Order>(blank);
  const order = state.uid === uid ? state : blank; // a new log in starts with no order
  const latest = useRef(order);
  const sync = useMemo(() => (uid ? new CartSync(uid) : null), [uid]);
  const subscribe = useCallback((fn: () => void) => (sync ? sync.subscribe(fn) : noSync()), [sync]);
  useSyncExternalStore(subscribe, () => sync?.version ?? 0); // redraw when "Not saved" may have changed

  // save: send it to the server after the pause (a customer's own order; not a correction)
  const put = (next: Order, save = false) => {
    latest.current = next;
    setState(next);
    sync?.keep(next, save);
  };
  const current = () => (latest.current.uid === uid ? latest.current : blank);
  const change = (fn: (o: Order) => Order) => put(fn(current()), true);
  const setItems = (fn: (prev: CartItem[]) => CartItem[]) => change((o) => ({ ...o, items: fn(o.items) }));

  useEffect(() => {
    if (!sync) return;
    // The saved order of a customer opened offline could be read now: its other lines join the order on screen.
    const stop = sync.start((cid, extra) => {
      const o = latest.current;
      if (o.customer?.id !== cid || o.correcting) return;
      const add = fitLines(extra.filter((s) => !o.items.some((i) => i.product_id === s.product_id)));
      put({ ...o, loaded: true, items: [...o.items, ...add] });
      if (add.length) {
        notice(t("Saved order added"), t("{{name}} had a saved order with {{items}}. They are now in this order.",
          { name: o.customer.name, items: plural(howMany(add), "1 item", "{{n}} items") }));
      }
    });
    // After a restart: the order that was on screen comes back (unless one was started meanwhile).
    sync.restore().then((was) => {
      const o = latest.current; // still from before this log in, or an order started meanwhile
      if (was && (o.uid !== was.uid || (!o.customer && !o.correcting && !o.items.length))) put(was);
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sync]);

  // The customer's saved order: what this phone still has to send if anything (it is newer), else the server's.
  async function load(c: OrderCustomer): Promise<Saved> {
    const waiting = sync?.waiting(c.id);
    if (waiting) {
      return waiting.kind === "drop" ? null
        : { items: fitLines(waiting.body.items), discount: waiting.body.discount, gstRemoved: waiting.body.gst_removed };
    }
    if (!onlineManager.isOnline()) return "unreadable";
    try {
      const s = await apiGet(`/orders/cart?customer_id=${encodeURIComponent(c.id)}`);
      return s ? { items: fitLines(s.items ?? []), discount: s.discount ?? 0, gstRemoved: !!s.gst_removed } : null;
    } catch (e) {
      // refused (e.g. no such customer): there is none; no answer or a server error: unknown, merged when it can be read
      return e instanceof ApiError && e.status >= 400 && e.status < 500 ? null : "unreadable";
    }
  }
  const found = (s: Saved) => (s && s !== "unreadable" ? s : null);

  async function openFor(c: OrderCustomer) {
    sync?.flush(); // the order on screen goes to the server on its own; not waited for
    const o = current();
    if (o.customer?.id === c.id && !o.correcting) return howMany(o.items);
    const saved = await load(c);
    const s = found(saved);
    put({ ...blank, customer: c, items: s?.items ?? [], discount: s?.discount ?? 0, gstRemoved: s?.gstRemoved ?? false,
      loaded: saved !== "unreadable" });
    return howMany(s?.items ?? []);
  }

  async function moveTo(c: OrderCustomer) {
    const from = current();
    if (from.customer?.id === c.id) return 0;
    if (from.correcting) { // the corrected bill goes to another customer: nothing saved to move
      put({ ...from, customer: c });
      return 0;
    }
    const saved = await load(c);
    const s = found(saved);
    // the old customer's saved order is removed, if it was read (nothing the worker never saw is removed)
    if (from.customer) {
      if (from.loaded) sync?.queue(from.customer.id, { kind: "drop" });
      else sync?.forget(from.customer.id);
    }
    const extra = (s?.items ?? []).filter((x) => !from.items.some((i) => i.product_id === x.product_id));
    change(() => ({ ...from, uid, customer: c, items: [...from.items, ...extra], discount: from.discount || s?.discount || 0,
      correcting: null, loaded: saved !== "unreadable" }));
    return extra.length;
  }

  function startNew() {
    sync?.flush();
    put(blank);
  }

  async function discard() {
    const o = current();
    const c = o.correcting ? null : o.customer; // stopping a correction: the customer's saved order stays
    put(blank);
    // a saved order that couldn't be read is left alone (the worker never saw it)
    if (c) {
      if (o.loaded) sync?.queue(c.id, { kind: "drop" });
      else sync?.forget(c.id);
    }
  }

  async function beforeBill() {
    const o = current();
    if (o.customer && !o.correcting) await sync?.beforeBill(o.customer.id);
  }

  function billKey() {
    const o = current();
    if (o.billKey) return o.billKey;
    const k = Crypto.randomUUID();
    put({ ...o, billKey: k });
    return k;
  }

  // A clear answer from the server that the bill wasn't made: saving goes on as usual. No answer: the bill may have
  // been made, so the order stays on the phone only until the next try (same key) settles it.
  function billFailed(noAnswer: boolean) {
    const o = current();
    put({ ...o, inDoubt: noAnswer }, !noAnswer);
  }

  function billed() {
    const c = current().customer;
    if (c && !current().correcting) sync?.forget(c.id); // the server removed the saved order with the bill
    put(blank);
  }

  // The bill's lines as they are now: the price it was billed at, the shop's price, GST and stock today
  // (plus what this bill took). Lines of deleted products are left out; one line per product, like any order.
  async function startCorrection(inv: Invoice, products: Product[]) {
    sync?.flush(); // the order on screen (if any) stays saved for its customer
    const byId = new Map(products.map((p) => [p.id, p]));
    const returning: Record<string, number> = {};
    for (const l of inv.items) if (l.product_id) returning[l.product_id] = roundQty((returning[l.product_id] ?? 0) + l.quantity, false);
    const items: CartItem[] = [];
    const skipped: string[] = [];
    inv.items.forEach((l, i) => {
      if (!l.product_id) {
        items.push({ product_id: `loose-${Date.now().toString(36)}${i}`, name: l.name, price: l.price, list_price: l.price, quantity: l.quantity,
          unit: l.unit ?? null });
        return;
      }
      const p = byId.get(l.product_id);
      if (!p) {
        skipped.push(l.name);
        return;
      }
      const had = items.find((x) => x.product_id === p.id);
      if (had) had.quantity = fitQty(had, had.quantity + l.quantity);
      else items.push({ product_id: p.id, name: p.name, price: l.price, list_price: p.selling_price, quantity: l.quantity, gst_percent: p.gst_percent,
        image_path: p.image_path, stock: p.stock + (returning[p.id] ?? 0), unit: p.unit });
    });
    put({ ...blank, customer: inv.customer_id ? { id: inv.customer_id, name: inv.customer_name, phone: inv.customer_phone } : null, items,
      discount: inv.discount, gstRemoved: !!inv.gst_removed,
      correcting: { id: inv.id, number: inv.invoice_number, until: inv.correct_until ?? "", returning, paid: inv.grand_total,
        paidBy: inv.payment_method } });
    return [...new Set(skipped)];
  }

  const addItem = (item: CartItem) =>
    setItems((prev) => {
      const had = prev.find((i) => i.product_id === item.product_id);
      if (!had) return [...prev, { ...item, list_price: item.list_price ?? item.price, quantity: fitQty(item, item.quantity) }];
      const next = { ...had, unit: item.unit ?? had.unit, stock: item.stock ?? had.stock }; // the newer stock count
      return prev.map((i) => (i === had ? { ...next, quantity: fitQty(next, had.quantity + item.quantity) } : i));
    });
  const setQuantity = (id: string, qty: number) =>
    setItems((prev) => (qty <= 0 ? prev.filter((i) => i.product_id !== id) : prev.map((i) => (i.product_id === id ? { ...i, quantity: fitQty(i, qty) } : i))));
  const setPrice = (id: string, price: number) =>
    setItems((prev) => prev.map((i) => (i.product_id === id ? { ...i, price: Math.max(0, price) } : i)));
  const removeItem = (id: string) => setItems((prev) => prev.filter((i) => i.product_id !== id));

  const { items, customer } = order;
  const subtotal = items.reduce((s, i) => s + i.price * i.quantity, 0);
  const d = Math.min(order.discount, subtotal);
  const factor = subtotal ? (subtotal - d) / subtotal : 1;
  // prices include GST: the tax inside a line is amount * rate / (100 + rate)
  const gstAvailable = items.reduce((s, i) => {
    const r = i.gst_percent || 0;
    return s + (r ? (i.price * i.quantity * factor * r) / (100 + r) : 0);
  }, 0);
  const gstRemoved = order.gstRemoved && gstAvailable > 0.004;

  return (
    <CartContext.Provider value={{
      customer, items, addItem, setQuantity, setPrice, removeItem,
      discount: d, setDiscount: (v) => change((x) => ({ ...x, discount: v })),
      count: howMany(items), total: subtotal - d, gstIncluded: gstRemoved ? 0 : gstAvailable, gstAvailable, gstRemoved,
      setGstRemoved: (v) => change((x) => ({ ...x, gstRemoved: v })),
      openFor, moveTo, startNew, discard, beforeBill, billKey, billFailed, billed,
      notSaved: !order.correcting && !!sync?.unsaved(customer?.id), saveNow: () => sync?.saveNow(),
      correcting: order.correcting, startCorrection, returning: (id) => order.correcting?.returning[id] ?? 0,
    }}>
      {children}
    </CartContext.Provider>
  );
}

export function useCart() {
  const ctx = useContext(CartContext);
  if (!ctx) throw new Error("useCart must be used within CartProvider");
  return ctx;
}
