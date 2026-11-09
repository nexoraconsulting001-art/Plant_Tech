// New order: take a photo (AI finds the product) or type its name.
// Found → add one (adding again adds one more; quantity and price are changed on the order screen, owner 10 Nov
// 2026). Not in stock → save it as a new product, or sell it on this bill only.
// More than the stock count of a product: the owner may, after a warning (it stays a normal line of that
// product with its GST, and the stock goes below zero); an employee may not.
// Photos of anything a nursery doesn't sell are refused with an explanation; a photo of a bill or a written list can be
// read as the customer's order (paper.tsx) or, for those who add stock, opened as a supplier bill.
// Search by name: sent 250 ms after the last key, with the last results kept on screen meanwhile; a search
// overtaken by a newer one is cancelled (React Query's signal).
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Keyboard, Pressable, ScrollView, TextInput, View } from "react-native";
import { Image } from "expo-image";
import * as Haptics from "expo-haptics";
import { Redirect, useRouter } from "expo-router";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { ApiError, apiGet, apiPost, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useCart } from "@/src/cart-context";
import { CorrectionBanner, DiscardOrder, OrderCustomerBar } from "@/src/components/order-customer";
import { CategoryPicker } from "@/src/components/category-picker";
import { NetStatus } from "@/src/components/net-status";
import { choose, confirm } from "@/src/confirm";
import { useToast } from "@/src/components/toast";
import { Badge, BottomBar, Button, Field, Header, Icon, IconName, Screen, SearchBox, Segmented, Stepper, T, Thumb } from "@/src/components/ui";
import { formatCurrency, formatQty, formatQtyUnit } from "@/src/format";
import { CaptureResult, pickFromGallery, takePhoto } from "@/src/hooks/useImageCapture";
import { LONG_EDGE } from "@/src/image-tools";
import { setPendingPhoto } from "@/src/pending-photo";
import { askOwner, shortWarning } from "@/src/stock";
import { colors, radius, spacing, touch } from "@/src/theme";
import type { Product } from "@/src/types";
import { roundQty, unitShort, wholeUnit } from "@/src/units";

const SEARCH_AFTER_MS = 250;

type Stage =
  | { kind: "start" }
  | { kind: "checking" }
  | { kind: "found"; product: Product; confidence?: string; others: Product[] }
  | { kind: "new"; name: string; botanical: string; categoryId: string | null; similar: Product[] };

