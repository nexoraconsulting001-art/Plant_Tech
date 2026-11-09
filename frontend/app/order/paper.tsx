// Upload order details (owner, 10 Nov 2026): a customer's order written on paper → photos of its pages (up to 5,
// each uploaded as soon as it is added) → the lines are read and matched to the shop's products (POST
// /ai/scan-order) → check them here → "Add N items to the order" adds them to the order being built and opens it.
// Each line: what was written, the product it was matched to (tap to change: the other close products, or a search),
// quantity and price. The price is the one written on the paper when there is one (owner's choice; the order shows
// the shop's price crossed out next to it), else the shop's price. A line without a product gets one picked, is sold
// on this bill only, or is removed. Stock rules as on the order screen: the owner may sell more than the stock count
// after a warning; an employee may not.
// Opened from the scan screen ("Upload order details"), or from its "This looks like a bill" answer with that photo
// as the first page (src/pending-photo.ts).
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, View } from "react-native";
import { Redirect, useRouter } from "expo-router";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { ApiError, apiGet, apiPost, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useCart } from "@/src/cart-context";
import { Page, PageThumb } from "@/src/components/bill-pages";
import { Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Badge, BottomBar, Button, Field, Header, Icon, Screen, SearchBox, T, Thumb } from "@/src/components/ui";
import { choose, notice } from "@/src/confirm";
import { formatCurrency, formatQtyUnit, plural } from "@/src/format";
import { CaptureResult, pickPages, takePhoto } from "@/src/hooks/useImageCapture";
import { takePendingPhoto } from "@/src/pending-photo";
import { askOwner, shortWarning } from "@/src/stock";
import { colors, radius, spacing } from "@/src/theme";
import type { Product } from "@/src/types";
import { roundQty, unitShort, wholeUnit } from "@/src/units";

const MAX_PAGES = 5;
const SEARCH_AFTER_MS = 250;

// paper: the price written on the paper (0 = none). loose: sold on this bill only, without a product.
type Line = {
  key: string; read: string; name: string; quantity: string; price: string; paper: number;
  product: Product | null; others: Product[]; loose: boolean;
};
const num = (v: string) => v.replace(/[^0-9.]/g, "");
const positive = (v: string) => Number.isFinite(Number(v)) && Number(v) > 0;
const priceOk = (v: string) => v !== "" && Number.isFinite(Number(v)) && Number(v) >= 0;
const looseId = (i: number) => `loose-${Date.now().toString(36)}${i}`; // a line sold on this bill only

