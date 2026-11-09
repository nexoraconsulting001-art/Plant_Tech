// Categories: rename, GST % (only when GST registered), add, delete an unused one.
import { useState } from "react";
import { Pressable, View } from "react-native";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiDelete, apiPatch, apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useCategories } from "@/src/components/category-picker";
import { confirm } from "@/src/confirm";
import { useToast } from "@/src/components/toast";
import { Button, Chip, Field, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { colors, radius, spacing } from "@/src/theme";
import type { Category } from "@/src/types";

// What shops normally use since GST 2.0 (Sep 2025), plus the older 12% / 28%.
const SLABS = [0, 5, 12, 18, 28, 40];

export default function Categories() {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const { user } = useAuth();
  const gst = !!user?.business?.gst_registered;
  const cats = useCategories();
  const [open, setOpen] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  async function call(fn: () => Promise<unknown>) {
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["products"] });
    } catch (e: any) {
      toast.show(e.message, "error");
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Categories")} />
      <KeyboardAwareScrollView bottomOffset={24} contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }} keyboardShouldPersistTaps="handled">
        <T v="body" color={colors.inkSoft}>
          {gst ? t("Tap a category to rename it or change its GST rate. Prices include GST, and the bill shows the tax portion.")
            : t("Your shop is not GST registered, so no tax is shown on bills. Tap a category to rename it.")}
        </T>
        {cats.isLoading && <LoadingView />}
        {(cats.data ?? []).map((c) => (
          <CategoryRow key={c.id} c={c} gst={gst} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)}
            onPatch={(body) => call(() => apiPatch(`/categories/${c.id}`, body))}
            onDelete={async () => {
              if (await confirm(t("Delete {{name}}?", { name: c.name }), t("A category can only be deleted when no product uses it."), t("Delete"), true)) {
                call(() => apiDelete(`/categories/${c.id}`));
              }
            }} />
        ))}
        <View style={{ flexDirection: "row", gap: spacing.sm, alignItems: "flex-end", marginTop: spacing.sm }}>
          <Field style={{ flex: 1 }} label={t("Add category")} value={newName} onChangeText={setNewName} placeholder={t("e.g. Seeds")} autoCapitalize="words" testID="cat-add-name" />
          <Button title={t("Add")} icon="plus" size="md" disabled={!newName.trim()} testID="cat-add"
            onPress={() => call(async () => { await apiPost("/categories", { name: newName.trim(), gst_percent: 0 }); setNewName(""); })} />
        </View>
      </KeyboardAwareScrollView>
    </Screen>
  );
}

function CategoryRow({ c, gst, open, onToggle, onPatch, onDelete }: {
  c: Category; gst: boolean; open: boolean; onToggle: () => void; onPatch: (b: object) => void; onDelete: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(c.name);
  return (
    <View style={{ borderRadius: radius.lg, borderWidth: open ? 2 : 1, borderColor: open ? colors.green : colors.line, overflow: "hidden" }}>
      <Pressable onPress={onToggle} testID={`cat-row-${c.name}`}
        style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.lg, minHeight: 68 }}>
        <View style={{ flex: 1 }}>
          <T v="bold">{t(c.name)}</T>
          <T v="small">{c.product_count === 1 ? t("1 product") : t("{{n}} products", { n: c.product_count ?? 0 })}</T>
        </View>
        {gst && (
          <View style={{ paddingHorizontal: spacing.md, paddingVertical: 6, borderRadius: 14, backgroundColor: c.gst_percent ? colors.greenTint : colors.page }}>
            <T v="bold" color={c.gst_percent ? colors.greenDark : colors.inkSoft}>{t("{{n}}% GST", { n: c.gst_percent })}</T>
          </View>
        )}
        <Icon name={open ? "chevron-up" : "chevron-down"} size={26} color={colors.muted} />
      </Pressable>
      {open && (
        <View style={{ gap: spacing.md, padding: spacing.lg, paddingTop: 0 }}>
          {gst && (
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
              {SLABS.map((s) => (
                <Chip key={s} label={`${s}%`} active={c.gst_percent === s} onPress={() => onPatch({ gst_percent: s })} testID={`cat-gst-${c.name}-${s}`} />
              ))}
            </View>
          )}
          <View style={{ flexDirection: "row", gap: spacing.sm, alignItems: "flex-end" }}>
            <Field style={{ flex: 1 }} label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" />
            <Button title={t("Rename")} size="md" variant="secondary" disabled={!name.trim() || name.trim() === c.name} onPress={() => onPatch({ name: name.trim() })} />
          </View>
          {!c.product_count && <Button title={t("Delete category")} icon="trash-can-outline" variant="danger" size="md" onPress={onDelete} />}
        </View>
      )}
    </View>
  );
}