export default function OrderScan() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const cart = useCart();
  const { can } = useAuth();
  // Opened with no customer (a reload, an old link): pick one first. Not when the order is cleared while open (Discard),
  // nor when correcting an old bill that had none (the order screen asks for one).
  const [openedWithout] = useState(() => !cart.customer && !cart.correcting);
  const [stage, setStage] = useState<Stage>({ kind: "start" });
  const [photo, setPhoto] = useState<CaptureResult | null>(null);
  const [focusSearch, setFocusSearch] = useState(0);

  async function capture(source: "camera" | "gallery") {
    const out = source === "camera" ? await takePhoto({ aspect: 1, longEdge: LONG_EDGE.plant }) : await pickFromGallery({ aspect: 1, longEdge: LONG_EDGE.plant });
    if (out.status === "denied") {
      toast.show(t("Please allow camera and photo access for Hara Hisaab in your phone settings."), "error");
      return;
    }
    if (out.status !== "ok") return;
    setPhoto(out.result);
    setStage({ kind: "checking" });
    try {
      const r = await apiPost("/ai/identify-plant", { image_base64: out.result.base64 });
      // recommended_products: the shop's other close products (a server before them sends similar_products only)
      const others: Product[] = r.recommended_products ?? r.similar_products ?? [];
      if (r.matched_product) {
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
        setStage({ kind: "found", product: r.matched_product, confidence: r.confidence_label, others });
      } else {
        setStage({ kind: "new", name: r.plant_name, botanical: r.scientific_name || "",
          categoryId: r.suggested_category_id ?? null, similar: others });
      }
    } catch (e: any) {
      restart();
      await refused(e, source, out.result);
    }
  }

  // The photo was not of something to sell: say why, and offer the useful next step.
  async function refused(e: any, source: "camera" | "gallery", photoOf: CaptureResult) {
    const code = e instanceof ApiError ? e.code : "";
    if (code === "looks_like_bill") {
      // a customer's order on paper, or a supplier's bill (only for those who add stock)
      const pick = can("stock")
        ? await choose(t("This looks like a bill"), t("Read it as a customer's order, or add stock from a supplier's bill."), t("Read as order"),
          t("Open supplier bill"))
        : (await confirm(t("This looks like a bill"), t("Read it as a customer's order?"), t("Read as order"))) ? "yes" : null;
      if (pick === "yes") {
        setPendingPhoto(photoOf);
        router.push("/order/paper");
      } else if (pick === "no") router.push("/more/supplier-bill");
      return;
    }
    if (code === "not_nursery_item" || code === "not_identified") {
      const pick = await choose(code === "not_nursery_item" ? t("Not a nursery item") : t("Item not identified"), e.message,
        t("Retake photo"), t("Search by name"));
      if (pick === "yes") capture(source);
      else if (pick === "no") setFocusSearch((n) => n + 1);
      return;
    }
    toast.show(e.message, "error");
  }

  const restart = () => {
    setPhoto(null);
    setStage({ kind: "start" });
  };
  // After adding: back to the camera for the next plant, or on to the order.
  const added = (next: "scan" | "cart") => {
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
    restart();
    if (next === "cart") router.push("/order/cart");
    else toast.show(t("Added to the order. Scan the next item."), "success");
  };

  if (openedWithout) return <Redirect href="/order/customer" />;
  if (stage.kind === "found") {
    // Picking one of the other matches: it opens, and the one shown before joins the list of other matches.
    const pickOther = (p: Product) =>
      setStage({ kind: "found", product: p, others: [stage.product, ...stage.others.filter((o) => o.id !== p.id)] });
    return <Found key={stage.product.id} product={stage.product} confidence={stage.confidence} others={stage.others}
      photoUri={photo?.uri} onBack={restart} onSearch={() => { restart(); setFocusSearch((n) => n + 1); }} onAdded={added} onPick={pickOther} />;
  }
  if (stage.kind === "new") {
    return <NotInStock initial={stage} photo={photo} onBack={restart} onAdded={added}
      onPick={(p) => setStage({ kind: "found", product: p, others: [] })} />;
  }
  return <Start checking={stage.kind === "checking"} photoUri={photo?.uri} onCapture={capture} focusSearch={focusSearch} onSearchOpened={() => setFocusSearch(0)}
    onPick={(p) => setStage({ kind: "found", product: p, others: [] })}
    onNew={(name) => { setPhoto(null); setStage({ kind: "new", name, botanical: "", categoryId: null, similar: [] }); }} />;
}

