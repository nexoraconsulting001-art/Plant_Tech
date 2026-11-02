// Edit a customer's name, phone, address and notes. Bills already made keep the details they were made with.
import { useState } from "react";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiGet, apiPatch } from "@/src/api";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, ErrorView, Field, Header, LoadingView, PhoneField, Screen, T } from "@/src/components/ui";
import { normalizePhone } from "@/src/format";
import { colors, spacing } from "@/src/theme";
import type { Customer } from "@/src/types";

export default function EditCustomer() {
  const { t } = useTranslation();
  const { id } = useLocalSearchParams<{ id: string }>();
  const q = useQuery<Customer>({ queryKey: ["customer", id], queryFn: () => apiGet(`/customers/${id}`) });
  return (
    <Screen bg={colors.surface}>
      <Header title={t("Edit customer")} />
      {q.data ? <Form c={q.data} /> : q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />}
    </Screen>
  );
}

function Form({ c }: { c: Customer }) {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { focus } = useLocalSearchParams<{ focus?: string }>();
  const [name, setName] = useState(c.name);
  const [phone, setPhone] = useState(c.phone.replace(/\D/g, "").slice(-10));
  const [address, setAddress] = useState(c.address ?? "");
  const [notes, setNotes] = useState(c.notes ?? "");
  const [busy, setBusy] = useState(false);
  const phone10 = normalizePhone(phone);

  async function save() {
    setBusy(true);
    try {
      await apiPatch(`/customers/${c.id}`, { name: name.trim(), phone: `+91${phone10}`, address, notes });
      await Promise.all([qc.invalidateQueries({ queryKey: ["customer", c.id] }), qc.invalidateQueries({ queryKey: ["customers"] }),
        qc.invalidateQueries({ queryKey: ["customer-find"] })]);
      toast.show(t("Saved"), "success");
      router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24} keyboardShouldPersistTaps="handled">
        <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" testID="customer-edit-name" />
        <PhoneField label={t("Phone (WhatsApp)")} value={phone} onChangeText={setPhone} testID="customer-edit-phone"
          error={phone.length === 10 && !phone10 ? t("Enter a 10-digit mobile number") : undefined} />
        <Field label={t("Address (optional)")} value={address} onChangeText={setAddress} multiline autoFocus={focus === "address"}
          testID="customer-edit-address" />
        <Field label={t("Notes (optional)")} value={notes} onChangeText={setNotes} multiline placeholder={t("e.g. Likes flowering plants")} />
        <T v="small">{t("Bills already made keep the details they were made with.")}</T>
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Save changes")} icon="content-save" onPress={save} loading={busy} disabled={!name.trim() || !phone10} testID="customer-edit-save" />
      </BottomBar>
    </>
  );
}
