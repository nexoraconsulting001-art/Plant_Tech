// Add or edit a supplier (a sheet). The server refuses a second record for the same supplier
// (same name, phone or GSTIN) and says which one already exists.
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiPatch, apiPost } from "@/src/api";
import { Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Button, Field } from "@/src/components/ui";
import type { Supplier } from "@/src/types";

const GSTIN = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$/;
type Values = { name: string; phone: string; gstin: string; address: string; notes: string };
const EMPTY: Values = { name: "", phone: "", gstin: "", address: "", notes: "" };

type Props = { visible: boolean; onClose: () => void; supplier?: Supplier; onSaved?: (s: Supplier) => void };

// Mounted only while open, so every opening starts from the saved values.
export function SupplierForm(props: Props) {
  return props.visible ? <FormSheet {...props} /> : null;
}

function FormSheet({ visible, onClose, supplier, onSaved }: Props) {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const [v, setV] = useState<Values>(() => (supplier
    ? { name: supplier.name, phone: supplier.phone, gstin: supplier.gstin, address: supplier.address, notes: supplier.notes } : EMPTY));
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<Values>) => setV((o) => ({ ...o, ...patch }));
  const gstinBad = !!v.gstin && !GSTIN.test(v.gstin);

  async function save() {
    setBusy(true);
    try {
      const body = { ...v, name: v.name.trim() };
      const s: Supplier = supplier ? await apiPatch(`/suppliers/${supplier.id}`, body) : await apiPost("/suppliers", body);
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      qc.invalidateQueries({ queryKey: ["supplier"] });
      toast.show(t("Saved"), "success");
      onSaved?.(s);
      onClose();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet visible={visible} onClose={onClose} title={supplier ? t("Edit supplier") : t("Add supplier")} testID="supplier-form"
      footer={<Button title={t("Save")} icon="content-save" onPress={save} loading={busy} disabled={!v.name.trim() || gstinBad} testID="supplier-save" />}>
      <Field label={t("Name")} value={v.name} onChangeText={(name) => set({ name })} autoCapitalize="words" testID="supplier-form-name" />
      <Field label={t("Phone")} value={v.phone} onChangeText={(phone) => set({ phone })} keyboardType="phone-pad" testID="supplier-form-phone" />
      <Field label={t("GST number")} value={v.gstin} onChangeText={(g) => set({ gstin: g.toUpperCase() })} autoCapitalize="characters" maxLength={15}
        placeholder="06ABCDE1234F1Z4" error={gstinBad ? t("This does not look like a valid GST number") : undefined} />
      <Field label={t("Address")} value={v.address} onChangeText={(address) => set({ address })} multiline />
      <Field label={t("Notes (optional)")} value={v.notes} onChangeText={(notes) => set({ notes })} multiline
        placeholder={t("Contact person, bank or UPI details, payment terms")} />
    </Sheet>
  );
}
