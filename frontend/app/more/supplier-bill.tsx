// Supplier bill: photos of the bill's pages (up to 5, each uploaded as soon as it is added) → the items are
// read from all pages together → review them and the supplier → stock is added. Items that match a
// product add to its stock; the rest become new products. Pages from different suppliers or different
// bills are refused, so one scan is always one bill.
// Never added twice: every try of "Add to stock" on this screen sends the same Idempotency-Key, and the server
// answers a key it has seen with the supplier bill it already saved. After no answer (or a busy server that may
// have saved it) the screen stays and the worker taps again; if the bill was changed meanwhile and the first try
// had been saved after all, the server says so (idempotency_mismatch) and the screen closes.
import { useRef, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, View } from "react-native";
import { Image } from "expo-image";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import * as Crypto from "expo-crypto";
import { useTranslation } from "react-i18next";

import { ApiError, apiPost, noReply, serverBusy, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { choose, notice } from "@/src/confirm";
import { SupplierChoice, SupplierPicker } from "@/src/components/supplier-picker";
import { useToast } from "@/src/components/toast";
import { Badge, BottomBar, Button, Field, Header, Icon, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatPhone, plural } from "@/src/format";
import { CaptureResult, pickPages, takePhoto } from "@/src/hooks/useImageCapture";
import { colors, radius, spacing } from "@/src/theme";

type Item = { key: string; product_id: string | null; matched_name: string | null; name: string; quantity: string; purchase_price: string };
type Page = { key: string; uri: string; base64: string; path: string | null; state: "uploading" | "done" | "failed" };
const num = (v: string) => v.replace(/[^0-9.]/g, "");
const blank = (key: string): Item => ({ key, product_id: null, matched_name: null, name: "", quantity: "1", purchase_price: "" });
const MAX_PAGES = 5;
const NO_SUPPLIER: SupplierChoice = { id: null, name: "", phone: "", address: "", gstin: "" };

export default function SupplierBill() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { user } = useAuth();
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
      setItems(r.items.map((it: any, i: number) => ({ key: `${i}`, product_id: it.product_id, matched_name: it.matched_name, name: it.name,
        quantity: String(it.quantity || 1), purchase_price: String(it.purchase_price || "") })));
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
        items: valid.map((i) => ({ product_id: i.product_id, name: i.name.trim(), quantity: Number(i.quantity), purchase_price: Number(i.purchase_price) || 0 })),
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
              <Field style={{ flex: 1 }} label={t("Cost ₹ each")} value={it.purchase_price} onChangeText={(v) => set(it.key, { purchase_price: num(v) })} keyboardType="decimal-pad" />
            </View>
          </View>
        ))}
        <Button title={t("Add an item")} icon="plus" variant="dashed" size="md"
          onPress={() => setItems((l) => [...l!, blank(`m${Date.now()}`)])} />
        <T v="small">{t("New products are priced at cost plus {{p}}%. You can change any price later in Products.", { p: user?.business?.profit_percent ?? 60 })}</T>
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

function PageThumb({ page, n, onRemove, onRetry }: { page: Page; n: number; onRemove: () => void; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <View testID={`sb-page-${n}`} style={{ width: 96, gap: 6 }}>
      <View style={{ width: 96, height: 120, borderRadius: radius.md, overflow: "hidden", backgroundColor: colors.page, borderWidth: 1, borderColor: colors.line }}>
        <Image source={{ uri: page.uri }} style={{ width: "100%", height: "100%" }} contentFit="cover" />
        {page.state !== "done" && (
          <Pressable disabled={page.state !== "failed"} onPress={onRetry} accessibilityLabel={t("Try again")}
            style={{ position: "absolute", left: 0, top: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center",
              backgroundColor: "rgba(14,26,20,0.45)" }}>
            {page.state === "uploading" ? <ActivityIndicator color={colors.white} /> : <Icon name="refresh" size={30} color={colors.white} />}
          </Pressable>
        )}
        <Pressable onPress={onRemove} accessibilityLabel={t("Remove page {{n}}", { n })} hitSlop={8}
          style={{ position: "absolute", right: 4, top: 4, width: 30, height: 30, borderRadius: 15, backgroundColor: colors.surface,
            alignItems: "center", justifyContent: "center" }}>
          <Icon name="close" size={18} color={colors.ink} />
        </Pressable>
      </View>
      <T v="caption" style={{ textAlign: "center" }}>
        {page.state === "failed" ? t("Upload failed") : t("Page {{n}}", { n })}
      </T>
    </View>
  );
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
