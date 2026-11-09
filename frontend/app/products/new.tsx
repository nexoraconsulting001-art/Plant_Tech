// Add product: type a name → your own match ("add stock"), a shared-library match (picture + category),
// or create new. Or take a photo and the AI fills in the name and category (photos of anything a
// nursery doesn't sell are refused, and only accepted photos are uploaded).
import { useState } from "react";
import { ActivityIndicator, Pressable, View } from "react-native";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError, apiGet, apiPost, uploadImage } from "@/src/api";
import { useCategories } from "@/src/components/category-picker";
import { choose, confirm } from "@/src/confirm";
import { emptyProduct, ProductForm, ProductValues } from "@/src/components/product-form";
import { useToast } from "@/src/components/toast";
import { Button, Field, Header, Icon, Screen, T, Thumb } from "@/src/components/ui";
import { formatQty } from "@/src/format";
import { takePhoto } from "@/src/hooks/useImageCapture";
import { LONG_EDGE } from "@/src/image-tools";
import { colors, radius, spacing } from "@/src/theme";
import type { LibraryEntry, Product } from "@/src/types";
import { isPlantCategory } from "@/src/units";

export default function NewProduct() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const params = useLocalSearchParams<{ name?: string }>();
  const cats = useCategories();
  const [q, setQ] = useState(params.name ?? "");
  const [form, setForm] = useState<ProductValues | null>(null);
  const [scanning, setScanning] = useState(false);
  const [saving, setSaving] = useState(false);
  const query = q.trim();
  const sug = useQuery<{ own: Product[]; library: LibraryEntry[] }>({
    queryKey: ["suggest", query],
    queryFn: () => apiGet(`/products/suggest?q=${encodeURIComponent(query)}`),
    enabled: query.length >= 2 && !form,
  });

  const catIdByName = (name?: string) =>
    (cats.data ?? []).find((c) => c.name.toLowerCase() === (name ?? "").toLowerCase())?.id ?? null;

  function fromLibrary(e: LibraryEntry) {
    setForm({ ...emptyProduct, name: e.name, botanical_name: e.botanical, library_id: e.id, library_image: e.image_path,
      category_id: catIdByName(e.default_category), plant: isPlantCategory(e.default_category) });
  }

  async function scan() {
    const out = await takePhoto({ aspect: 1, longEdge: LONG_EDGE.plant });
    if (out.status === "denied") toast.show(t("Please allow camera and photo access for Hara Hisaab in your phone settings."), "error");
    if (out.status !== "ok") return;
    setScanning(true);
    try {
      const r = await apiPost("/ai/identify-plant", { image_base64: out.result.base64 });
      if (r.matched_product) {
        toast.show(t("{{name}} is already in your products. You can add stock here.", { name: r.matched_product.name }), "info");
        router.replace({ pathname: "/products/[id]", params: { id: r.matched_product.id } });
        return;
      }
      // Not sure it is one of the shop's products, but close to one: ask before making a second one.
      const close: Product | undefined = (r.recommended_products ?? r.similar_products ?? [])[0];
      if (close) {
        const pick = await choose(t("Already in your products?"), t("This may be {{name}}, which you already have.", { name: close.name }),
          t("Open {{name}}", { name: close.name }), t("Add as new"));
        if (pick === "yes") {
          router.replace({ pathname: "/products/[id]", params: { id: close.id } });
          return;
        }
        if (pick !== "no") return; // closed without choosing
      }
      const path = await uploadImage(out.result.base64);
      setForm({ ...emptyProduct, name: r.plant_name, botanical_name: r.scientific_name || "",
        category_id: r.suggested_category_id ?? null, photo_path: path, plant: r.kind === "plant" });
    } catch (e: any) {
      const code = e instanceof ApiError ? e.code : "";
      if (code === "looks_like_bill") {
        if (await confirm(t("Supplier bill detected"), e.message, t("Open supplier bill"))) router.replace("/more/supplier-bill");
      } else if (code === "not_nursery_item" || code === "not_identified") {
        const pick = await choose(code === "not_nursery_item" ? t("Not a nursery item") : t("Item not identified"), e.message,
          t("Retake photo"), t("Type the name"));
        if (pick === "yes") setTimeout(scan, 0);
      } else {
        toast.show(e.message, "error");
      }
    } finally {
      setScanning(false);
    }
  }

  async function save(body: Record<string, unknown>) {
    setSaving(true);
    try {
      const p: Product = await apiPost("/products", body);
      qc.invalidateQueries({ queryKey: ["products"] });
      qc.invalidateQueries({ queryKey: ["categories"] });
      toast.show(t("Saved {{name}}", { name: p.name }), "success");
      router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  if (form) {
    return (
      <Screen>
        <Header title={t("New product")} onBack={() => setForm(null)} />
        <ProductForm initial={form} isNew onSave={save} saving={saving} />
      </Screen>
    );
  }

  const own = sug.data?.own ?? [];
  const lib = sug.data?.library ?? [];
  return (
    <Screen bg={colors.surface}>
      <Header title={t("Add product")} close />
      <KeyboardAwareScrollView bottomOffset={24} keyboardShouldPersistTaps="handled" contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
        <Field label={t("Type the product name")} value={q} onChangeText={setQ} autoFocus autoCapitalize="words"
          placeholder={t("e.g. Money Plant, Clay pot")} testID="add-search" inputStyle={{ fontSize: 22 }} />

        {query.length >= 2 && sug.isLoading && <ActivityIndicator color={colors.green} />}

        {own.length > 0 && (
          <View style={{ gap: spacing.sm }}>
            <T v="label">{t("Already in your products")}</T>
            {own.map((p) => (
              <View key={p.id} style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, borderRadius: radius.lg, backgroundColor: colors.page }}>
                <Thumb path={p.image_path} size={52} />
                <View style={{ flex: 1 }}>
                  <T v="bold">{p.name}</T>
                  <T v="small" color={p.low_stock ? colors.amber : colors.muted}>
                    {p.out_of_stock ? t("Out of stock") : p.low_stock ? t("Only {{n}} left", { n: formatQty(p.stock) }) : t("{{n}} in stock", { n: formatQty(p.stock) })}
                  </T>
                </View>
                <Button title={t("Add stock")} size="md" variant="secondary"
                  onPress={() => router.replace({ pathname: "/products/[id]", params: { id: p.id } })} testID={`add-own-${p.name}`} />
              </View>
            ))}
          </View>
        )}

        {lib.length > 0 && (
          <View style={{ gap: spacing.sm }}>
            <T v="label">{t("From the shared catalogue")}</T>
            {lib.map((e) => (
              <Pressable key={e.id} onPress={() => fromLibrary(e)} testID={`add-lib-${e.name}`}
                style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 72, borderRadius: radius.lg,
                  borderWidth: 2, borderColor: colors.line, opacity: pressed ? 0.85 : 1 })}>
                <Thumb path={e.image_path} size={52} />
                <View style={{ flex: 1 }}>
                  <T v="bold">{e.name}{e.name_hi ? `  ·  ${e.name_hi}` : ""}</T>
                  <T v="small">{e.default_category}</T>
                </View>
                <Icon name="chevron-right" size={26} color={colors.muted} />
              </Pressable>
            ))}
          </View>
        )}

        {query.length >= 2 && sug.data && (
          <Pressable onPress={() => setForm({ ...emptyProduct, name: query })} testID="add-create-new"
            style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.lg, minHeight: 72, borderRadius: radius.lg, borderWidth: 2,
              borderStyle: "dashed", borderColor: colors.green }}>
            <Icon name="plus-circle" size={32} color={colors.green} />
            <View style={{ flex: 1 }}>
              <T v="bold" color={colors.greenDark}>{t("Create new “{{q}}”", { q: query })}</T>
              <T v="small">{t("Any item: plant, pot, soil or tool")}</T>
            </View>
          </Pressable>
        )}

        <View style={{ height: 8 }} />
        <Button title={scanning ? t("Identifying the item") : t("Identify from a photo")} icon="camera" variant="scan"
          onPress={scan} loading={scanning} testID="add-scan" />
      </KeyboardAwareScrollView>
    </Screen>
  );
}