function Start({ checking, photoUri, onCapture, onPick, onNew, focusSearch, onSearchOpened }: {
  checking: boolean; photoUri?: string; onCapture: (s: "camera" | "gallery") => void; onPick: (p: Product) => void; onNew: (name: string) => void;
  focusSearch: number; onSearchOpened: () => void; // focusSearch > 0: open the search (asked once; onSearchOpened sets it back to 0)
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const [q, setQ] = useState("");
  // Searching by name: the camera panel folds away and the box sits at the top, the results right under it, so
  // what is typed is never under the keyboard (owner, 10 Nov 2026).
  const [searching, setSearching] = useState(false);
  const searchRef = useRef<TextInput>(null);
  if (focusSearch > 0 && !searching) setSearching(true);
  useEffect(() => {
    if (!focusSearch) return;
    const id = setTimeout(() => { searchRef.current?.focus(); onSearchOpened(); }, 400); // after the alert sheet has closed
    return () => clearTimeout(id);
  }, [focusSearch]); // eslint-disable-line react-hooks/exhaustive-deps
  const query = q.trim();
  const [term, setTerm] = useState(""); // what was searched: the query once typing pauses
  useEffect(() => {
    const id = setTimeout(() => setTerm(query), SEARCH_AFTER_MS);
    return () => clearTimeout(id);
  }, [query]);
  const results = useQuery<Product[]>({
    queryKey: ["products", "search", term],
    queryFn: ({ signal }) => apiGet(`/products?search=${encodeURIComponent(term)}`, { signal }),
    enabled: query.length >= 2 && term.length >= 2,
    placeholderData: keepPreviousData,
  });
  const shown = query.length >= 2 ? results.data ?? [] : [];
  const settled = query.length >= 2 && query === term && !!results.data && !results.isPlaceholderData; // the results are for what is typed
  const stopSearching = () => {
    onSearchOpened();
    setQ("");
    setSearching(false);
    Keyboard.dismiss();
  };

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Add items")} close onBack={() => router.back()} testID="scan-close" right={<DiscardOrder />} />
      {!searching && (
        <View style={{ paddingHorizontal: spacing.lg, paddingBottom: spacing.md, gap: spacing.md }}>
          <NetStatus />
          <OrderCustomerBar />
          <CorrectionBanner />
        </View>
      )}
      {searching && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm, paddingHorizontal: spacing.lg, paddingBottom: spacing.md }}>
          <Pressable onPress={stopSearching} accessibilityRole="button" accessibilityLabel={t("Back to scan")} testID="scan-search-close"
            hitSlop={8} style={{ width: 44, height: 44, alignItems: "center", justifyContent: "center" }}>
            <Icon name="arrow-left" size={26} color={colors.ink} />
          </Pressable>
          <View style={{ flex: 1 }}>
            <SearchBox ref={searchRef} value={q} onChangeText={setQ} placeholder={t("Search by product name")} autoFocus testID="scan-search" />
          </View>
        </View>
      )}
      <KeyboardAwareScrollView keyboardShouldPersistTaps="handled" bottomOffset={24} contentContainerStyle={{ paddingBottom: spacing.lg }}>
        {!searching && (
          <>
            <Pressable testID="scan-camera-panel" disabled={checking} onPress={() => onCapture("camera")}
              accessibilityRole="button" accessibilityLabel={t("Tap to scan")}
              style={{ marginHorizontal: spacing.lg, height: 280, borderRadius: 26, overflow: "hidden", backgroundColor: "#1C2B24",
                alignItems: "center", justifyContent: "center" }}>
              {checking && photoUri ? (
                <>
                  <Image source={{ uri: photoUri }} style={{ position: "absolute", width: "100%", height: "100%" }} contentFit="cover" />
                  <View style={{ position: "absolute", inset: 0, backgroundColor: "rgba(16,32,26,0.55)" } as any} />
                  <ActivityIndicator size="large" color={colors.white} />
                  <T v="bodyLarge" color={colors.white} style={{ marginTop: spacing.md }}>{t("Identifying the item")}</T>
                </>
              ) : (
                <>
                  <View style={{ width: 150, height: 150, borderWidth: 4, borderColor: colors.white, borderRadius: 28, opacity: 0.85,
                    alignItems: "center", justifyContent: "center" }}>
                    <Icon name="camera" size={56} color={colors.white} />
                  </View>
                  <T v="title" color={colors.white} style={{ marginTop: spacing.lg }}>{t("Tap to scan")}</T>
                  <T v="small" color="#D5E9DB" style={{ marginTop: 2 }}>{t("Take a photo of the item")}</T>
                </>
              )}
            </Pressable>
            <View style={{ flexDirection: "row", gap: spacing.md, paddingTop: spacing.lg, paddingHorizontal: spacing.lg }}>
              <SideButton icon="image-outline" title={t("Upload photo")} disabled={checking} onPress={() => onCapture("gallery")} testID="scan-gallery" />
              <SideButton icon="file-document-outline" title={t("Upload order details")} disabled={checking}
                onPress={() => router.push("/order/paper")} testID="scan-paper" />
            </View>
            <Pressable onPress={() => setSearching(true)} testID="scan-search-open" accessibilityRole="search"
              style={{ marginHorizontal: spacing.lg, marginTop: spacing.lg, flexDirection: "row", alignItems: "center", gap: spacing.sm,
                minHeight: 52, paddingHorizontal: spacing.md, borderRadius: radius.md, borderWidth: 1.5, borderColor: colors.lineStrong }}>
              <Icon name="magnify" size={22} color={colors.muted} />
              <T v="body" color={colors.muted}>{t("Or search by product name")}</T>
            </Pressable>
          </>
        )}

        {searching && (
          <View style={{ paddingHorizontal: spacing.lg, gap: spacing.md }}>
            {query.length < 2 && <T v="small">{t("Type at least 2 letters of the name, in English or Hindi.")}</T>}
            {query.length >= 2 && !shown.length && !settled && !results.isError && <ActivityIndicator color={colors.green} style={{ marginTop: spacing.md }} />}
            {shown.map((p) => (
              <Pressable key={p.id} testID={`scan-result-${p.name}`} onPress={() => { Keyboard.dismiss(); onPick(p); }}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 72, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
                <Thumb path={p.image_path} size={52} />
                <View style={{ flex: 1 }}>
                  <T v="bold">{p.name}</T>
                  <T v="small" color={p.out_of_stock ? colors.red : colors.muted}>
                    {p.out_of_stock ? t("Out of stock") : t("{{n}} in stock", { n: formatQtyUnit(p.stock, p.unit) })}
                  </T>
                </View>
                <T v="number">{formatCurrency(p.selling_price)}</T>
              </Pressable>
            ))}
            {settled && (
              <Pressable testID="scan-not-listed" onPress={() => { Keyboard.dismiss(); onNew(query); }}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 64, borderRadius: radius.lg, borderWidth: 2,
                  borderStyle: "dashed", borderColor: colors.green }}>
                <Icon name="plus-circle" size={30} color={colors.green} />
                <T v="bold" color={colors.greenDark} style={{ flex: 1 }}>
                  {shown.length ? t("Sell “{{q}}” as a new item", { q: query }) : t("“{{q}}” is not in your products", { q: query })}
                </T>
              </Pressable>
            )}
          </View>
        )}
      </KeyboardAwareScrollView>
      <CartBar />
    </Screen>
  );
}

