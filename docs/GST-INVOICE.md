# GST and the tax invoice (WP-9)

**Status: pending CA sign-off (decision D7).** The layout, the HSN defaults and the rounding below are our reading
of CGST rules 46, 46A and 49. A practising CA must confirm them before paying GST shops rely on the bill. This note
lives in the docs only; nothing on the printed bill says "draft" or "pending".

Code: `backend/gst.py` (rules), `backend/invoice_html.py` (the bill page), `backend/routers/business.py` (shop
profile, categories, a product's HSN), `backend/migrations/0901_category_hsn.py`, tests in `backend/tests/test_gst.py`.

## What the bill shows

| Shop | Lines | Title | Tax details |
|---|---|---|---|
| Not GST registered | any | Bill (as before) | none |
| Regular | all taxed (rate above 0%) | Tax Invoice | per line and per HSN |
| Regular | none taxed (0% or exempt) | Bill of Supply | HSN and "Exempt" / "GST 0%" per line |
| Regular | both kinds | Invoice-cum-Bill of Supply (rule 46A) | per line and per HSN |
| Composition | any | Bill of Supply, with "Composition taxable person, not eligible to collect tax on supplies" | HSN only, no tax |
| Any | a "Bill without GST" bill | Bill (as before, decision D6 open) | none |

A tax invoice also shows: the shop's GSTIN and state, place of supply (the bill's own `place_of_supply`, else
the buyer's GSTIN state, else the shop's state), "Reverse charge: No", a table of taxable value and CGST + SGST (or
IGST when the place of supply is another state) by HSN and rate with totals, and "For <shop> / Authorised
signatory". Under each item: HSN, taxable value, GST rate and tax.

**The customer's link (`/i/{token}`)** shows the customer's phone masked (`+91 XXXXXX2334`) and their address only
when the bill needs it: a B2B bill (the bill has `customer_gstin`) or one of ₹50,000 or more. The shop's own print
(`/api/invoices/{id}/html`) shows both in full, as before.

## Numbers and rounding

`gst.split()` works on the tax-inclusive line amounts with `Decimal` and `ROUND_HALF_UP`:
- The bill's discount is spread over the lines in proportion to their amounts. The lines are then made to add up to
  the bill's saved total exactly (the last paisa goes on the biggest line), so **bill totals never change**.
- Same state: CGST = SGST = value × (rate/2) / (100 + rate), each rounded to the paisa, so they are always equal.
  Other state: IGST = value × rate / (100 + rate), rounded. Taxable value = line value − tax.
- Rate totals and HSN totals are sums of the rounded lines, so lines, rate totals and HSN totals agree to the paisa
  (tested on 400 random bills, same state and other state).

For the CA: taxable × rate can differ from the tax by up to a paisa per line because the tax is worked back from
the inclusive price. Please confirm this is acceptable, or that tax should be computed on a rounded taxable value.

## Suggested HSN codes (owner and CA to confirm)

Categories get a suggestion from their name; the owner can change it, and a product can have its own code.

| Category name contains | HSN | Exempt by default |
|---|---|---|
| seed | 1209 (seeds for sowing) | yes, at 0% |
| pot, planter, gamla | 6914 (ceramic / clay; plastic pots are 3924) | no |
| fertili, manure, compost, khad | 3101 (organic; mineral fertilisers are 3105) | no |
| soil, mitti, cocopeat, potting mix, media | 2530 (also 3824 for prepared media) | no |
| tool | 8201 | no |
| plant, tree, shrub, herb, succulent, cact, flower, bonsai, sapling, creeper, palm, grass, fern, bulb | 0602 (live plants) | yes, at 0% |

"Exempt" only ever applies at 0% GST: an exempt category with a GST rate is refused, and giving an exempt category
a rate ends the exemption.

## The shop's tax profile

- `gst_registered` plus `gst_scheme` (`regular`, the default, or `composition`). State from the GSTIN.
- **GSTIN check:** format (state code, PAN, entity number, "Z") plus the mod-36 check character. A valid GSTIN is
  needed to turn GST on or to save a new GSTIN (`400 gstin_missing` / `400 bad_gstin`, an `AppError` with the GSTIN
  as a parameter).
- **Shops saved before the check are not locked out:** the same, already-saved GSTIN (or a missing one) is never
  refused, so the Shop details form still saves the shop's other settings. `GET /api/business` returns
  `gstin_status` (`not_registered`, `ok`, `missing`, `invalid`) and `gst_warning` (an English sentence with a
  Hindi entry in `hi.json`) so the app can ask the owner to fix it.
- Changes to the tax profile, to a category's rate, HSN or exemption, and to a product's HSN go to the audit log.

## API (all additive; 1.1.0 and 1.2.0 keep working)

- `GET /api/business`: new `gst_scheme`, `gstin_status`, `gst_warning`, `gst_state_code`, `gst_state`.
- `PUT /api/business`: optional `gst_scheme`; GSTIN rules above; `logo_path` must be one of the shop's own uploads
  (the saved one and an empty one always pass).
- Categories: optional `hsn` and `tax_exempt` on create, bulk and patch; responses and `GET /api/categories`
  include them (a category saved before shows its suggestion). `GET /api/categories/suggestions` adds `hsn` and
  `tax_exempt`. `GET /api/categories` counts products in one aggregation.
- New `GET /api/products/{id}/tax` (any user) and `PUT /api/products/{id}/tax` `{hsn}` (owner; `""` = the
  category's code).

## Decision D6: "Bill without GST" is unchanged

`gst_removed` bills still collect the full price and print as a plain "Bill" with no tax (`gst.title()` and the
comment in `invoice_html.render`). Nothing here removes or changes the toggle. When the owner decides D6, change
`gst.title()` and `sales.py` together, and ask the CA to review the existing `gst_removed` bills.

## Wave 2 hooks (for the owner of `sales.py`)

Steps 1 and 2 were done at integration (2 Nov 2026): new bills carry `hsn` and `exempt` per line, and older bills
print today's codes. Step 3 was done in the review fixes (3 Nov 2026). Step 4 remains.

1. Done. `_lines()`: add to each line `"hsn": gst.line_hsn(p, cat)` and
   `"exempt": bool((cat or {}).get("tax_exempt")) and not line["gst_percent"]` (frozen on the bill, like the rate).
2. Done. `invoice_page_for_print` and `public_bill`: `codes = await gst.bill_codes(db, inv)` and pass
   `codes=codes` to `invoice_html.render(...)`, so bills saved before step 1 print today's HSN codes.
3. Done. `price_lines()` splits the bill with `gst.split(lines, discount, total=grand_total, inter_state=...)` and
   saves each line's `net`, `tax` and `cgst`/`sgst`/`igst`, the `gst_breakup` as their sums, and `inter_state` on
   the bill. A composition shop's lines are saved at 0% (`_lines`). The print (`gst.split_bill`) and GSTR-1
   (`gstr.py`) read the saved amounts through `gst.saved_split`, so print, app, exports and returns agree to the
   paisa. Bills saved before keep their one rounded tax (CGST gets the odd paisa). The GSTR exports refuse
   composition shops (`gst_composition`): they file CMP-08 and GSTR-4. `inter_state` is false until step 4.
4. Optional buyer fields on `InvoiceIn`: `customer_gstin` (validated with `gst.valid_gstin`) and `place_of_supply`
   (state code). The bill page already uses them when present.
