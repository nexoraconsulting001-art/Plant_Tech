// Keeping the orders safe: on the phone first, then on the server (one CartSync per logged-in user, see cart-context).
// - The order on screen and every change still to reach the server are kept on the phone (storage key per user), so
//   killing the app or losing the network loses nothing; they come back when the app starts again.
// - The server gets each customer's order 1.5 s after the last change (SAVE_AFTER_MS), at once when the worker moves
//   to another customer or the app goes to the background. A send that fails is tried again with backoff
//   (sync-policy.ts; never sooner than a busy server's Retry-After, after api.ts's own tries), when the phone is back
//   online, and when the app comes back to the foreground; one that the
//   server refuses (a 4xx other than 408/429) is dropped. Either way the order shows "Not saved" until a save lands.
// - "Outbox": per customer, the latest thing to send, either their order (put) or its removal (drop). A newer change
//   replaces an older one that hasn't gone yet, so only the latest version is ever sent.
// - merge: the order was opened while the saved one couldn't be read (offline). Before it is sent, the saved one is
//   read and its other lines are added (onMerged tells the screen), so the server's copy is never overwritten unseen.
import { AppState } from "react-native";
import { onlineManager } from "@tanstack/react-query";

import { ApiError, apiDelete, apiGet, apiPut } from "@/src/api";
import type { CartItem, Correction, OrderCustomer } from "@/src/cart-context";
import { SAVE_AFTER_MS, backoffMs, linesMissing, retryable } from "@/src/sync-policy";
import { storage } from "@/src/utils/storage";

// billKey: the Idempotency-Key of this order's bill (order.ts), made at the first Pay and kept until it is billed or
// left. inDoubt: a bill was sent and no answer came, so it may have been made; nothing is sent to the server for this
// order meanwhile (a save landing after the bill would bring back a saved order that was already billed).
// loaded: the customer's saved order was read (or there was none); false = opened offline, merged when sent.
export type Order = {
  uid: string | null; customer: OrderCustomer | null; items: CartItem[]; discount: number; gstRemoved: boolean;
  correcting: Correction | null; billKey: string | null; inDoubt: boolean; loaded: boolean;
};

export const cartBody = (o: Order) => ({
  customer_id: o.customer!.id, discount: o.discount, gst_removed: o.gstRemoved,
  items: o.items.map((i) => ({ ...i, list_price: i.list_price ?? null, gst_percent: i.gst_percent ?? 0, image_path: i.image_path ?? null, stock: i.stock ?? null,
    unit: i.unit ?? null })),
});
type Body = ReturnType<typeof cartBody>;
export type Pending = { kind: "put"; body: Body; merge: boolean } | { kind: "drop" };
type Stored = { v: 1; order: Order | null; outbox: Record<string, Pending> };

const PERSIST_AFTER_MS = 250;
const key = (uid: string) => `hh.order.v1.${uid}`;
const savable = (o: Order | null): o is Order & { customer: OrderCustomer } => !!o?.customer && !o.correcting && !o.inDoubt;

export class CartSync {
  private order: Order | null = null; // the order on screen
  private outbox: Record<string, Pending> = {};
  private failed = new Set<string>(); // customers whose last save didn't land
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private storeTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private running: Promise<void> | null = null;
  private again = false;
  private inflight: { cid: string; done: Promise<unknown> } | null = null;
  private listeners = new Set<() => void>();
  private stopped = false;
  version = 0; // changes whenever what unsaved() says may have changed
  private onMerged: ((cid: string, extra: CartItem[]) => void) | null = null;

  constructor(private uid: string) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private emit() {
    this.version += 1;
    this.listeners.forEach((fn) => fn());
  }

  // Online again, or back in the foreground: send what is waiting. Going to the background: send it now and write
  // the phone's copy at once (the app may be killed there).
  start(onMerged: (cid: string, extra: CartItem[]) => void) {
    this.stopped = false;
    this.onMerged = onMerged;
    const offOnline = onlineManager.subscribe((online) => {
      this.emit();
      if (online) this.pump();
    });
    const app = AppState.addEventListener("change", (s) => {
      if (s === "active") this.pump();
      else if (s === "background") {
        this.flush();
        this.store();
      }
    });
    return () => {
      this.stopped = true;
      this.onMerged = null;
      offOnline();
      app.remove();
      [this.saveTimer, this.storeTimer, this.retryTimer].forEach((t) => t && clearTimeout(t));
      this.store();
    };
  }

  // What was kept on the phone when the app last ran: its queued saves join the outbox (newer ones here win) and
  // the order that was on screen is returned.
  async restore(): Promise<Order | null> {
    const raw = await storage.getItem(key(this.uid), null);
    let s: Stored | null = null;
    try {
      s = typeof raw === "string" ? JSON.parse(raw) : null;
    } catch {}
    if (s?.v !== 1) return null;
    this.outbox = { ...s.outbox, ...this.outbox };
    this.emit();
    this.pump();
    return s.order?.uid === this.uid ? s.order : null;
  }