// "Upload photo" / "Upload order details" under the camera panel: two equal buttons.
function SideButton({ icon, title, onPress, disabled, testID }: {
  icon: IconName; title: string; onPress: () => void; disabled?: boolean; testID: string;
}) {
  return (
    <Pressable onPress={onPress} disabled={disabled} testID={testID} accessibilityRole="button"
      style={({ pressed }) => ({ flex: 1, minHeight: 64, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.sm,
        paddingHorizontal: spacing.sm, borderRadius: radius.lg, borderWidth: 1.5, borderColor: colors.green,
        backgroundColor: pressed ? colors.greenTint : colors.surface, opacity: disabled ? 0.5 : 1 })}>
      <Icon name={icon} size={24} color={colors.greenDark} />
      <T v="bold" color={colors.greenDark} style={{ flexShrink: 1, textAlign: "center" }}>{title}</T>
    </Pressable>
  );
}

// "2 · View order · ₹500" at the bottom while there are items.
function CartBar() {
  const { t } = useTranslation();
  const router = useRouter();
  const cart = useCart();
  if (!cart.count) return null;
  return (
    <BottomBar style={{ borderTopWidth: 0 }}>
      <Pressable testID="scan-view-order" onPress={() => router.push("/order/cart")}
        style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.lg, minHeight: touch.primary, borderRadius: radius.lg, backgroundColor: colors.green }}>
        <View style={{ minWidth: 34, height: 34, paddingHorizontal: spacing.sm, borderRadius: 17, backgroundColor: colors.white, alignItems: "center", justifyContent: "center" }}>
          <T v="number" color={colors.greenDark} style={{ fontSize: 19 }}>{formatQty(cart.count)}</T>
        </View>
        <T v="bodyLarge" color={colors.white} style={{ flex: 1 }}>{t("View order")}</T>
        <T v="title" color={colors.white} style={{ fontSize: 24 }}>{formatCurrency(cart.total)}</T>
      </Pressable>
    </BottomBar>
  );
}

