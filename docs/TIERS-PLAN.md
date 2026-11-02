# Hara Hisaab: plan to sell in tiers (draft, 23 Oct 2026)

A plan to review, not built yet. Prices are proposals for the owner to decide. Competitor prices come from
third-party comparison sites and disagree with each other, so check them before fixing our own.

## 1. What we are selling (from the code on `staging`)

| Area | What works today | Where |
|---|---|---|
| Billing | Customer-first orders with drafts, cart per login, tax-inclusive GST by category, "Bill without GST", discounts, Cash / UPI (QR) / Card, printable bill, WhatsApp bill link | `routers/sales.py`, `routers/carts.py`, `invoice_html.py` |
| AI photo scan | Photo → plant or nursery product named and matched to stock; laptops, bills, junk refused; scans refunded when Claude wasn't reached | `routers/ai.py`, `ai_client.py` |
| AI supplier bill | Up to 5 pages read in one go, items matched to stock, new products priced at cost + profit %, supplier found or created without duplicates | `routers/ai.py`, `routers/purchases.py`, `routers/suppliers.py` |
| Stock | Products, stock in/out, stock sheets, categories with GST, shared picture library (admin-checked) | `routers/products.py`, `routers/library.py` |
| People | Owner + employees with 4 switches (sell, stock, customers, reports), employee performance report | `auth.py`, `routers/staff.py`, `sales.py /reports/employees` |
| Customers | Phone lookup, saved order per customer, order history | `routers/customers.py` |
| Reports | Today dashboard, day / week / month sales, every item sold | `sales.py` |
| Language | Full Hindi (626 texts), search in Hindi / Hinglish / English | `frontend/src/locales/hi.json`, `search.py` |
| Sign-in | Phone + password, Google (owners), 4-digit PIN for workers | `routers/auth.py` |

**The wedge against generic billing apps (Vyapar, myBillBook):** they make the shop type item names. We let a
new employee photograph a plant they can't name, and let the owner photograph a supplier bill instead of keying
it in. Plus Hindi and plant-aware search. Price on that, not on "billing software".

**What is missing before anyone can pay:** there is no plan, trial, limit per plan or payment anywhere in the
code. The only limit is one global `AI_DAILY_LIMIT=300` scans per shop per day (`routers/ai.py:36`).

## 2. What it costs us to serve one shop

The marginal cost is almost all Claude. The VM, Mongo and R2 are shared and small.

Rough cost per AI call on `claude-sonnet-5-5` ($2 in / $10 out per million tokens, about ₹88 to the dollar).
These are estimates from the prompt sizes in `ai_client.py`; measure the real numbers first (Phase 0).

| Call | Input | Output (incl. thinking) | Cost each |
|---|---|---|---|
| Plant photo (effort low) | photo ~1.6k + catalogue names + prompt ≈ 3–4k tokens | ~0.5–1k | **≈ ₹1–1.5** |
| Bill page (effort medium) | ~2.5k per page | ~2–4k | **≈ ₹2.5–4** |
| Hindi name look-up (background) | tiny | ~0.3k | ≈ ₹0.3 |

What that means: a busy shop doing 40 plant scans a day for 26 days costs about **₹1,200 a month**, more than
any price below. Today's cap of 300 scans a day allows ~₹10,000 a month per shop. So:

- **Limits per plan are the first thing to build**, before payments.
- **Cut the cost per scan before launch.** Three levers, in order:
  1. **Try `claude-haiku-5-5` for plant photos** ($0.10 / $0.50, about 20× cheaper, ≈ ₹0.05–0.1 a scan). Run it on
     the test photos and the demo shop's pictures; keep Sonnet for bills, or as a second try when Haiku is unsure.
  2. **Match against the shop's own product photos first** (already in the plant-ID research notes). A repeat
     item then costs no AI call at all.
  3. **Prompt caching** for the fixed system prompt and the catalogue list.