  // The order on screen changed: keep it on the phone; send it after a pause (if it is a customer's order).
  keep(o: Order, save: boolean) {
    this.order = o;
    this.storeSoon();
    if (!save) return;
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    if (savable(o)) this.saveTimer = setTimeout(() => this.flush(), SAVE_AFTER_MS);
  }

  // Send a waiting change now (before leaving this order). Doesn't wait for the server.
  flush() {
    if (!this.saveTimer) return;
    clearTimeout(this.saveTimer);
    this.saveTimer = null;
    const o = this.order;
    if (savable(o)) this.queue(o.customer.id, { kind: "put", body: cartBody(o), merge: !o.loaded });
  }

  // "Try again" on "Not saved": the order on screen is sent again now (also one the server refused before), with
  // everything else waiting.
  saveNow() {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    const o = this.order;
    if (savable(o)) this.queue(o.customer.id, { kind: "put", body: cartBody(o), merge: !o.loaded });
    else this.pump();
  }

  queue(cid: string, p: Pending) {
    this.outbox[cid] = p;
    this.storeSoon();
    this.emit();
    this.pump();
  }

  // The newest version of this customer's order still waiting for the server (newer than the server's copy).
  waiting(cid: string): Pending | undefined {
    return this.outbox[cid];
  }

  // Billing this customer: the bill carries the order and the server then removes the saved one, so a waiting save
  // is not sent. One already on its way is waited for (at most its time limit) so it can't land after the bill.
  async beforeBill(cid: string) {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    this.forget(cid);
    if (this.inflight?.cid === cid) await this.inflight.done;
  }

  // Nothing more to send for this customer (billed, or the order was left unsaved on purpose).
  forget(cid: string) {
    if (this.outbox[cid]?.kind === "put") delete this.outbox[cid];
    this.failed.delete(cid);
    this.storeSoon();
    this.emit();
  }

  // "Not saved": the order is only on this phone for now.
  unsaved(cid: string | undefined) {
    if (!cid) return false;
    return this.failed.has(cid) || (!!this.outbox[cid] && !onlineManager.isOnline());
  }

  // Send everything waiting, one request at a time. Called again while running: one more round afterwards.
  pump(): Promise<void> {
    if (this.running) {
      this.again = true;
      return this.running;
    }
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.running = this.drain().finally(() => {
      this.running = null;
      this.emit();
    });
    return this.running;
  }

  private async drain() {
    do {
      this.again = false;
      let retry = false;
      let notBefore = 0; // the longest Retry-After a busy server asked for in this round
      for (const cid of Object.keys(this.outbox)) {
        if (this.stopped) return;
        if (!onlineManager.isOnline()) return; // the online event starts it again
        const p = this.outbox[cid];
        if (!p) continue;
        const done = this.send(cid, p);
        this.inflight = { cid, done: done.catch(() => {}) };
        try {
          await done;
          if (this.outbox[cid] === p) delete this.outbox[cid];
          this.failed.delete(cid);
          this.attempt = 0;
        } catch (e) {
          this.failed.add(cid);
          // Kept and tried again (no answer, a timeout, a server error); anything else is refused as it is and
          // dropped, so the next change tries again. Retry-After only sets the wait, never whether it is kept.
          if (retryable(e instanceof ApiError ? e.status : -1)) {
            retry = true;
            if (e instanceof ApiError && e.retryAfterMs) notBefore = Math.max(notBefore, e.retryAfterMs);
          } else if (this.outbox[cid] === p) delete this.outbox[cid];
        } finally {
          this.inflight = null;
          this.storeSoon();
          this.emit();
        }
      }
      if (retry && !this.stopped) {
        this.retryTimer = setTimeout(() => this.pump(), Math.max(backoffMs(this.attempt++), notBefore));
        return;
      }
    } while (this.again);
  }

  private async send(cid: string, p: Pending) {
    const q = `/orders/cart?customer_id=${encodeURIComponent(cid)}`;
    if (p.kind === "drop") return apiDelete(q);
    if (p.merge) {
      const s = await apiGet(q);
      const extra = linesMissing(p.body.items, s?.items ?? []);
      // merged once: a retry of this same entry sends it as it is now
      p.body = { ...p.body, items: [...p.body.items, ...extra], discount: p.body.discount || s?.discount || 0 };
      p.merge = false;
      this.onMerged?.(cid, extra);
    }
    return apiPut("/orders/cart", p.body);
  }

  private storeSoon() {
    if (this.storeTimer) return;
    this.storeTimer = setTimeout(() => this.store(), PERSIST_AFTER_MS);
  }

  // One string per user (the storage helper keeps plain values only).
  private store() {
    if (this.storeTimer) clearTimeout(this.storeTimer);
    this.storeTimer = null;
    const s: Stored = { v: 1, order: this.order, outbox: this.outbox };
    storage.setItem(key(this.uid), JSON.stringify(s));
  }
}
