// The order's save timing and retry rules (src/sync-policy.ts), run by Node itself, no test framework:
//   cd frontend && node --test --experimental-strip-types tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { BUSY_TRIES, SAVE_AFTER_MS, backoffMs, busyRetry, busyWaitMs, linesMissing, retryAfterMs, retryable } from "../src/sync-policy.ts";

test("autosave waits 1.2 to 1.5 s after the last change", () => {
  assert.ok(SAVE_AFTER_MS >= 1200 && SAVE_AFTER_MS <= 1500);
});

test("backoff doubles from 1 s up to 30 s, the second half random", () => {
  const low = (n) => backoffMs(n, () => 0);
  const high = (n) => backoffMs(n, () => 1);
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 10].map(high), [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  assert.deepEqual([0, 1, 5].map(low), [500, 1000, 15000]);
  assert.equal(high(-3), 1000);
});

test("only no answer, 408, 425, 429 and server errors are tried again", () => {
  for (const s of [0, 408, 425, 429, 500, 502, 503, 504]) assert.equal(retryable(s), true, String(s));
  for (const s of [-1, 400, 401, 403, 404, 409, 413, 422]) assert.equal(retryable(s), false, String(s));
});

test("a 503 busy is sent again whatever it was; another 503 only when it is safe to repeat", () => {
  for (const m of ["GET", "POST", "PUT", "PATCH", "DELETE"]) assert.equal(busyRetry(503, "busy", m, false), true, m);
  for (const m of ["GET", "put", "DELETE"]) assert.equal(busyRetry(503, "db_busy", m, false), true, m);
  assert.equal(busyRetry(503, "db_busy", "POST", false), false); // may have done part of its work
  assert.equal(busyRetry(503, "db_busy", "PATCH", false), false);
  assert.equal(busyRetry(503, "db_busy", "POST", true), true); // a bill with its Idempotency-Key
  assert.equal(busyRetry(503, "", "POST", true), true); // the proxy's own 503
  for (const s of [0, 429, 500, 502, 504]) assert.equal(busyRetry(s, "busy", "GET", true), false, String(s));
  assert.equal(BUSY_TRIES, 3);
});

test("Retry-After in seconds or as a date", () => {
  assert.equal(retryAfterMs("2"), 2000);
  assert.equal(retryAfterMs(" 0 "), 0);
  assert.equal(retryAfterMs(null), null);
  assert.equal(retryAfterMs(""), null);
  assert.equal(retryAfterMs("soon"), null);
  const now = Date.parse("2026-11-03T10:00:00Z");
  assert.equal(retryAfterMs("Tue, 03 Nov 2026 10:00:05 GMT", now), 5000);
  assert.equal(retryAfterMs("Tue, 03 Nov 2026 09:59:00 GMT", now), 0);
});

test("a busy server's wait: at least what it asked and the backoff, at most 10 s, plus up to half at random", () => {
  const low = (n, after) => busyWaitMs(n, after, () => 0);
  const high = (n, after) => busyWaitMs(n, after, () => 1);
  assert.deepEqual([low(0, 2000), low(1, 2000), low(2, 2000)], [2000, 2000, 4000]);
  assert.equal(high(0, 2000), 3000);
  assert.equal(low(0, null), 1000);
  assert.equal(low(0, 60_000), 10_000);
  assert.equal(high(9, null), 15_000);
});

test("lines from the server join only for products not already on the phone", () => {
  const local = [{ product_id: "a", quantity: 3 }, { product_id: "loose-x", quantity: 1 }];
  const server = [{ product_id: "a", quantity: 1 }, { product_id: "b", quantity: 2 }, { product_id: "c", quantity: 1 }];
  assert.deepEqual(linesMissing(local, server), [{ product_id: "b", quantity: 2 }, { product_id: "c", quantity: 1 }]);
  assert.deepEqual(linesMissing(local, []), []);
});