function Found({ product: p, confidence, others, photoUri, onBack, onSearch, onAdded, onPick }: {
  product: Product; confidence?: string; others: Product[]; photoUri?: string; onBack: () => void; onSearch: () => void;
  onAdded: (next: "scan" | "cart") => void; onPick: (p: Product) => void;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const cart = useCart();
  const { can, isOwner } = useAuth();
  const inCart = cart.items.find((i) => i.product_id === p.id)?.quantity ?? 0;
  const back = cart.returning(p.id); // correcting a bill: what that bill took counts as back in stock
  const stock = roundQty(p.stock + back, false);
  const whole = wholeUnit(p.unit);
  const left = Math.max(0, roundQty(stock - inCart, false));
  const qty = whole ? 1 : Math.min(1, left) || 1; // one at a time; 0.5 kg left: 0.5
  const short = qty > left + 1e-9; // only the owner gets here with more than is left
  const blocked = !isOwner && left <= 0; // an employee can't sell what isn't in stock
  const conf = confidence === "High" ? { text: t("Strong match"), tone: "green" as const }
    : confidence === "Medium" ? { text: t("Likely match"), tone: "amber" as const }
    : confidence ? { text: t("Please verify"), tone: "red" as const } : null;

  // Always a line of this product (with its GST), even when selling without stock.
  function add(next: "scan" | "cart") {
    cart.addItem({ product_id: p.id, name: p.name, price: p.selling_price, quantity: qty, gst_percent: p.gst_percent, image_path: p.image_path,
      stock, unit: p.unit });
    onAdded(next);
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={conf ? t("Match found") : p.name} onBack={onBack} />
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
        <View style={{ flexDirection: "row", gap: spacing.lg, alignItems: "center" }}>
          {photoUri ? <Image source={{ uri: photoUri }} style={{ width: 110, height: 110, borderRadius: 24 }} contentFit="cover" />
            : <Thumb path={p.image_path} size={110} />}
          <View style={{ flex: 1, gap: spacing.xs }}>
            <T v="title" testID="found-name">{p.name}</T>
            {!!p.botanical_name && <T v="small" style={{ fontStyle: "italic" }}>{p.botanical_name}</T>}
            {conf && <Badge text={conf.text} tone={conf.tone} />}
          </View>
        </View>
        <View style={{ flexDirection: "row", gap: spacing.md }}>
          <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
            <T v="label">{t("Price")}</T>
            <T v="title">{formatCurrency(p.selling_price)}</T>
            {!whole && <T v="caption">{t("per {{unit}}", { unit: unitShort(p.unit) })}</T>}
          </View>
          <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: left ? colors.page : "#FDECEC" }}>
            <T v="label">{t("In stock")}</T>
            <T v="title" color={left ? colors.ink : colors.red}>{formatQtyUnit(stock, p.unit)}</T>
            {back > 0 && <T v="caption">{t("Includes {{n}} from the bill being corrected", { n: formatQtyUnit(back, p.unit) })}</T>}
            {inCart > 0 && <T v="caption">{t("{{n}} already in this order", { n: formatQtyUnit(inCart, p.unit) })}</T>}
          </View>
        </View>

        {(blocked || short) && (
          <View testID="found-short" style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: blocked ? colors.redTint : colors.amberTint }}>
            <T v="bold" color={blocked ? colors.red : colors.amberInk}>{stock > 0 ? t("Not enough stock") : t("Out of stock")}</T>
            <T v="small" color={blocked ? colors.red : colors.amberInk}>
              {blocked ? askOwner(p.name, stock, p.unit) : shortWarning(p.name, stock, inCart + qty, p.unit)}
            </T>
            {!blocked && <T v="small" color={colors.amberInk}>{t("You can still sell it if you have it on hand. Update the stock count afterwards.")}</T>}
            {can("stock") && (
              <Button title={t("Add stock")} icon="package-variant-plus" variant="outline" size="md"
                onPress={() => router.push({ pathname: "/products/[id]", params: { id: p.id } })} />
            )}
          </View>
        )}

        {others.length > 0 && (
          <View style={{ gap: spacing.sm }}>
            <T v="label">{t("Not this one? Other matches in your stock")}</T>
            {others.map((o) => (
              <Pressable key={o.id} testID={`found-other-${o.name}`} onPress={() => onPick(o)}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 64,
                  borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
                <Thumb path={o.image_path} size={44} />
                <View style={{ flex: 1 }}>
                  <T v="bold">{o.name}</T>
                  <T v="small" color={o.out_of_stock ? colors.red : colors.muted}>
                    {o.out_of_stock ? t("Out of stock") : t("{{n}} in stock", { n: formatQtyUnit(o.stock, o.unit) })}
                  </T>
                </View>
                <T v="number">{formatCurrency(o.selling_price)}</T>
              </Pressable>
            ))}
          </View>
        )}

        <Pressable onPress={onSearch} testID="found-search" style={{ minHeight: 48, justifyContent: "center", alignItems: "center" }}>
          <T v="bold" color={colors.green}>{t("Not the right item? Search by name")}</T>
        </Pressable>
      </ScrollView>
      <BottomBar>
        {blocked ? (
          <Button title={t("Search another item")} icon="magnify" variant="outline" onPress={onSearch} testID="found-search-again" />
        ) : (
          <>
            <Button title={t("Add and scan next")} icon="camera" variant="outline" size="md" onPress={() => add("scan")} testID="found-add-next" />
            {short ? (
              <Button title={t("Sell without stock · {{total}}", { total: formatCurrency(p.selling_price * qty) })} icon="cart-plus"
                onPress={() => add("cart")} testID="found-sell-anyway" />
            ) : (
              <Button title={t("Add to order · {{total}}", { total: formatCurrency(p.selling_price * qty) })} icon="cart-plus"
                onPress={() => add("cart")} testID="found-add" />
            )}
          </>
        )}
      </BottomBar>
    </Screen>
  );
}

