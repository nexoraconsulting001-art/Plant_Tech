// Checks src/paging.ts, the client side of the paged lists (Products, Customers).
// Run from frontend/: node --experimental-strip-types --test scripts/test-paging.mjs
// (Node 22.6+; from Node 23.6 the flag is not needed.)
import assert from "node:assert/strict";
import { test } from "node:test";

import { asPage, mergePages, nextCursor, queryString } from "../src/paging.ts";

// A server like GET /customers?limit=&cursor=: sorted by (name, id), the cursor is the last row's sort key.
function fakeServer(rows) {
  const key = (r) => [r.name, r.id];
  const before = (a, b) => key(a)[0] < key(b)[0] || (key(a)[0] === key(b)[0] && key(a)[1] < key(b)[1]); // as MongoDB sorts strings
  const sorted = [...rows].sort((a, b) => (before(a, b) ? -1 : before(b, a) ? 1 : 0));
  return ({ limit, cursor }) => {
    let start = 0;
    if (cursor) {
      const [name, id] = JSON.parse(Buffer.from(cursor, "base64url").toString());
      start = sorted.findIndex((r) => r.name > name || (r.name === name && r.id > id));
      if (start < 0) start = sorted.length;
    }
    const page = sorted.slice(start, start + limit);
    const more = start + limit < sorted.length;
    const last = page[page.length - 1];
    return {
      items: page,
      next_cursor: more ? Buffer.from(JSON.stringify([last.name, last.id])).toString("base64url") : null,
      ...(cursor ? {} : { total: sorted.length }),
    };
  };
}

test("every one of 20,000 customers is reachable page by page (no cap at 5000)", () => {
  const rows = Array.from({ length: 20000 }, (_, i) => ({ id: `c${String(i).padStart(5, "0")}`, name: `Customer ${i % 997}` }));
  const server = fakeServer(rows);
  const pages = [];
  let cursor = "";
  for (let guard = 0; guard < 1000; guard++) {
    const page = asPage(server({ limit: 50, cursor }));
    pages.push(page);
    const next = nextCursor(page);
    if (next === undefined) break;
    cursor = next;
  }
  assert.equal(pages.length, 400);
  assert.equal(pages[0].total, 20000);
  const items = mergePages(pages);
  assert.equal(items.length, 20000);
  assert.equal(new Set(items.map((c) => c.id)).size, 20000);
});

test("a row seen on two pages shows once, in its first place", () => {
  const a = { id: "a" }, b = { id: "b" }, c = { id: "c" };
  assert.deepEqual(mergePages([{ items: [a, b], next_cursor: "x" }, { items: [b, c], next_cursor: null }]), [a, b, c]);
  assert.deepEqual(mergePages([]), []);
});

test("a server without pages: its whole array is one last page", () => {
  const page = asPage([{ id: "1" }, { id: "2" }]);
  assert.deepEqual(page, { items: [{ id: "1" }, { id: "2" }], next_cursor: null, total: 2 });
  assert.equal(nextCursor(page), undefined);
  const paged = { items: [], next_cursor: "abc" };
  assert.equal(asPage(paged), paged);
  assert.equal(nextCursor(paged), "abc");
  assert.equal(nextCursor({ items: [], next_cursor: "" }), undefined);
});

test("query strings leave out unset filters and encode the rest", () => {
  assert.equal(queryString({}), "");
  assert.equal(queryString({ search: "", category: "", low: false, cursor: undefined, limit: 50 }), "?limit=50");
  assert.equal(queryString({ search: "गुलाब", low: true, limit: 100 }), "?search=%E0%A4%97%E0%A5%81%E0%A4%B2%E0%A4%BE%E0%A4%AC&low=true&limit=100");
  assert.equal(queryString({ cursor: "WyJhIiwgImIiXQ==", search: "a&b +91" }), "?cursor=WyJhIiwgImIiXQ%3D%3D&search=a%26b%20%2B91");
  assert.equal(queryString({ limit: 0 }), "?limit=0");
});
