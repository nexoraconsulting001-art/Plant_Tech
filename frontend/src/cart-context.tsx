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
import React, { createContext, useContext, useRef, useState } from "react";

import { apiDelete, apiGet, apiPut } from "@/src/api";
import { useAuth } from "@/src/auth";
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

// the bill being corrected; returning = how much of each product it took (given back when the correction is billed)
export type Correction = { id: string; number: string; until: string; returning: Record<string, number> };

type Order = { uid: string | null; customer: OrderCustomer | null; items: CartItem[]; discount: number; gstRemoved: boolean;
  correcting: Correction | null };
type Saved = { items: CartItem[]; discount: number; gstRemoved: boolean } | null;

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
  beforeBill: () => Promise<void>; // send the last change first, then nothing more
  billed: () => void;
  correcting: Correction | null;
  startCorrection: (inv: Invoice, products: Product[]) => Promise<string[]>; // returns the names left out (no longer a product)
  returning: (productId: string) => number; // of this product, how much the bill being corrected gives back
};

const CartContext = createContext<CartContextType | null>(null);
const SAVE_AFTER_MS = 600;

export const isLoose = (id: string) => id.startsWith("loose-");
// "3 items": pieces count one by one; 1.5 kg of something counts as one item
const howMany = (items: CartItem[]) => items.reduce((s, i) => s + (wholeUnit(i.unit) ? i.quantity : 1), 0);
// Never more than the server takes on one line (a refused save would be lost silently), whole for pieces.
const fitQty = (i: CartItem, qty: number) => Math.min(MAX_QTY, roundQty(qty, wholeUnit(i.unit)));

const body = (o: Order) => ({
  customer_id: o.customer!.id, discount: o.discount, gst_removed: o.gstRemoved,
  items: o.items.map((i) => ({ ...i, list_price: i.list_price ?? null, gst_percent: i.gst_percent ?? 0, image_path: i.image_path ?? null, stock: i.stock ?? null,
    unit: i.unit ?? null })),
});

export function CartProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const uid = user?.id ?? null;
  const blank: Order = { uid, customer: null, items: [], discount: 0, gstRemoved: false, correcting: null };
  const [state, setState] = useState<Order>(blank);
  const order = state.uid === uid ? state : blank; // a new log in starts with no order
  const latest = useRef(order);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sending = useRef<Promise<unknown>>(Promise.resolve());

  const send = (o: Order) => {
    sending.current = sending.current.then(() => apiPut("/orders/cart", body(o)).catch(() => {}));
    return sending.current;
  };
  const cancel = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  // Send a waiting change now (before leaving this customer).
  const flush = () => {
    if (!timer.current) return sending.current;
    cancel();
    return send(latest.current);
  };

  const put = (next: Order) => {
    latest.current = next;
    setState(next);
  };
  const change = (fn: (o: Order) => Order) => {
    const base = latest.current.uid === uid ? latest.current : blank;
    const next = fn(base);
    put(next);
    if (!next.customer || next.correcting) return; // a correction is never saved as the customer's order
    cancel();
    timer.current = setTimeout(() => {
      timer.current = null;
      send(latest.current);
    }, SAVE_AFTER_MS);
  };
  const setItems = (fn: (prev: CartItem[]) => CartItem[]) => change((o) => ({ ...o, items: fn(o.items) }));

  async function load(c: OrderCustomer): Promise<Saved> {
    try {
      const s = await apiGet(`/orders/cart?customer_id=${encodeURIComponent(c.id)}`);
      return s ? { items: s.items ?? [], discount: s.discount ?? 0, gstRemoved: !!s.gst_removed } : null;
    } catch {
      return null; // offline: start empty; it is saved when it next changes
    }
  }

  async function openFor(c: OrderCustomer) {
    await flush();
    if (latest.current.customer?.id === c.id && latest.current.uid === uid && !latest.current.correcting) return howMany(latest.current.items);
    const saved = await load(c);
    put({ uid, customer: c, items: saved?.items ?? [], discount: saved?.discount ?? 0, gstRemoved: saved?.gstRemoved ?? false, correcting: null });
    return howMany(saved?.items ?? []);
  }

  async function moveTo(c: OrderCustomer) {
    const from = latest.current.uid === uid ? latest.current : blank;
    if (from.customer?.id === c.id) return 0;
    if (from.correcting) { // the corrected bill goes to another customer: nothing saved to move
      change(() => ({ ...from, customer: c }));
      return 0;
    }
    cancel();
    await sending.current;
    const saved = await load(c);
    if (from.customer) apiDelete(`/orders/cart?customer_id=${encodeURIComponent(from.customer.id)}`).catch(() => {});
    const extra = (saved?.items ?? []).filter((s) => !from.items.some((i) => i.product_id === s.product_id));
    change(() => ({ uid, customer: c, items: [...from.items, ...extra], discount: from.discount || saved?.discount || 0,
      gstRemoved: from.gstRemoved, correcting: null }));
    return extra.length;
  }

  function startNew() {
    flush();
    put(blank);
  }

  async function discard() {
    const o = latest.current.uid === uid ? latest.current : blank;
    const c = o.correcting ? null : o.customer; // stopping a correction: the customer's saved order stays
    cancel();
    await sending.current;
    put(blank);
    if (c) await apiDelete(`/orders/cart?customer_id=${encodeURIComponent(c.id)}`).catch(() => {});
  }

  async function beforeBill() {
    cancel();
    await sending.current; // a save still on its way must land before the bill clears the saved order
  }

  // The bill's lines as they are now: the price it was billed at, the shop's price, GST and stock today
  // (plus what this bill took). Lines of deleted products are left out; one line per product, like any order.
  async function startCorrection(inv: Invoice, products: Product[]) {
    await flush(); // the order on screen (if any) stays saved for its customer
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
    put({ uid, customer: inv.customer_id ? { id: inv.customer_id, name: inv.customer_name, phone: inv.customer_phone } : null, items,
      discount: inv.discount, gstRemoved: !!inv.gst_removed,
      correcting: { id: inv.id, number: inv.invoice_number, until: inv.correct_until ?? "", returning } });
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
      openFor, moveTo, startNew, discard, beforeBill, billed: () => put(blank),
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