function NotInStock({ initial, photo, onBack, onAdded, onPick }: {
  initial: { name: string; botanical: string; categoryId: string | null; similar: Product[] }; photo: CaptureResult | null;
  onBack: () => void; onAdded: (next: "scan" | "cart") => void; onPick: (p: Product) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const cart = useCart();
  const { can } = useAuth();
  const canSave = can("stock");
  const [mode, setMode] = useState<"save" | "once">(canSave ? "save" : "once");
  const [name, setName] = useState(initial.name);
  const [price, setPrice] = useState("");
  const [stock, setStock] = useState("1");
  const [categoryId, setCategoryId] = useState<string | null>(initial.categoryId);
  const [qty, setQty] = useState(1);
  const [busy, setBusy] = useState(false);

  const priceN = Number(price);
  const stockN = Number(stock);
  const priceOk = price !== "" && priceN >= 0 && Number.isFinite(priceN);
  const stockOk = mode === "once" || (Number.isFinite(stockN) && stockN >= qty);
  const ok = !!name.trim() && priceOk && stockOk;

  async function submit() {
    if (mode === "once") {
      cart.addItem({ product_id: `loose-${Date.now().toString(36)}`, name: name.trim(), price: priceN, quantity: qty });
      onAdded("cart");
      return;
    }
    setBusy(true);
    try {
      const photo_path = photo ? await uploadImage(photo.base64) : null;
      const p: Product = await apiPost("/products", { name: name.trim(), botanical_name: initial.botanical, category_id: categoryId,
        selling_price: priceN, stock: stockN, photo_path });
      qc.invalidateQueries({ queryKey: ["products"] });
      cart.addItem({ product_id: p.id, name: p.name, price: p.selling_price, quantity: qty, gst_percent: p.gst_percent, image_path: p.image_path, stock: p.stock });
      onAdded("cart");
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("New item")} onBack={onBack} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24} keyboardShouldPersistTaps="handled">
        <View style={{ flexDirection: "row", gap: spacing.lg, alignItems: "center", padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.amberTint }}>
          {photo ? <Image source={{ uri: photo.uri }} style={{ width: 72, height: 72, borderRadius: 18 }} contentFit="cover" />
            : <Icon name="help-circle" size={44} color={colors.amber} />}
          <View style={{ flex: 1 }}>
            <T v="bodyLarge" color={colors.amberInk}>{t("This item is not in your products")}</T>
            <T v="small" color={colors.amberInk}>{t("Add it to your products, or sell it on this bill only.")}</T>
          </View>
        </View>

        {initial.similar.length > 0 && (
          <View style={{ gap: spacing.sm }}>
            <T v="label">{t("Already in your products?")}</T>
            {initial.similar.map((p) => (
              <Pressable key={p.id} testID={`new-similar-${p.name}`} onPress={() => onPick(p)}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 64, borderRadius: radius.lg,
                  borderWidth: 1, borderColor: colors.line }}>
                <Thumb path={p.image_path} size={44} />
                <T v="bold" style={{ flex: 1 }}>{p.name}</T>
                <T v="number">{formatCurrency(p.selling_price)}</T>
              </Pressable>
            ))}
          </View>
        )}

        {canSave && (
          <Segmented value={mode} onChange={setMode}
            options={[{ value: "save", label: t("Add to products") }, { value: "once", label: t("This bill only") }]} />
        )}
        <T v="small">
          {mode === "save" ? t("Saves the item to your products and adds it to this order.") : t("Adds the item to this bill without creating a product.")}
        </T>

        <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" testID="new-name" />
        <Field label={t("Price ₹ (each)")} value={price} onChangeText={(v) => setPrice(v.replace(/[^0-9.]/g, ""))} keyboardType="decimal-pad"
          placeholder="0" testID="new-price" inputStyle={{ fontSize: 24 }} />
        {mode === "save" && (
          <>
            <Field label={t("Opening stock (including this sale)")} value={stock} onChangeText={(v) => setStock(v.replace(/\D/g, ""))}
              keyboardType="number-pad" testID="new-stock" error={!stockOk ? t("Must be at least {{n}} for this order", { n: qty }) : undefined} />
            <CategoryPicker value={categoryId} onChange={setCategoryId} />
          </>
        )}
        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
          <T v="bodyLarge">{t("Quantity")}</T>
          <Stepper value={qty} onChange={setQty} testID="new-qty" />
        </View>
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={mode === "save" ? t("Save and add to order") : t("Add to order")} icon="cart-plus" onPress={submit}
          loading={busy} disabled={!ok} testID="new-submit" />
      </BottomBar>
    </Screen>
  );
}