export default function PaperOrder() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const cart = useCart();
  const { isOwner } = useAuth();
  const [openedWithout] = useState(() => !cart.customer && !cart.correcting); // a reload: pick the customer first
  const [first] = useState(takePendingPhoto); // the scan screen's photo of the bill, if it sent one
  const [pages, setPages] = useState<Page[]>([]);
  const [reading, setReading] = useState(false);
  const [lines, setLines] = useState<Line[] | null>(null);
  const [changing, setChanging] = useState<string | null>(null); // the line whose product is being changed

  const PAGE = { hint: t("Drag the corners to fit the page") };
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

  const started = useRef(false);
  useEffect(() => {
    if (first && !started.current) addPages([first]);
    started.current = true;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function addFrom(source: "camera" | "gallery") {
    const out = source === "camera" ? await takePhoto(PAGE) : await pickPages(MAX_PAGES - pages.length, PAGE);
    if (out.status === "denied") toast.show(t("Please allow camera and photo access for Hara Hisaab in your phone settings."), "error");
    if (out.status !== "ok") return;
    addPages("results" in out ? out.results : [out.result]);
  }

  async function read() {
    setReading(true);
    try {
      const r = await apiPost("/ai/scan-order", { image_paths: pages.map((p) => p.path) });
      const read: Line[] = r.items.map((it: any, i: number) => {
        const product: Product | null = it.matched_product ?? null;
        const paper = Number(it.price) > 0 ? Number(it.price) : 0;
        const qty = product && wholeUnit(product.unit) ? Math.max(1, Math.round(it.quantity)) : it.quantity;
        return { key: `${i}`, read: it.name, name: it.name, quantity: String(qty), paper,
          price: paper ? String(paper) : product ? String(product.selling_price) : "",
          product, others: it.other_products ?? [], loose: false };
      });
      if (!read.length) {
        await notice(t("No items read"), t("No items could be read from these pages. Retake the photo, or add the items on the scan screen."));
        return;
      }
      setLines(read);
    } catch (e: any) {
      await refused(e);
    } finally {
      setReading(false);
    }
  }

  async function refused(e: any) {
    const code = e instanceof ApiError ? e.code : "";
    if (code === "not_an_order") {
      const pick = await choose(t("Not an order"), e.message, t("Retake photo"), t("Search by name"));
      if (pick === "yes") {
        setPages([]);
        setTimeout(() => addFrom("camera"), 0);
      } else if (pick === "no") router.back();
      return;
    }
    const titles: Record<string, string> = { too_many_pages: t("Too many pages"), order_too_long: t("Order too long") };
    if (titles[code]) await notice(titles[code], e.message);
    else toast.show(e.message, "error");
  }

  const set = (key: string, patch: Partial<Line>) => setLines((list) => list!.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  const pick = (key: string, p: Product) => {
    setLines((list) => list!.map((l) => {
      if (l.key !== key) return l;
      const qty = wholeUnit(p.unit) ? String(Math.max(1, Math.round(Number(l.quantity) || 1))) : l.quantity;
      // the one shown before joins the other choices
      const others = [...(l.product && l.product.id !== p.id ? [l.product] : []), ...l.others.filter((o) => o.id !== p.id)];
      return { ...l, product: p, others, loose: false, quantity: qty, price: l.paper ? String(l.paper) : String(p.selling_price) };
    }));
    setChanging(null);
  };

  if (openedWithout) return <Redirect href="/order/customer" />;

  if (!lines) {
    return (
      <Screen bg={colors.surface}>
        <Header title={t("Upload order details")} />
        {!pages.length ? (
          <View style={{ flex: 1, padding: spacing.lg, gap: spacing.lg, justifyContent: "center" }}>
            <View style={{ alignItems: "center", gap: spacing.md, marginBottom: spacing.md }}>
              <View style={{ width: 96, height: 96, borderRadius: 48, backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
                <Icon name="file-document-outline" size={50} color={colors.green} />
              </View>
              <T v="title" style={{ textAlign: "center" }}>{t("Read a written order")}</T>
              <T v="body" color={colors.inkSoft} style={{ textAlign: "center" }}>
                {t("Photograph the customer's order on paper. The items are read and matched to your products; check them, then add them to the order.")}
              </T>
            </View>
            <Button title={t("Take photo")} icon="camera" variant="scan" onPress={() => addFrom("camera")} testID="po-camera" />
            <Button title={t("Pick from gallery")} icon="image-outline" variant="outline" size="md" onPress={() => addFrom("gallery")} testID="po-gallery" />
          </View>
        ) : (
          <>
            <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
              <T v="label">{t("Pages of this order ({{n}} of {{max}})", { n: pages.length, max: MAX_PAGES })}</T>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.md }}>
                {pages.map((p, i) => (
                  <PageThumb key={p.key} page={p} n={i + 1} testPrefix="po-page" onRemove={() => setPages((l) => l.filter((x) => x.key !== p.key))}
                    onRetry={() => { setPages((l) => l.map((x) => (x.key === p.key ? { ...x, state: "uploading" } : x))); upload(p); }} />
                ))}
              </View>
              {pages.length < MAX_PAGES && (
                <View style={{ flexDirection: "row", gap: spacing.md }}>
                  <Button title={t("Add page")} icon="camera-plus-outline" variant="outline" size="md" style={{ flex: 1 }}
                    onPress={() => addFrom("camera")} disabled={reading} testID="po-add-page" />
                  <Button title={t("From gallery")} icon="image-plus" variant="outline" size="md" style={{ flex: 1 }}
                    onPress={() => addFrom("gallery")} disabled={reading} testID="po-add-gallery" />
                </View>
              )}
              <T v="small">{t("Add every page of this customer's order. Each page counts as one scan.")}</T>
            </ScrollView>
            <BottomBar>
              <Button title={reading ? t("Reading the order") : pages.length > 1 ? t("Read {{n}} pages", { n: pages.length }) : t("Read the order")}
                icon="text-recognition" onPress={read} loading={reading} disabled={uploading || failed || !pages.length} testID="po-read" />
            </BottomBar>
          </>
        )}
      </Screen>
    );
  }

  // How much of each product the order would have, with what is already on it, for the stock check.
  const want = new Map<string, number>();
  for (const it of cart.items) want.set(it.product_id, it.quantity);
  for (const l of lines) if (l.product && positive(l.quantity)) want.set(l.product.id, (want.get(l.product.id) ?? 0) + Number(l.quantity));
  const stockOf = (p: Product) => roundQty(p.stock + cart.returning(p.id), false);
  const shortOf = (p: Product) => (want.get(p.id) ?? 0) > stockOf(p) + 1e-9;

  const ready = lines.filter((l) => (l.product || (l.loose && l.name.trim())) && positive(l.quantity) && priceOk(l.price));
  const open = lines.length - ready.length; // lines still without a product, quantity or price
  const blocked = !isOwner && ready.some((l) => l.product && shortOf(l.product)); // an employee can't sell what isn't in stock
  const total = ready.reduce((s, l) => s + Number(l.quantity) * Number(l.price), 0);

  function addAll() {
    ready.forEach((l, i) => {
      const p = l.product;
      if (p) {
        const had = cart.items.some((it) => it.product_id === p.id);
        cart.addItem({ product_id: p.id, name: p.name, price: Number(l.price), list_price: p.selling_price, quantity: Number(l.quantity),
          gst_percent: p.gst_percent, image_path: p.image_path, stock: stockOf(p), unit: p.unit });
        if (had && l.paper) cart.setPrice(p.id, Number(l.price)); // already on the order: the paper's price for the whole line
      } else {
        cart.addItem({ product_id: looseId(i), name: l.name.trim(), price: Number(l.price), quantity: Number(l.quantity) });
      }
    });
    toast.show(t("Added {{items}} to the order", { items: plural(ready.length, "1 item", "{{n}} items") }), "success");
    router.replace("/order/cart");
  }

  const line = lines.find((l) => l.key === changing) ?? null;
  return (
    <Screen bg={colors.surface}>
      <Header title={t("Check the order")} onBack={() => setLines(null)} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }} bottomOffset={24} keyboardShouldPersistTaps="handled">
        <T v="small">{t("Check each line: the product, how many and the price. Tap a product to change it.")}</T>
        {lines.map((l) => (
          <LineCard key={l.key} line={l} short={!!l.product && shortOf(l.product)} stock={l.product ? stockOf(l.product) : 0}
            want={l.product ? want.get(l.product.id) ?? 0 : 0} isOwner={isOwner} onChange={(patch) => set(l.key, patch)}
            onChangeProduct={() => setChanging(l.key)} onRemove={() => setLines((list) => list!.filter((x) => x.key !== l.key))} />
        ))}
        {!lines.length && <T v="body" style={{ textAlign: "center", paddingVertical: spacing.xl }}>{t("No lines left.")}</T>}
      </KeyboardAwareScrollView>
      <BottomBar>
        {open > 0 && (
          <T v="small" color={colors.amberInk}>
            {plural(open, "1 line is not ready: pick a product, sell it on this bill only, or remove it.",
              "{{n}} lines are not ready: pick a product, sell them on this bill only, or remove them.")}
          </T>
        )}
        {blocked && <T v="small" color={colors.red}>{t("Some items are more than the stock. Ask the owner to update the stock.")}</T>}
        <Button title={ready.length ? t("Add {{items}} to the order · {{total}}", { items: plural(ready.length, "1 item", "{{n}} items"), total: formatCurrency(total) })
          : t("Add to order")} icon="cart-plus" onPress={addAll} disabled={!ready.length || open > 0 || blocked} testID="po-add" />
      </BottomBar>
      <ProductChooser line={line} onClose={() => setChanging(null)} onPick={(p) => line && pick(line.key, p)}
        onLoose={() => { if (line) set(line.key, { product: null, loose: true, price: line.paper ? String(line.paper) : line.price }); setChanging(null); }} />
    </Screen>
  );
}