- Log `response.usage` per call and per shop, so caps can be set from real numbers.

The caps below are safe on Sonnet for an average shop (most will use a fraction of the cap), and comfortable
for every shop once plant photos run on Haiku.

## 3. The tiers

Names follow the brand: quiet, plant-themed, English with Hindi.

| | **Seed (बीज)** | **Sapling (पौधा)** | **Tree (पेड़)** | **Orchard (बाग़)** |
|---|---|---|---|---|
| For | Trying it out, small roadside nursery | One shop, owner billing | Established nursery with staff | Large / wholesale / several outlets |
| Price, monthly | Free | ₹249 | ₹499 | ₹1,199 |
| Price, yearly (2 months free) | Free | **₹2,499** | **₹4,999** | **₹11,999** |
| Employees | Owner only | 1 | 5 | Unlimited |
| Products, customers, bills | 50 products, unlimited bills | Unlimited | Unlimited | Unlimited |
| AI plant photos / month | 20 | 100 | 300 | 1,000 |
| AI supplier bill pages / month | — | 10 | 50 | 200 |
| GST billing, UPI QR, WhatsApp bill | Yes | Yes | Yes | Yes |
| Shop logo and footer on the bill | "Made with Hara Hisaab" footer | Own logo | Own logo | Own logo |
| Hindi app and Hindi search | Yes | Yes | Yes | Yes |
| Reports | Today only | Day / week | Day / week / month, every item | All, plus export (to build) |
| Suppliers list, purchases | — | Yes | Yes | Yes |
| Employee permissions + performance report | — | — | Yes | Yes |
| Profit % pricing, "Bill without GST" | — | Yes | Yes | Yes |
| Order history kept | 3 months | Full | Full | Full |
| Support | Help pages | WhatsApp, 48 h | WhatsApp, same day | Named contact, setup done for them |

**Add-ons** (any paid plan): 200 extra plant photos ₹149; 50 extra bill pages ₹199; extra employee ₹79 a month;
**assisted setup** ₹999 once (we enter their catalogue and photos; free with a yearly Orchard plan).

**Trial:** every new shop gets **30 days of Tree**, then drops to Seed unless they pay. Nothing is deleted on
a downgrade; over-limit features become read-only.

**Launch offer:** the first 50 paying nurseries keep their yearly price for life ("founding nursery").

### Why these prices
- Generic GST billing apps sit around ₹2,500–4,000 a year for a phone-plus-desktop plan (sources below,
  unconfirmed). Sapling at ₹2,499 is at or under them; Tree at ₹4,999 is a premium justified by AI scans and
  staff controls.
- One saved hour a week of keying supplier bills is worth more than ₹400 a month to a nursery owner; that is the
  Tree pitch.
- Seed is generous enough to be useful (unlimited bills) but caps AI at ~₹30 a month of cost, and its bill
  footer advertises us on every WhatsApp bill.

### Gross margin check (per shop per month, Sonnet today vs. Haiku for plants)

| Plan | Revenue (yearly ÷ 12) | AI cost at full cap, Sonnet | At full cap, Haiku plants | At a typical 30% of cap, Sonnet |
|---|---|---|---|---|
| Seed | ₹0 | ₹30 | ₹2 | ₹9 |
| Sapling | ₹208 | ₹185 | ₹45 | ₹55 |
| Tree | ₹417 | ₹625 | ₹200 | ₹190 |
| Orchard | ₹1,000 | ₹2,200 | ₹780 | ₹660 |

(Full cap at ₹1.5 a plant photo and ₹3.5 a bill page on Sonnet; ₹0.08 a plant photo on Haiku.)

On Sonnet alone, Tree and Orchard lose money on shops that use their full allowance. With Haiku for plant
photos every plan is profitable even at full cap. **Do the Haiku test before announcing the caps.**

## 4. What has to be built

