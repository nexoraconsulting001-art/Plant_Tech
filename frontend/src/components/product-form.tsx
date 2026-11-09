// Product details form, used by Add product and Edit product.
// Picture: the shop's own photo wins; otherwise the shared library picture is shown.
// Sold by: pieces (whole numbers) or kg / litre / metre (decimals); plants are always pieces.
import { useState } from "react";
import { ActivityIndicator, Pressable, View } from "react-native";
import { Image } from "expo-image";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { fileUrl, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { CategoryPicker, useCategories } from "@/src/components/category-picker";
import { OptionRow, SelectField, Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Field, Icon, T } from "@/src/components/ui";
import { formatCurrency } from "@/src/format";
import { pickFromGallery, takePhoto } from "@/src/hooks/useImageCapture";
import { LONG_EDGE } from "@/src/image-tools";
import { colors, radius, spacing } from "@/src/theme";
import { isPlantCategory, roundQty, Unit, UNITS, unitLabel, unitName, unitShort, wholeUnit } from "@/src/units";

export type ProductValues = {
  name: string; botanical_name: string; category_id: string | null; library_id: string | null;
  photo_path: string | null; library_image: string | null;
  selling_price: string; stock: string; cost_price: string; min_stock: string;
  unit: Unit; plant?: boolean; // plant: the photo was of a plant (always pieces)
  other_names?: string; // the owner's other names for it, separated by commas (Tulsi, Holy Basil)
};

export const emptyProduct: ProductValues = {
  name: "", botanical_name: "", category_id: null, library_id: null, photo_path: null, library_image: null,
  selling_price: "", stock: "", cost_price: "", min_stock: "5", unit: "pcs",
};

const num = (v: string) => v.replace(/[^0-9.]/g, "");
const OTHER_NAMES_MAX = 10; // as the server keeps (products.OTHER_NAMES_MAX)
const otherNames = (v = "") => v.split(/[,\n]/).map((x) => x.trim()).filter(Boolean).slice(0, OTHER_NAMES_MAX);
const digits = (v: string) => v.replace(/\D/g, "");

export function ProductForm({ initial, isNew, onSave, saving, extra }: {
  initial: ProductValues; isNew: boolean; saving: boolean; extra?: React.ReactNode;
  onSave: (body: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isOwner, user } = useAuth();
  const [v, setV] = useState<ProductValues>(initial);
  const [uploading, setUploading] = useState(false);
  const set = (patch: Partial<ProductValues>) => setV((o) => ({ ...o, ...patch }));
  const gst = !!user?.business?.gst_registered;
  const profit = user?.business?.profit_percent ?? 60;
  const cats = useCategories();
  const plant = !!v.plant || isPlantCategory(cats.data?.find((c) => c.id === v.category_id)?.name);
  const unit: Unit = plant ? "pcs" : v.unit;
  const whole = wholeUnit(unit);
  const count = whole ? digits : num; // stock typed in whole numbers for pieces

  async function photo(source: "camera" | "gallery") {
    const out = source === "camera" ? await takePhoto({ aspect: 1, longEdge: LONG_EDGE.plant }) : await pickFromGallery({ aspect: 1, longEdge: LONG_EDGE.plant });
    if (out.status === "denied") toast.show(t("Please allow camera and photo access for Hara Hisaab in your phone settings."), "error");
    if (out.status !== "ok") return;
    setUploading(true);
    try {
      set({ photo_path: await uploadImage(out.result.base64) });
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setUploading(false);
    }
  }

  const picture = v.photo_path || v.library_image;
  // Owner typed a cost but no price yet: offer cost plus the shop's profit % (never filled in silently).
  const cost = Number(v.cost_price);
  const suggested = isOwner && v.selling_price === "" && cost > 0 ? Math.round(cost * (1 + profit / 100)) : null;
  const ok = !!v.name.trim() && v.selling_price !== "" && !uploading;

  function save() {
    const body: Record<string, unknown> = {
      name: v.name.trim(), botanical_name: v.botanical_name, category_id: v.category_id, photo_path: v.photo_path,
      selling_price: Number(v.selling_price) || 0, min_stock: roundQty(Number(v.min_stock) || 0, whole), unit,
      other_names: otherNames(v.other_names),
    };
    if (isOwner && v.cost_price !== "") body.cost_price = Number(v.cost_price) || 0;
    if (isNew) {
      body.stock = roundQty(Number(v.stock) || 0, whole);
      body.library_id = v.library_id;
    }
    onSave(body);
  }

  return (
    <>
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24} keyboardShouldPersistTaps="handled">
        <Section title={t("Picture")}>
          <View style={{ flexDirection: "row", gap: spacing.lg }}>
            <Pressable onPress={() => photo("gallery")} accessibilityLabel={t("Choose a picture")}
              style={{ width: 112, height: 112, borderRadius: radius.lg, overflow: "hidden", backgroundColor: colors.greenTint,
                alignItems: "center", justifyContent: "center", borderWidth: picture ? 0 : 2, borderStyle: "dashed", borderColor: colors.greenMid }}>
              {picture ? <Image source={{ uri: fileUrl(picture) }} style={{ width: "100%", height: "100%" }} contentFit="cover" />
                : uploading ? <ActivityIndicator color={colors.green} /> : <Icon name="camera-plus-outline" size={40} color={colors.greenMid} />}
            </Pressable>
            <View style={{ flex: 1, gap: spacing.sm, justifyContent: "center" }}>
              <View>
                <T v="bold">{uploading ? t("Uploading") : v.photo_path ? t("Your own photo") : v.library_image ? t("Shared picture") : t("No picture yet")}</T>
                <T v="caption">{v.photo_path ? t("Shown on bills and in your list") : v.library_image ? t("Add your own photo to use it instead") : t("Helps customers recognise items")}</T>
              </View>
              <Button title={t("Take photo")} icon="camera" variant="outline" size="md" style={{ paddingHorizontal: spacing.md, minHeight: 50 }} onPress={() => photo("camera")} />
              <Button title={t("From gallery")} icon="image-outline" variant="outline" size="md" style={{ paddingHorizontal: spacing.md, minHeight: 50 }} onPress={() => photo("gallery")} />
            </View>
          </View>
        </Section>

        <Section title={t("Details")}>
          <Field label={t("Product name")} value={v.name} onChangeText={(name) => set({ name })} autoCapitalize="words" testID="product-name"
            placeholder={t("e.g. Money Plant")} />
          <Field label={t("Other names (optional)")} value={v.other_names ?? ""} onChangeText={(other_names) => set({ other_names })}
            autoCapitalize="words" testID="product-other-names" placeholder={t("e.g. Pothos, Golden Pothos")}
            hint={t("Other names customers use, separated by commas. Search and scans find the product by these too.")} />
          <CategoryPicker value={v.category_id} onChange={(category_id) => set({ category_id })} />
          {plant ? (
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }} testID="product-unit-plant">
              <Icon name="sprout" size={20} color={colors.greenMid} />
              <T v="small" style={{ flex: 1 }}>{t("Plants are sold by the piece.")}</T>
            </View>
          ) : <UnitPicker value={unit} onChange={(u) => set({ unit: u })} />}
        </Section>

        <Section title={t("Price and stock")}>
          <Field label={gst ? t("Selling price ₹ (includes GST)") : t("Selling price ₹")} value={v.selling_price} keyboardType="decimal-pad"
            onChangeText={(x) => set({ selling_price: num(x) })} placeholder="0" testID="product-price" inputStyle={{ fontSize: 26 }}
            hint={whole ? undefined : t("Price for 1 {{unit}}", { unit: unitShort(unit) })} />
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            {isNew && (
              <Field style={{ flex: 1 }} label={unitLabel(unit) ? `${t("Stock now")} · ${unitLabel(unit)}` : t("Stock now")} value={v.stock} keyboardType={whole ? "number-pad" : "decimal-pad"}
                onChangeText={(x) => set({ stock: count(x) })} placeholder="0" testID="product-stock" />
            )}
            <Field style={{ flex: 1 }} label={unitLabel(unit) ? `${t("Low-stock alert at")} · ${unitLabel(unit)}` : t("Low-stock alert at")} value={v.min_stock} keyboardType={whole ? "number-pad" : "decimal-pad"}
              onChangeText={(x) => set({ min_stock: count(x) })} testID="product-min" />
          </View>
          {isOwner && (
            <Field label={t("Cost price ₹")} hint={t("Used for profit in reports. Visible only to you.")} value={v.cost_price} keyboardType="decimal-pad"
              onChangeText={(x) => set({ cost_price: num(x) })} placeholder="0" testID="product-cost" />
          )}
          {suggested !== null && (
            <Pressable onPress={() => set({ selling_price: String(suggested) })} testID="product-suggested-price" accessibilityRole="button"
              style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, borderRadius: radius.md, backgroundColor: colors.greenTint }}>
              <Icon name="tag-outline" size={22} color={colors.green} />
              <T v="small" color={colors.greenDark} style={{ flex: 1 }}>
                {t("Suggested selling price {{price}} (cost plus {{p}}%)", { price: formatCurrency(suggested), p: profit })}
              </T>
              <T v="bold" color={colors.green}>{t("Use")}</T>
            </Pressable>
          )}
        </Section>
        {extra}
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Save product")} icon="content-save" onPress={save} loading={saving} disabled={!ok} testID="product-save" />
      </BottomBar>
    </>
  );
}

// "Sold by": one field that opens the list of units.
function UnitPicker({ value, onChange }: { value: Unit; onChange: (u: Unit) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <SelectField label={t("Sold by")} value={unitName(value)} onPress={() => setOpen(true)} testID="product-unit" />
      <Sheet visible={open} onClose={() => setOpen(false)} title={t("Sold by")}>
        {UNITS.map((u) => (
          <OptionRow key={u} label={unitName(u) === unitShort(u) ? unitName(u) : `${unitName(u)} · ${unitShort(u)}`} selected={value === u} testID={`unit-${u}`}
            sub={wholeUnit(u) ? t("Whole numbers, like 40") : t("Decimals allowed, like 1.5")}
            onPress={() => { onChange(u); setOpen(false); }} />
        ))}
      </Sheet>
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line }}>
      <T v="label" style={{ fontSize: 14, letterSpacing: 0.6, textTransform: "uppercase", color: colors.muted }}>{title}</T>
      {children}
    </View>
  );
}

// Big tappable row used in Add product suggestions.
export function SuggestBox({ children }: { children: React.ReactNode }) {
  return <View style={{ gap: spacing.sm, padding: spacing.md, borderRadius: radius.lg, backgroundColor: colors.page }}>{children}</View>;
}
