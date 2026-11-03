// k6 load test: the billing path, one iteration = one bill, at a fixed rate of bills per second.
//
//   k6 run -e BASE_URL=https://staging-api.example -e MULT=1 loadtest/k6/bill.js     # 14 bills/s (design peak)
//   k6 run -e BASE_URL=... -e MULT=3 -e DURATION=15m loadtest/k6/bill.js               # 42 bills/s (the target)
//   k6 run -e BASE_URL=... -e MULT=5 loadtest/k6/bill.js                               # 70 bills/s (headroom)
//
// Data: loadtest/shops.json from loadtest/seed.py (SHOPS_FILE to use another). Each bill, like the app:
//   customer screen (open + typing), load the saved order, 2-5 products (typing a search per product, cart saved
//   after each), the Pay screen's stock check, POST /api/invoices with an Idempotency-Key (retried once with the
//   same key on a network error or 5xx), the bill screen, the Home refresh, and half the time the WhatsApp link
//   and the customer opening it.
// Environment: BASE_URL, MULT (1/3/5), PEAK (bills/s at MULT=1, default 14), DURATION (hold, default 10m),
//   RAMP (default 1m), THINK (multiplies the pauses, default 1; 0 = no pauses), SHOPS_FILE.
// Pass/fail: thresholds below (p95 of POST /api/invoices under 500 ms = decision D13, errors under 1%).
// Never point this at production: it creates real bills.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

