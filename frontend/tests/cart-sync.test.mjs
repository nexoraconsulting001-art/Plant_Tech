// The order's outbox (src/cart-sync.ts) and the API client's tries (src/api.ts), the real modules with a fake fetch:
//   cd frontend && node --test --experimental-transform-types tests/cart-sync.test.mjs
// (transform, not only strip: api.ts and cart-sync.ts use constructor parameter properties). React Native, React
// Query, i18next and the phone's storage are stand-ins (tests/stubs).
import assert from "node:assert/strict";
import { register } from "node:module";
import { mock, test } from "node:test";

register("./stubs/hooks.mjs", import.meta.url);
const { CartSync } = await import("@/src/cart-sync");
const { apiGet, apiPost, setApiToken, waitForLogin } = await import("@/src/api");

// The server's answers in turn: a status (with an optional error code and Retry-After), or "offline".
function serve(...answers) {
  const sent = [];
  globalThis.fetch = async (url, init) => {
    sent.push({ url, ...init });
    const a = answers[Math.min(sent.length, answers.length) - 1];
    if (a === "offline") throw new TypeError("Network request failed");
    const { status, code = "", retryAfter = null, body = {} } = a;
    const headers = { get: (h) => (h.toLowerCase() === "retry-after" ? retryAfter : null) };
    const data = status < 400 ? body : { detail: `error ${status}`, code };
    return { ok: status < 400, status, headers, json: async () => data };
  };
  return sent;
}

const put = { kind: "put", body: { customer_id: "c1", discount: 0, gst_removed: false, items: [] }, merge: false };

// One save of customer c1 against `answer`: whether it is still in the outbox, and the retry timer's delay.
async function saveOnce(answer) {
  serve(answer);
  const timers = mock.method(globalThis, "setTimeout");
  const sync = new CartSync("u1");
  try {
    sync.queue("c1", { ...put });
    await sync.pump();
    const retry = timers.mock.calls.map((c) => c.arguments[1]).filter((ms) => ms >= 500); // not the 250 ms store
    return { kept: !!sync.waiting("c1"), retryIn: retry.length ? retry[retry.length - 1] : null, unsaved: sync.unsaved("c1") };
  } finally {
    sync.start(() => {})(); // stop: clears its timers
    timers.mock.restore();
  }
}

test("a save that got no answer or a server error stays in the outbox and is tried again", async () => {
  for (const answer of ["offline", { status: 500 }, { status: 502 }, { status: 504 }, { status: 408 }]) {
    const r = await saveOnce(answer);
    assert.equal(r.kept, true, JSON.stringify(answer));
    assert.ok(r.retryIn !== null && r.retryIn <= 1000, JSON.stringify(answer)); // first backoff: 0.5 to 1 s
    assert.equal(r.unsaved, true);
  }
});

test("a busy server's Retry-After sets the wait before the next try", async () => {
  const r = await saveOnce({ status: 429, retryAfter: "7" });
  assert.equal(r.kept, true);
  assert.equal(r.retryIn, 7000);
});

test("a save the server refuses as it is is dropped and not tried again, Retry-After or not", async () => {
  for (const answer of [{ status: 422 }, { status: 400, retryAfter: "5" }, { status: 404 }]) {
    const r = await saveOnce(answer);
    assert.equal(r.kept, false, JSON.stringify(answer));
    assert.equal(r.retryIn, null, JSON.stringify(answer));
    assert.equal(r.unsaved, true); // "Not saved" until the next change lands
  }
});

test("a save that lands leaves the outbox", async () => {
  const r = await saveOnce({ status: 200, body: { ok: true } });
  assert.deepEqual(r, { kept: false, retryIn: null, unsaved: false });
});

// A bill is sent again after a 503 (it keeps its Idempotency-Key). Which 503s came before decides what the last
// one means: "busy" alone was refused before any work; a "db_busy" try may have made the bill (order.ts).
async function bill(...answers) {
  const sent = serve(...answers);
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const done = apiPost("/invoices", { items: [] }, { headers: { "Idempotency-Key": "k1" } }).catch((e) => e);
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setImmediate(r));
      mock.timers.tick(20_000);
    }
    return { error: await done, tries: sent.length, keys: sent.map((s) => s.headers["Idempotency-Key"]) };
  } finally {
    mock.timers.reset();
  }
}

test("a bill that got db_busy and then busy may have been made", async () => {
  const { error, tries, keys } = await bill({ status: 503, code: "db_busy", retryAfter: "2" }, { status: 503, code: "busy", retryAfter: "2" });
  assert.equal(tries, 3);
  assert.deepEqual(keys, ["k1", "k1", "k1"]);
  assert.equal(error.status, 503);
  assert.equal(error.code, "busy");
  assert.equal(error.maybeDone, true);
});

test("a bill refused as busy on every try was not made", async () => {
  const { error, tries } = await bill({ status: 503, code: "busy", retryAfter: "2" });
  assert.equal(tries, 3);
  assert.equal(error.code, "busy");
  assert.equal(error.maybeDone, false);
});

test("any last answer after a db_busy try says an earlier try may have done it", async () => {
  const { error, tries } = await bill({ status: 503, code: "db_busy" }, { status: 409, code: "out_of_stock" });
  assert.equal(tries, 2);
  assert.equal(error.code, "out_of_stock");
  assert.equal(error.maybeDone, true); // an earlier try may have done it; order.ts reads this only with a 503
});

test("a request made while the saved login is read waits for it and carries the token", async () => {
  const sent = serve({ status: 200, body: { ok: true } });
  let restore;
  waitForLogin(new Promise((resolve) => { restore = resolve; }).then(() => setApiToken("t-saved")));
  const answer = apiGet("/reports/day");
  await new Promise((r) => setImmediate(r));
  assert.equal(sent.length, 0); // not sent before the login is back
  restore();
  assert.deepEqual(await answer, { ok: true });
  assert.equal(sent[0].headers.Authorization, "Bearer t-saved");
  setApiToken(null);
});
