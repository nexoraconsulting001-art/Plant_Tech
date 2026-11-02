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

// The server's lines for products that are not in the order on the phone yet (an order opened while the saved one
// couldn't be read keeps what was added on the phone and gets the rest when it can be read).
export function linesMissing<T extends { product_id: string }>(local: T[], server: T[]): T[] {
  const have = new Set(local.map((i) => i.product_id));
  return server.filter((s) => !have.has(s.product_id));
}