const BASE = (__ENV.BASE_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');
const MULT = Number(__ENV.MULT || 1);
const PEAK = Number(__ENV.PEAK || 14);
const RATE = Math.max(1, Math.round(PEAK * MULT));
const THINK = Number(__ENV.THINK || 1);
const DATA = JSON.parse(open(__ENV.SHOPS_FILE || '../shops.json'));
const SHOPS = new SharedArray('shops', () => DATA.shops);
const PRODUCT_TERMS = DATA.product_terms;
const CUSTOMER_TERMS = DATA.customer_terms;

const billTime = new Trend('bill_flow_duration', true);
const billsMade = new Counter('bills_made');
const replays = new Counter('invoice_retries');

export const options = {
  scenarios: {
    bills: {
      executor: 'ramping-arrival-rate',
      startRate: Math.max(1, Math.round(RATE / 5)),
      timeUnit: '1s',
      stages: [
        { target: RATE, duration: __ENV.RAMP || '1m' },
        { target: RATE, duration: __ENV.DURATION || '10m' },
      ],
      preAllocatedVUs: RATE * 15, // a bill takes about 10-15 s of a VU's time with THINK=1
      maxVUs: RATE * 40,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    'http_req_duration{name:POST /api/invoices}': ['p(95)<500', 'p(99)<1500'],
    'http_req_duration{name:GET /api/products?search}': ['p(95)<400'],
    'http_req_duration{name:PUT /api/orders/cart}': ['p(95)<300'],
    http_req_duration: ['p(95)<800'],
    dropped_iterations: ['count<' + Math.max(10, RATE * 6)], // bills k6 could not start (too few VUs = overload)
  },
  setupTimeout: '10m',
  summaryTrendStats: ['avg', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

const pause = (seconds) => { if (THINK > 0) sleep(seconds * THINK * (0.7 + Math.random() * 0.6)); };
const pick = (list) => list[Math.floor(Math.random() * list.length)];
const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
  const r = (Math.random() * 16) | 0;
  return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
});

// Shops without a token in shops.json (no JWT_SECRET when seeding): log in once, a few at a time.
export function setup() {
  const tokens = {};
  const missing = [];
  for (const s of SHOPS) if (!s.token) missing.push(s); // a SharedArray has no .filter
  for (let i = 0; i < missing.length; i += 10) {
    const batch = missing.slice(i, i + 10).map((s) => ['POST', `${BASE}/api/auth/login`,
      JSON.stringify({ phone: s.phone, password: DATA.password }),
      { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /api/auth/login' } }]);
    http.batch(batch).forEach((r, k) => {
      if (r.status === 200) tokens[missing[i + k].phone] = r.json('token');
    });
    sleep(1);
  }
  return { tokens };
}

function api(method, path, token, body, name, extraHeaders) {
  const params = {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(extraHeaders || {}) },
    tags: { name: name || `${method} ${path.split('?')[0]}` },
    timeout: '30s',
  };
  return http.request(method, `${BASE}${path}`, body === undefined ? null : JSON.stringify(body), params);
}

// Typing a search: one request per keystroke from the 2nd letter (300 ms debounce in the app, about 1 in 2 kept).
function typeSearch(term, request) {
  let last = null;
  for (let k = 2; k <= term.length; k += 1) {
    if (k < term.length && Math.random() < 0.5) continue;
    last = request(term.slice(0, k));
    pause(0.35);
  }
  return last;
}

export default function (data) {
  const shop = SHOPS[exec.scenario.iterationInTest % SHOPS.length];
  const token = shop.token || data.tokens[shop.phone];
  if (!token) { check(null, { 'shop has a login': () => false }); return; }
  const started = Date.now();

  // 1. Customer screen: opens with the recent customers, then the owner types a name
  let r = api('GET', '/api/customers/find?q=', token, undefined, 'GET /api/customers/find');
  check(r, { 'customers 200': (x) => x.status === 200 });
  pause(1);
  r = typeSearch(pick(CUSTOMER_TERMS), (q) => api('GET', `/api/customers/find?q=${encodeURIComponent(q)}`, token,
    undefined, 'GET /api/customers/find'));
  let found = r && r.status === 200 ? r.json() : [];
  if (!found || !found.length) {
    const recent = api('GET', '/api/customers/find?q=', token, undefined, 'GET /api/customers/find');
    found = recent.status === 200 ? recent.json() : [];
  }
  if (!found || !found.length) { check(null, { 'a customer to bill': () => false }); return; }
  const customer = found[0];

  // 2. The customer's saved order, if any
  r = api('GET', `/api/orders/cart?customer_id=${customer.id}`, token, undefined, 'GET /api/orders/cart');
  check(r, { 'cart 200': (x) => x.status === 200 });

  // 3. Products: 2-5 lines (3.5 on average), a search for each, the order saved after each change
  const lines = [];
  const want = 2 + Math.floor(Math.random() * 4);
  for (let n = 0; n < want; n += 1) {
    pause(1.5);
    r = typeSearch(pick(PRODUCT_TERMS), (q) => api('GET', `/api/products?search=${encodeURIComponent(q)}`, token,
      undefined, 'GET /api/products?search'));
    const products = r && r.status === 200 ? r.json() : [];
    check(r, { 'product search 200': (x) => x && x.status === 200 });
    const p = (products || []).find((x) => !lines.some((l) => l.product_id === x.id));
    if (!p) continue;
    lines.push({ product_id: p.id, name: p.name, quantity: 1 + Math.floor(Math.random() * 3), price: p.selling_price,
      list_price: p.selling_price, gst_percent: p.gst_percent || 0, image_path: p.image_path || null,
      stock: p.stock, unit: p.unit || null });
    pause(0.6);
    r = api('PUT', '/api/orders/cart', token, { customer_id: customer.id, items: lines, discount: 0, gst_removed: false },
      'PUT /api/orders/cart');
    check(r, { 'cart saved': (x) => x.status === 200 });
  }
  if (!lines.length) { check(null, { 'products found': () => false }); return; }

  // 4. Pay screen: fresh stock of the items
  pause(2);
  r = api('GET', `/api/products?ids=${lines.map((l) => l.product_id).join(',')}`, token, undefined, 'GET /api/products?ids');
  check(r, { 'stock check 200': (x) => x.status === 200 });

  // 5. The bill, retried once with the same key if the answer never arrived
  const key = uuid();
  const bill = {
    items: lines.map((l) => ({ product_id: l.product_id, name: l.name, quantity: l.quantity, price: l.price, unit: null })),
    customer_id: customer.id, discount: 0, payment_method: pick(['Cash', 'Cash', 'UPI', 'Card']), gst_removed: false,
  };
  r = api('POST', '/api/invoices', token, bill, 'POST /api/invoices', { 'Idempotency-Key': key });
  if (r.status === 0 || r.status >= 500) {
    replays.add(1);
    sleep(1);
    r = api('POST', '/api/invoices', token, bill, 'POST /api/invoices', { 'Idempotency-Key': key });
  }
  const ok = check(r, { 'bill made': (x) => x.status === 200 && !!x.json('id') });
  if (!ok) return;
  billsMade.add(1);
  const invoice = r.json();

  // 6. Bill screen, Home refreshes, and half the time the WhatsApp link and the customer opening it
  r = api('GET', `/api/invoices/${invoice.id}`, token, undefined, 'GET /api/invoices/{id}');
  check(r, { 'bill shown': (x) => x.status === 200 });
  api('GET', '/api/dashboard/today', token, undefined, 'GET /api/dashboard/today');
  if (Math.random() < 0.5) {
    r = api('GET', `/api/invoices/${invoice.id}/share`, token, undefined, 'GET /api/invoices/{id}/share');
    if (check(r, { 'share link': (x) => x.status === 200 })) {
      const page = http.get(`${BASE}/i/${r.json('token')}`, { tags: { name: 'GET /i/{token}' } });
      check(page, { 'customer bill page': (x) => x.status === 200 });
    }
  }
  billTime.add(Date.now() - started);
}
