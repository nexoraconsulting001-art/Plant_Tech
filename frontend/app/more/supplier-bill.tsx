// Supplier bill: photos of the bill's pages (up to 5, each uploaded as soon as it is added) → the items are
// read from all pages together → review them and the supplier → stock is added. Items that match a
// product add to its stock; the rest become new products. Pages from different suppliers or different
// bills are refused, so one scan is always one bill.
// Never added twice: every try of "Add to stock" on this screen sends the same Idempotency-Key, and the server
// answers a key it has seen with the supplier bill it already saved. After no answer (or a busy server that may
// have saved it) the screen stays and the worker taps again; if the bill was changed meanwhile and the first try
// had been saved after all, the server says so (idempotency_mismatch) and the screen closes.
import { useRef, useState } from "react";
import { Pressable, ScrollView, View } from "react-native";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import * as Crypto from "expo-crypto";
import { useTranslation } from "react-i18next";

import { ApiError, apiPost, noReply, serverBusy, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { choose, notice } from "@/src/confirm";
import { Page, PageThumb } from "@/src/components/bill-pages";
import { SupplierChoice, SupplierPicker } from "@/src/components/supplier-picker";
import { useToast } from "@/src/components/toast";
import { Badge, BottomBar, Button, Field, Header, Icon, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatPhone, plural } from "@/src/format";
import { CaptureResult, pickPages, takePhoto } from "@/src/hooks/useImageCapture";
import { colors, radius, spacing } from "@/src/theme";

// selling_price: typed on the bill (owner, 10 Nov 2026). A new item starts at cost plus the shop's profit % and follows
// the cost until it is typed (priced); a product already in the shop starts at its current price, and a changed one
// becomes its price.
type Item = {
  key: string; product_id: string | null; matched_name: string | null; name: string; quantity: string; purchase_price: string;
  selling_price: string; current_price: number | null; priced: boolean;
};
const num = (v: string) => v.replace(/[^0-9.]/g, "");
const blank = (key: string): Item => ({ key, product_id: null, matched_name: null, name: "", quantity: "1", purchase_price: "",
  selling_price: "", current_price: null, priced: false });
const MAX_PAGES = 5;
const NO_SUPPLIER: SupplierChoice = { id: null, name: "", phone: "", address: "", gstin: "" };

export default function SupplierBill() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { user } = useAuth();
  const profit = user?.business?.profit_percent ?? 60;
  const gst = !!user?.business?.gst_registered;
  const withMarkup = (cost: string) => (Number(cost) > 0 ? String(Math.round(Number(cost) * (1 + profit / 100))) : "");
  const [pages, setPages] = useState<Page[]>([]);
  const [reading, setReading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [supplier, setSupplier] = useState<SupplierChoice>(NO_SUPPLIER);
  const [picking, setPicking] = useState(false);
  const [number, setNumber] = useState("");
  const [date, setDate] = useState("");
  const [items, setItems] = useState<Item[] | null>(null);
  const saveKey = useRef<string | null>(null); // this supplier bill's Idempotency-Key, kept until it is saved

  const BILL = { hint: t("Drag the corners to fit the bill") };
  const uploading = pages.some((p) => p.state === "uploading");
  const failed = pages.some((p) => p.state === "failed");

  function upload(page: Page) {
    uploadImage(page.base64)
      .then((path) => setPages((list) => list.map((p) => (p.key === page.key ? { ...p, path, state: "done" } : p))))
      .catch(() => setPages((list) => list.map((p) => (p.key === page.key ? { ...p, state: "failed" } : p))));
  }

  function addPages(photos: CaptureResult[]) {
    const added = photos.slice(0, MAX_PAGES - pages.length).map((ph, i): Page =>
      ({ key: `${Date.now()}-${i}`, uri: ph.uri, base64: ph.base64, path: null, state: "uploading" }));
    setPages((list) => [...list, ...added]);
    added.forEach(upload);
  }

  async function addFrom(source: "camera" | "gallery") {
    const out = source === "camera" ? await takePhoto(BILL) : await pickPages(MAX_PAGES - pages.length, BILL);
    if (out.status === "denied") toast.show(t("Please allow camera and photo access for Hara Hisaab in your phone settings."), "error");
    if (out.status !== "ok") return;
    addPages("results" in out ? out.results : [out.result]);
  }

  async function read() {
    setReading(true);
    try {
      const r = await apiPost("/ai/scan-invoice", { image_paths: pages.map((p) => p.path) });
      const m = r.supplier_match;
      setSupplier(m ? { id: m.id, name: m.name, phone: m.phone, address: m.address, gstin: m.gstin }
        : { id: null, name: r.supplier || "", phone: r.supplier_phone || "", address: r.supplier_address || "", gstin: r.supplier_gstin || "" });
      setNumber(r.invoice_number || "");
      setDate(r.date || "");
      setItems(r.items.map((it: any, i: number) => {
        const cost = String(it.purchase_price || "");
        const current: number | null = it.product_id && it.current_price != null ? it.current_price : null;
        return { key: `${i}`, product_id: it.product_id, matched_name: it.matched_name, name: it.name, quantity: String(it.quantity || 1),
          purchase_price: cost, current_price: current, priced: !!it.product_id,
          selling_price: it.product_id ? (current != null ? String(current) : "") : withMarkup(cost) };
      }));
      if (!r.items.length) toast.show(t("No items could be read from this bill. Please add them below."), "info");
    } catch (e: any) {
      await refused(e);
    } finally {
      setReading(false);
    }
  }

  // The pages could not be read as one bill: explain, keep the pages so the wrong one can be removed.
  async function refused(e: any) {
    const code = e instanceof ApiError ? e.code : "";
    if (code === "not_a_bill") {
      const pick = await choose(t("Not a supplier bill"), e.message, t("Retake photo"), t("Enter items manually"));
      if (pick === "yes") {
        setPages([]);
        setTimeout(() => addFrom("camera"), 0);
      } else if (pick === "no") {
        setItems([blank("m0")]);
      }
      return;
    }
    const titles: Record<string, string> = {
      bill_page_not_bill: t("Page not recognised"), bill_suppliers_differ: t("Different suppliers"),
      bill_numbers_differ: t("Different bills"), too_many_pages: t("Too many pages"),
    };
    if (titles[code]) await notice(titles[code], e.message);
    else toast.show(e.message, "error");
  }

  const set = (key: string, patch: Partial<Item>) => setItems((list) => list!.map((i) => (i.key === key ? { ...i, ...patch } : i)));
  // A new item's selling price follows its cost until it is typed.
  const setCost = (it: Item, v: string) => set(it.key, { purchase_price: v, ...(it.priced ? {} : { selling_price: withMarkup(v) }) });
  // Sent only when there is one, and for a product already in the shop only when it changes its price.
  const sellingPrice = (i: Item) => (i.selling_price === "" || (i.product_id && Number(i.selling_price) === i.current_price)
    ? undefined : Number(i.selling_price));
  const valid = (items ?? []).filter((i) => i.name.trim() && Number(i.quantity) > 0);
  const total = valid.reduce((s, i) => s + Number(i.quantity) * (Number(i.purchase_price) || 0), 0);

  function saved() {
    qc.invalidateQueries({ queryKey: ["products"] });
    qc.invalidateQueries({ queryKey: ["suppliers"] });
    router.back();
  }

  async function save() {
    setSaving(true);
    if (!saveKey.current) saveKey.current = Crypto.randomUUID();
    try {
      await apiPost("/purchases", {
        supplier: supplier.name.trim(), supplier_id: supplier.id, supplier_phone: supplier.phone, supplier_address: supplier.address,
        supplier_gstin: supplier.gstin, invoice_number: number, date, total,
        image_paths: pages.filter((p) => p.path).map((p) => p.path),
        items: valid.map((i) => ({ product_id: i.product_id, name: i.name.trim(), quantity: Number(i.quantity), purchase_price: Number(i.purchase_price) || 0,
          selling_price: sellingPrice(i) })),
      }, { headers: { "Idempotency-Key": saveKey.current } });
      toast.show(t("Stock added for {{items}}", { items: plural(valid.length, "1 item", "{{n}} items") }), "success");
      saved();
    } catch (e: any) {
      if (e instanceof ApiError && e.code === "idempotency_mismatch") {
        await notice(t("Already saved"), e.message); // an earlier try was saved: its stock is in, these changes are not
        saved();
      } else if (noReply(e) || (serverBusy(e) && (e.code !== "busy" || e.maybeDone))) {
        await notice(noReply(e) ? t("No answer from the server") : t("The server is busy"),
          t("The stock may or may not have been added. Tap Add to stock again: it will not be added twice."));
      } else toast.show(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  if (!items) {
    return (
      <Screen bg={colors.surface}>
        <Header title={t("Supplier bill")} />
        {!pages.length ? (
          <View style={{ flex: 1, padding: spacing.lg, gap: spacing.lg, justifyContent: "center" }}>
            <View style={{ alignItems: "center", gap: spacing.md, marginBottom: spacing.md }}>
              <View style={{ width: 96, height: 96, borderRadius: 48, backgroundColor: colors.blueTint, alignItems: "center", justifyContent: "center" }}>
                <Icon name="file-document-outline" size={50} color={colors.blue} />
              </View>
              <T v="title" style={{ textAlign: "center" }}>{t("Scan a supplier bill")}</T>
              <T v="body" color={colors.inkSoft} style={{ textAlign: "center" }}>
                {t("Photograph each page of the bill. The items are read from all pages together; review them, then add them to stock.")}
              </T>
            </View>
            <Button title={t("Take photo")} icon="camera" variant="scan" onPress={() => addFrom("camera")} testID="sb-camera" />
            <Button title={t("Pick from gallery")} icon="image-outline" variant="outline" size="md" onPress={() => addFrom("gallery")} testID="sb-gallery" />
            <Pressable onPress={() => setItems([blank("m0")])} style={{ minHeight: 48, alignItems: "center", justifyContent: "center" }}>
              <T v="bold" color={colors.green}>{t("Enter items manually")}</T>
            </Pressable>
          </View>
        ) : (
          <>
            <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
              <T v="label">{t("Pages of this bill ({{n}} of {{max}})", { n: pages.length, max: MAX_PAGES })}</T>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.md }}>
                {pages.map((p, i) => (
                  <PageThumb key={p.key} page={p} n={i + 1} onRemove={() => setPages((l) => l.filter((x) => x.key !== p.key))}
                    onRetry={() => { setPages((l) => l.map((x) => (x.key === p.key ? { ...x, state: "uploading" } : x))); upload(p); }} />
                ))}
              </View>
              {pages.length < MAX_PAGES && (
                <View style={{ flexDirection: "row", gap: spacing.md }}>
                  <Button title={t("Add page")} icon="camera-plus-outline" variant="outline" size="md" style={{ flex: 1 }}
                    onPress={() => addFrom("camera")} disabled={reading} testID="sb-add-page" />
                  <Button title={t("From gallery")} icon="image-plus" variant="outline" size="md" style={{ flex: 1 }}
                    onPress={() => addFrom("gallery")} disabled={reading} testID="sb-add-gallery" />
                </View>
              )}
              <T v="small">{t("Add every page of one bill from one supplier. Pages of different bills are not accepted together.")}</T>
            </ScrollView>
            <BottomBar>
              <Button title={reading ? t("Reading the bill") : pages.length > 1 ? t("Read {{n}} pages", { n: pages.length }) : t("Read the bill")}
                icon="text-recognition" onPress={read} loading={reading} disabled={uploading || failed} testID="sb-read" />
            </BottomBar>
          </>
        )}
      </Screen>
    );
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Review items")} onBack={() => setItems(null)} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }} bottomOffset={24}>
        <SupplierCard value={supplier} onChange={setSupplier} onPick={() => setPicking(true)} />
        <View style={{ flexDirection: "row", gap: spacing.md }}>
          <Field style={{ flex: 1 }} label={t("Bill no.")} value={number} onChangeText={setNumber} />
          <Field style={{ flex: 1 }} label={t("Bill date")} value={date} onChangeText={setDate} placeholder="12/10/2026" />
        </View>
        {items.map((it) => (
          <View key={it.key} style={{ gap: spacing.sm, padding: spacing.md, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              {it.product_id ? <Badge text={t("Adds to {{name}}", { name: it.matched_name })} tone="green" /> : <Badge text={t("New product")} tone="blue" />}
              <View style={{ flex: 1 }} />
              <Pressable accessibilityLabel={t("Remove")} onPress={() => setItems((l) => l!.filter((x) => x.key !== it.key))}
                style={{ width: 44, height: 44, alignItems: "center", justifyContent: "center" }}>
                <Icon name="trash-can-outline" size={24} color={colors.red} />
              </Pressable>
            </View>
            <Field value={it.name} onChangeText={(name) => set(it.key, { name })}
              placeholder={t("Item name")} autoCapitalize="words" />
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <Field style={{ flex: 1 }} label={t("Quantity")} value={it.quantity} onChangeText={(v) => set(it.key, { quantity: num(v) })} keyboardType="decimal-pad" />
              <Field style={{ flex: 1 }} label={t("Cost ₹ each")} value={it.purchase_price} onChangeText={(v) => setCost(it, num(v))} keyboardType="decimal-pad" />
            </View>
            <Field label={gst ? t("Selling price ₹ (includes GST)") : t("Selling price ₹")} value={it.selling_price}
              onChangeText={(v) => set(it.key, { selling_price: num(v), priced: true })} keyboardType="decimal-pad"
              placeholder={it.product_id ? t("Unchanged") : "0"} testID={`sb-price-${it.name}`} />
            <PriceNote it={it} profit={profit} />
          </View>
        ))}
        <Button title={t("Add an item")} icon="plus" variant="dashed" size="md"
          onPress={() => setItems((l) => [...l!, blank(`m${Date.now()}`)])} />
        <T v="small">{t("Selling prices start at cost plus {{p}}% for new products. A changed price for a product you already have becomes its price.", { p: profit })}</T>
      </KeyboardAwareScrollView>
      <BottomBar>
        <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
          <T v="label">{plural(valid.length, "1 item", "{{n}} items")}</T>
          <T v="bold">{formatCurrency(total)}</T>
        </View>
        <Button title={t("Add to stock")} icon="package-variant-plus" onPress={save} loading={saving} disabled={!valid.length} testID="sb-save" />
      </BottomBar>
      <SupplierPicker visible={picking} onClose={() => setPicking(false)} selectedId={supplier.id}
        onPick={(s) => setSupplier(s.id ? s : { ...supplier, id: null, name: s.name })} />
    </Screen>
  );
}

// Under the selling price: "Cost plus 60%" while a new item's price follows its cost; for a product already in the
// shop, "Current selling price", or which price a typed one replaces.
function PriceNote({ it, profit }: { it: Item; profit: number }) {
  const { t } = useTranslation();
  if (!it.product_id) return !it.priced && it.selling_price !== "" ? <T v="caption">{t("Cost plus {{p}}%", { p: profit })}</T> : null;
  if (it.current_price == null) return null;
  const changed = it.selling_price !== "" && Number(it.selling_price) !== it.current_price;
  return changed ? (
    <T v="caption" color={colors.amberInk} testID={`sb-price-change-${it.name}`}>
      {t("Changes the price of {{name}} from {{old}}", { name: it.matched_name, old: formatCurrency(it.current_price) })}
    </T>
  ) : <T v="caption">{t("Current selling price")}</T>;
}

// The supplier of this bill: a saved one, or a new one with the details read from the bill (editable).
function SupplierCard({ value, onChange, onPick }: { value: SupplierChoice; onChange: (s: SupplierChoice) => void; onPick: () => void }) {
  const { t } = useTranslation();
  if (value.id) {
    return (
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.greenTint }}>
        <Icon name="truck-outline" size={26} color={colors.green} />
        <View style={{ flex: 1, minWidth: 0 }}>
          <T v="label" color={colors.greenDark}>{t("Supplier")}</T>
          <T v="bold">{value.name}</T>
          {!!(value.phone || value.gstin) && <T v="small">{[formatPhone(value.phone), value.gstin].filter(Boolean).join(" · ")}</T>}
        </View>
        <Pressable onPress={onPick} hitSlop={8} testID="sb-supplier-change">
          <T v="bold" color={colors.green}>{t("Change")}</T>
        </Pressable>
      </View>
    );
  }
  return (
    <View style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
        <T v="label" style={{ flex: 1 }}>{t("Supplier")}</T>
        {!!value.name && <Badge text={t("New supplier")} tone="blue" />}
      </View>
      <Field value={value.name} onChangeText={(name) => onChange({ ...value, name })} placeholder={t("Supplier name")}
        autoCapitalize="words" testID="sb-supplier-name" />
      {(!!value.name || !!value.phone) && (
        <Field value={value.phone} onChangeText={(phone) => onChange({ ...value, phone })} placeholder={t("Phone (optional)")}
          keyboardType="phone-pad" testID="sb-supplier-phone" />
      )}
      <Pressable onPress={onPick} hitSlop={8} style={{ minHeight: 40, justifyContent: "center" }} testID="sb-supplier-pick">
        <T v="bold" color={colors.green}>{t("Choose a saved supplier")}</T>
      </Pressable>
    </View>
  );
}
