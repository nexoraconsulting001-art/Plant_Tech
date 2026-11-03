// The order's save timing and retry rules (src/sync-policy.ts), run by Node itself, no test framework:
//   cd frontend && node --test --experimental-strip-types tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { SAVE_AFTER_MS, backoffMs, linesMissing, retryable } from "../src/sync-policy.ts";

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

test("lines from the server join only for products not already on the phone", () => {
  const local = [{ product_id: "a", quantity: 3 }, { product_id: "loose-x", quantity: 1 }];
  const server = [{ product_id: "a", quantity: 1 }, { product_id: "b", quantity: 2 }, { product_id: "c", quantity: 1 }];
  assert.deepEqual(linesMissing(local, server), [{ product_id: "b", quantity: 2 }, { product_id: "c", quantity: 1 }]);
  assert.deepEqual(linesMissing(local, []), []);
});
