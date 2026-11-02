// Edit a product's name, category, price, picture. The owner can also delete it.
import { useState } from "react";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiDelete, apiGet, apiPut } from "@/src/api";
import { useAuth } from "@/src/auth";
import { confirm } from "@/src/confirm";
import { ProductForm } from "@/src/components/product-form";
import { useToast } from "@/src/components/toast";
import { Button, Header, LoadingView, Screen } from "@/src/components/ui";
import type { Product } from "@/src/types";

export default function EditProduct() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { isOwner } = useAuth();
  const { id } = useLocalSearchParams<{ id: string }>();
  const q = useQuery<Product>({ queryKey: ["product", id], queryFn: () => apiGet(`/products/${id}`) });
  const [saving, setSaving] = useState(false);
  const p = q.data;

  async function save(body: Record<string, unknown>) {
    setSaving(true);
    try {
      await apiPut(`/products/${id}`, body);
      await Promise.all([qc.invalidateQueries({ queryKey: ["products"] }), qc.invalidateQueries({ queryKey: ["product", id] }),
        qc.invalidateQueries({ queryKey: ["categories"] })]);
      toast.show(t("Saved"), "success");
      router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!p || !(await confirm(t("Delete {{name}}?", { name: p.name }), t("It will be removed from your products. Existing bills are not affected."), t("Delete"), true))) return;
    try {
      await apiDelete(`/products/${id}`);
      qc.invalidateQueries({ queryKey: ["products"] });
      router.dismissTo("/(tabs)/products");
    } catch (e: any) {
      toast.show(e.message, "error");
    }
  }

  return (
    <Screen>
      <Header title={t("Edit product")} />
      {!p ? <LoadingView /> : (
        <ProductForm isNew={false} onSave={save} saving={saving}
          initial={{ name: p.name, botanical_name: p.botanical_name, category_id: p.category_id, library_id: p.library_id,
            photo_path: p.photo_path, library_image: p.image_source === "library" ? p.image_path : null,
            selling_price: String(p.selling_price), stock: String(p.stock), cost_price: p.cost_price ? String(p.cost_price) : "",
            min_stock: String(p.min_stock), unit: p.unit ?? "pcs" }}
          extra={isOwner ? <Button title={t("Delete product")} icon="trash-can-outline" variant="danger" size="md" onPress={remove} testID="product-delete" /> : undefined} />
      )}
    </Screen>
  );
}