function LineCard({ line: l, short, stock, want, isOwner, onChange, onChangeProduct, onRemove }: {
  line: Line; short: boolean; stock: number; want: number; isOwner: boolean; onChange: (patch: Partial<Line>) => void;
  onChangeProduct: () => void; onRemove: () => void;
}) {
  const { t } = useTranslation();
  const p = l.product;
  const whole = wholeUnit(p?.unit);
  const changed = !!p && priceOk(l.price) && Math.abs(Number(l.price) - p.selling_price) > 0.004;
  return (
    <View testID={`po-line-${l.read}`} style={{ gap: spacing.md, padding: spacing.md, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
        <T v="small" style={{ flex: 1 }}>{t("Written: {{text}}", { text: l.read })}</T>
        <Pressable accessibilityLabel={t("Remove")} onPress={onRemove} testID={`po-remove-${l.read}`}
          style={{ width: 44, height: 44, alignItems: "center", justifyContent: "center" }}>
          <Icon name="trash-can-outline" size={24} color={colors.red} />
        </Pressable>
      </View>

      {p ? (
        <Pressable onPress={onChangeProduct} testID={`po-product-${l.read}`} accessibilityRole="button"
          style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.sm, borderRadius: radius.md, backgroundColor: colors.greenTint }}>
          <Thumb path={p.image_path} size={48} />
          <View style={{ flex: 1, minWidth: 0 }}>
            <T v="bold">{p.name}</T>
            <T v="small" color={p.out_of_stock ? colors.red : colors.muted}>
              {p.out_of_stock ? t("Out of stock") : t("{{n}} in stock", { n: formatQtyUnit(stock, p.unit) })}
            </T>
          </View>
          <T v="bold" color={colors.green}>{t("Change")}</T>
        </Pressable>
      ) : l.loose ? (
        <View style={{ gap: spacing.sm }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            <Badge text={t("This bill only")} tone="blue" />
            <View style={{ flex: 1 }} />
            <Pressable onPress={onChangeProduct} hitSlop={8} testID={`po-product-${l.read}`}>
              <T v="bold" color={colors.green}>{t("Pick a product")}</T>
            </Pressable>
          </View>
          <Field value={l.name} onChangeText={(name) => onChange({ name })} placeholder={t("Item name")} autoCapitalize="words"
            testID={`po-name-${l.read}`} />
        </View>
      ) : (
        <Pressable onPress={onChangeProduct} testID={`po-product-${l.read}`} accessibilityRole="button"
          style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, borderRadius: radius.md, backgroundColor: colors.amberTint }}>
          <Icon name="help-circle-outline" size={28} color={colors.amber} />
          <T v="bold" color={colors.amberInk} style={{ flex: 1 }}>{t("Not found in your products")}</T>
          <T v="bold" color={colors.green}>{t("Choose")}</T>
        </Pressable>
      )}

      <View style={{ flexDirection: "row", gap: spacing.md }}>
        <Field style={{ flex: 1 }} label={p && !whole ? t("Quantity ({{unit}})", { unit: unitShort(p.unit) }) : t("Quantity")} value={l.quantity}
          onChangeText={(v) => onChange({ quantity: whole ? v.replace(/\D/g, "") : num(v) })} keyboardType={whole ? "number-pad" : "decimal-pad"}
          error={l.quantity !== "" && !positive(l.quantity) ? t("More than 0") : undefined} testID={`po-qty-${l.read}`} />
        <Field style={{ flex: 1 }} label={p && !whole ? t("Price per {{unit}}", { unit: unitShort(p.unit) }) : t("Price each")} value={l.price}
          onChangeText={(v) => onChange({ price: num(v) })} keyboardType="decimal-pad" placeholder="0" testID={`po-price-${l.read}`} />
      </View>
      {changed && (
        <T v="small" testID={`po-shop-price-${l.read}`}>
          {l.paper && Math.abs(Number(l.price) - l.paper) < 0.005 ? t("Price on the paper.") : t("Price changed.")}{" "}
          {t("Shop price")} <T v="small" style={{ textDecorationLine: "line-through" }}>{formatCurrency(p!.selling_price)}</T>
        </T>
      )}
      {p && short && (
        <View testID={`po-short-${l.read}`} style={{ flexDirection: "row", gap: spacing.sm, padding: spacing.md, borderRadius: radius.md,
          backgroundColor: isOwner ? colors.amberTint : colors.redTint }}>
          <Icon name="alert-circle-outline" size={20} color={isOwner ? colors.amberInk : colors.red} />
          <T v="small" color={isOwner ? colors.amberInk : colors.red} style={{ flex: 1 }}>
            {isOwner ? shortWarning(p.name, stock, want, p.unit) : askOwner(p.name, stock, p.unit)}
          </T>
        </View>
      )}
    </View>
  );
}

