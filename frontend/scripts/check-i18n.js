#!/usr/bin/env node
// Every text the app can show must have a Hindi translation in src/locales/hi.json.
// Run before each commit:  npm run i18n:check   (exit 1 on a missing text or a {{placeholder}} mismatch)
//   --missing   print the missing texts as JSON, ready to paste into hi.json and translate
//
// Texts come from t("…") / i18n.t("…") and plural(n, "…", "…") in app/ and src/, the few texts used through
// a variable (payment methods, suggested categories), and the server's error messages (the app shows them
// through i18n, with the English sentence as the key).
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const BACKEND = path.join(ROOT, "..", "backend");
const STR = String.raw`"((?:[^"\\\n]|\\.)*)"`;
const PY_STRS = String.raw`((?:"(?:[^"\\\n]|\\.)*"\s*)+)`; // Python: adjacent literals are joined

const keys = new Map(); // text -> where it was first seen
const add = (text, where) => text && !keys.has(text) && keys.set(text, where);
const unquote = (s) => JSON.parse(`"${s}"`);

function files(dir, exts) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((d) => {
    const p = path.join(dir, d.name);
    if (d.isDirectory()) return d.name === "node_modules" || d.name.startsWith(".") ? [] : files(p, exts);
    return exts.some((e) => d.name.endsWith(e)) ? [p] : [];
  });
}

// ---- the app
for (const file of [...files(path.join(ROOT, "app"), [".tsx", ".ts"]), ...files(path.join(ROOT, "src"), [".tsx", ".ts"])]) {
  const src = fs.readFileSync(file, "utf8");
  const where = path.relative(ROOT, file);
  for (const m of src.matchAll(new RegExp(String.raw`\bt\(\s*` + STR, "g"))) add(unquote(m[1]), where);
  for (const m of src.matchAll(new RegExp(String.raw`\bplural\([^,]+,\s*` + STR + String.raw`\s*,\s*` + STR, "g"))) {
    add(unquote(m[1]), where);
    add(unquote(m[2]), where);
  }
}

// ---- texts used through a variable: t(m) for payment methods, t(name) for suggested categories
for (const m of ["Cash", "UPI", "Card"]) add(m, "payment methods");
const business = fs.readFileSync(path.join(BACKEND, "routers", "business.py"), "utf8");
const block = business.match(/SUGGESTED_CATEGORIES = \[([\s\S]*?)\]/)[1];
for (const m of block.matchAll(/\("([^"]+)",/g)) add(m[1], "suggested categories");
// GET /business gst_warning: the shop's saved GSTIN is missing or invalid (WP-9)
const gstWarnings = business.match(/GST_WARNINGS = \{([\s\S]*?)\}/)[1];
for (const m of gstWarnings.matchAll(/:\s*"([^"]+)"/g)) add(m[1], "GST warnings");

// ---- server messages
const py = [...files(BACKEND, [".py"])].filter((f) => !f.includes(`${path.sep}tests${path.sep}`));
for (const file of py) {
  const src = fs.readFileSync(file, "utf8");
  // gstr.py's warning(code, text) notes on the GST returns (WP-10) are shown like AppError messages
  const re = new RegExp(String.raw`(?:HTTPException\(\d+,|AppError\(\d+,|BadImage\(|AIUnavailable\(|\bwarning\("\w+",)\s*` + PY_STRS, "g");
  for (const m of src.matchAll(re)) {
    const text = [...m[1].matchAll(new RegExp(STR, "g"))].map((s) => unquote(s[1])).join("");
    add(text, path.relative(path.join(ROOT, ".."), file));
  }
}

// ---- compare with hi.json
const hi = JSON.parse(fs.readFileSync(path.join(ROOT, "src", "locales", "hi.json"), "utf8"));
const holes = (s) => [...s.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort().join(",");
const missing = [...keys.keys()].filter((k) => !(k in hi) || !String(hi[k]).trim());
const mismatched = [...keys.keys()].filter((k) => k in hi && hi[k] && holes(k) !== holes(hi[k]));
const unused = Object.keys(hi).filter((k) => !keys.has(k));

if (process.argv.includes("--missing")) {
  console.log(JSON.stringify(Object.fromEntries(missing.map((k) => [k, ""])), null, 2));
  process.exit(0);
}
for (const k of missing) console.log(`missing   ${JSON.stringify(k)}   (${keys.get(k)})`);
for (const k of mismatched) console.log(`{{…}} differ   ${JSON.stringify(k)} -> ${JSON.stringify(hi[k])}`);
for (const k of unused) console.log(`unused (warning)   ${JSON.stringify(k)}`);
console.log(`${keys.size} texts, ${missing.length} missing, ${mismatched.length} placeholder mismatches, ${unused.length} unused`);
process.exit(missing.length || mismatched.length ? 1 : 0);