### Phase 0: measure and cut cost (about 1 week)
- Log tokens and cost per AI call, per shop (`ai_client._ask`).
- Haiku vs Sonnet on plant photos: accuracy on the test images; decide the model per call.
- Prompt caching for the system prompt and catalogue.

### Phase 1: plans and limits, paid by hand (about 2 weeks)
- `businesses.plan`, `plan_until`, `trial_until`; one `entitlements(business)` helper in the backend that every
  check reads, with the table above as data (not scattered `if`s).
- Monthly AI allowance per plan in `ai_usage` (plant photos and bill pages counted apart); keep the daily cap as
  an abuse guard. Add-on packs add to the month.
- Checks: employee count in `add_staff`, product count in `create_product`, supplier bill scan, monthly
  reports, employee report, logo on the bill, history window.
- App: "Your plan" screen in More (plan, renewal date, usage bars), friendly upgrade prompts where a limit is
  hit, all in Hindi too (`npm run i18n:check`).
- Admin: set a shop's plan and dates, see usage (the `ADMIN_PHONES` admin already exists for the library).
- Payment by UPI link or bank transfer, admin switches the plan on. Enough for the first 50 shops.

### Phase 2: self-serve payment (about 2 weeks)
- Razorpay Subscriptions (UPI AutoPay, cards) through a web checkout page, webhook sets the plan.
- Our own GST invoice for each payment.
- **Check first:** Google Play's payments policy may require Play Billing (or its user-choice billing in India)
  for subscriptions bought inside the app. Paying on a web page outside the app avoids the question while we
  distribute the APK; confirm before adding a "Buy" button inside the app.

### Before the first paid shop (needed whatever the tiers)
These are already open items in `docs/SESSION-NOTES.md` and `docs/QA-CHECKLIST.md`:
- Production still runs the old single-shop version (`main` on 19ca6f3); release the multi-shop version.
- A real domain instead of `sslip.io` (bill links go to customers).
- Forgot password for phone sign-ups (B.12); a sign-up limit so nobody makes 100 free shops (P.15).
- Privacy policy and terms (customer phone numbers are personal data under the DPDP Act), refund policy.
- Daily backups checked by a restore test (`deploy/backup.sh` exists).
- Talk to a CA about GST registration for the subscription income.

### Phase 3: what makes Orchard worth ₹1,199 (not built)
Pick by what pilot customers ask for: several outlets under one owner, Excel / PDF export for the accountant,
**udhaar (credit) and part payments**, wholesale price lists, thermal printer, offline billing (N.1).

## 5. How to sell it
- **Pilot:** 10–20 nurseries in one area on 30-day Tree trials, with the owner or a helper on WhatsApp. Watch
  scans per day and which limits they hit; adjust caps before Phase 2.
- **Demo:** the "Green House" demo shop already refreshes every night with today's dates; use it in every
  pitch (it shows full reports on day one).
- **Field sales:** nurseries cluster on the same roads; one visit can reach several. Show the plant photo and the
  bill photo; those two moments sell it.
- **Referral:** a free month for both shops when one brings another; the Seed bill footer does the rest.
- **Wholesale suppliers** who bill many nurseries are a channel: if their bills scan cleanly, they can recommend it.

## 6. Decisions for the owner
1. Free Seed plan forever, or only the 30-day trial?
2. The four prices (and whether to show monthly prices at all, or yearly only).
3. Run the Haiku test now, before the caps are announced?
4. Payment by hand for the pilot, then Razorpay?
5. Tier names: Seed / Sapling / Tree / Orchard, or plain Free / Basic / Pro / Business?

## Sources for competitor prices
- [taxaj.com: GST billing software for retailers 2026](https://www.taxaj.com/learn/best-gst-billing-software-indian-retailers-2026/)
- [Accountune: Vyapar vs MyBillBook 2026](https://accountune.com/vyapar-vs-accountune-vs-mybillbook)
- [Capterra: Vyapar vs myBillBook](https://www.capterra.com/compare/180579-202732/Vyapar-vs-FloBooks)