// Changing a line's product: the other close products first, then a search by name; or sell it on this bill only.
function ProductChooser({ line, onClose, onPick, onLoose }: {
  line: Line | null; onClose: () => void; onPick: (p: Product) => void; onLoose: () => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const query = q.trim();
  const done = (then: () => void) => {
    setQ("");
    then();
  };
  useEffect(() => {
    const id = setTimeout(() => setTerm(query), SEARCH_AFTER_MS);
    return () => clearTimeout(id);
  }, [query]);
  const results = useQuery<Product[]>({
    queryKey: ["products", "search", term],
    queryFn: ({ signal }) => apiGet(`/products?search=${encodeURIComponent(term)}`, { signal }),
    enabled: !!line && query.length >= 2 && term.length >= 2,
    placeholderData: keepPreviousData,
  });
  const shown = query.length >= 2 ? results.data ?? [] : line ? [...(line.product ? [line.product] : []), ...line.others] : [];
  const searching = query.length >= 2 && (query !== term || results.isFetching) && !results.data;

  return (
    <Sheet visible={!!line} onClose={() => done(onClose)} title={t("Choose the product")} testID="po-chooser">
      {!!line && <T v="small">{t("Written: {{text}}", { text: line.read })}</T>}
      <SearchBox value={q} onChangeText={setQ} placeholder={t("Search by product name")} testID="po-search" loading={searching} />
      {query.length < 2 && !!shown.length && <T v="label">{t("Close matches in your stock")}</T>}
      {shown.map((p) => (
        <Pressable key={p.id} testID={`po-pick-${p.name}`} onPress={() => done(() => onPick(p))}
          style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 64, borderRadius: radius.lg,
            borderWidth: line?.product?.id === p.id ? 2 : 1, borderColor: line?.product?.id === p.id ? colors.green : colors.line }}>
          <Thumb path={p.image_path} size={44} />
          <View style={{ flex: 1 }}>
            <T v="bold">{p.name}</T>
            <T v="small" color={p.out_of_stock ? colors.red : colors.muted}>
              {p.out_of_stock ? t("Out of stock") : t("{{n}} in stock", { n: formatQtyUnit(p.stock, p.unit) })}
            </T>
          </View>
          <T v="number">{formatCurrency(p.selling_price)}</T>
        </Pressable>
      ))}
      {query.length >= 2 && !shown.length && !searching && results.data && <T v="small">{t("No products match “{{q}}”.", { q: query })}</T>}
      {query.length >= 2 && results.isFetching && !shown.length && <ActivityIndicator color={colors.green} />}
      <Pressable onPress={() => done(onLoose)} testID="po-loose"
        style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 60, borderRadius: radius.lg,
          borderWidth: 2, borderStyle: "dashed", borderColor: colors.green }}>
        <Icon name="tag-outline" size={26} color={colors.green} />
        <View style={{ flex: 1 }}>
          <T v="bold" color={colors.greenDark}>{t("Sell on this bill only")}</T>
          <T v="small">{t("Adds the item to this bill without creating a product.")}</T>
        </View>
      </Pressable>
    </Sheet>
  );
}
