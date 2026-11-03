// When the order on screen is sent to the server, and whether a failed send is tried again (see cart-sync.ts).
// No imports on purpose: the Node tests in frontend/tests run this file as it is.

export const SAVE_AFTER_MS = 1500; // a pause in changes before the order is sent: one save per burst of taps
const FIRST_RETRY_MS = 1000;
const MAX_RETRY_MS = 30_000;

// 1 s, 2 s, 4 s … up to 30 s, the second half of each random, so phones that lost the network together don't all
// come back in the same second ("equal jitter").
export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(MAX_RETRY_MS, FIRST_RETRY_MS * 2 ** Math.max(0, attempt));
  return Math.round(base / 2 + (random() * base) / 2);
}

// Worth trying again: no answer (0 = offline or timed out), a timeout or "too many requests" from the server, or a
// server error. Any other 4xx refuses the request itself, and sending it again would be refused again.
export function retryable(status: number): boolean {
  return status === 0 || status === 408 || status === 425 || status === 429 || status >= 500;
}

// The server is overloaded (503). "busy": it refused the request before doing anything with it, so any request may be
// sent again. Any other 503 ("db_busy": the database was too slow; or the proxy's own 503) may have done part of the
// work, so only requests that are safe to repeat are: reads, saves that replace (PUT), removals, and a request that
// carries an Idempotency-Key (a bill: the server answers a key it has seen with the bill it already made).
export const BUSY_TRIES = 3; // the first send and two more, inside api.ts, before the error reaches the screen
const BUSY_MAX_WAIT_MS = 10_000;

export function busyRetry(status: number, code: string, method: string, keyed: boolean): boolean {
  if (status !== 503) return false;
  return code === "busy" || ["GET", "PUT", "DELETE"].includes(method.toUpperCase()) || keyed;
}

// The server's Retry-After (seconds, or an HTTP date) in ms; null when there is none or it can't be read.
export function retryAfterMs(header: string | null | undefined, now: number = Date.now()): number | null {
  if (!header) return null;
  const s = header.trim();
  if (/^\d+$/.test(s)) return Number(s) * 1000;
  const at = Date.parse(s);
  return Number.isNaN(at) ? null : Math.max(0, at - now);
}

// The wait before try `attempt` + 1 (0 = the first resend): what the server asked for, at least the backoff above,
// at most 10 s, plus up to half again at random so phones refused together don't come back together.
export function busyWaitMs(attempt: number, retryAfter: number | null, random: () => number = Math.random): number {
  const base = Math.min(BUSY_MAX_WAIT_MS, Math.max(retryAfter ?? 0, FIRST_RETRY_MS * 2 ** Math.max(0, attempt)));
  return Math.round(base + (random() * base) / 2);
}

// The server's lines for products that are not in the order on the phone yet (an order opened while the saved one
// couldn't be read keeps what was added on the phone and gets the rest when it can be read).
export function linesMissing<T extends { product_id: string }>(local: T[], server: T[]): T[] {
  const have = new Set(local.map((i) => i.product_id));
  return server.filter((s) => !have.has(s.product_id));
}
