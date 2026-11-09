// New order, step 1: who is buying? Every order has a customer (name + phone), so bills can be sent on
// WhatsApp and each customer's history is complete. Search, or add a new one. Picking a customer opens
// their saved order if they have one (items added earlier, by anyone in the shop): see useStartOrder.
// With ?change=1 (the "Change" on an order): the items move to the customer picked.
// Before anything is typed: today's draft orders (owner, 10 Nov 2026), the orders anyone in the shop started today
// and has not billed (GET /orders/cart/today), newest first; tapping one opens it. Changing an order's customer
// (?change=1) shows the recent customers instead (the empty search, loaded at most every 30 minutes; each bill moves
// its customer to the top of it on the phone, afterBill in order.ts). Typed searches go 300 ms after the last key;
// an overtaken one is cancelled and the last results stay on screen meanwhile.
import { useEffect, useState } from "react";
import { ActivityIndicator, FlatList, Pressable, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiGet, apiPost } from "@/src/api";
import { OrderCustomer, useCart } from "@/src/cart-context";
import { NetStatus } from "@/src/components/net-status";
import { useToast } from "@/src/components/toast";
import { Button, Field, Header, Icon, PhoneField, Screen, SearchBox, T } from "@/src/components/ui";
import { useStartOrder } from "@/src/hooks/useStartOrder";
import { formatCurrency, formatPhone, formatTime, normalizePhone, plural } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";

const RECENT_FOR_MS = 30 * 60_000;
const DRAFTS_FOR_MS = 15_000;

type Draft = { customer: OrderCustomer; lines: number; total: number; updated_at: string; updated_by: string };

export default function OrderCustomerStep() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const cart = useCart();
  const startOrder = useStartOrder();
  const { change } = useLocalSearchParams<{ change?: string }>();
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [address, setAddress] = useState("");
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState<string | null>(null);

  useEffect(() => {
    const id = setTimeout(() => setQ(typed.trim()), 300);
    return () => clearTimeout(id);
  }, [typed]);

  const found = useQuery<OrderCustomer[]>({ queryKey: ["customer-find", q],
    queryFn: ({ signal }) => apiGet(`/customers/find?q=${encodeURIComponent(q)}`, { signal }),
    placeholderData: keepPreviousData, enabled: !!q || !!change, // nothing typed on New order: the drafts show instead
    ...(q ? {} : { staleTime: RECENT_FOR_MS, gcTime: RECENT_FOR_MS }) });
  const showDrafts = !change && !q;
  const drafts = useQuery<Draft[]>({ queryKey: ["drafts-today"], queryFn: ({ signal }) => apiGet("/orders/cart/today", { signal }),
    enabled: !change, staleTime: DRAFTS_FOR_MS });
  const results = found.data ?? [];
  const noMatch = !!q && found.isSuccess && !found.isPlaceholderData && !results.length;
  const rows: { key: string; customer: OrderCustomer; draft?: Draft }[] = showDrafts
    ? (drafts.data ?? []).map((d) => ({ key: d.customer.id, customer: d.customer, draft: d }))
    : results.map((c) => ({ key: c.id, customer: c }));

  async function choose(c: OrderCustomer) {
    if (opening) return;
    setOpening(c.id);
    try {
      if (change) {
        const extra = await cart.moveTo(c);
        if (extra) toast.show(t("{{name}}'s saved items have also been added", { name: c.name }), "success");
        router.back();
        return;
      }
      await startOrder(c, true);
    } finally {
      setOpening(null);
    }
  }

  function startAdding() {
    const digits = normalizePhone(typed);
    setPhone(digits ?? (/^\d+$/.test(typed.trim()) ? typed.trim().slice(-10) : ""));
    setName(digits || /^\d+$/.test(typed.trim()) ? "" : typed.trim());
    setAddress("");
    setAdding(true);
  }

  const phone10 = normalizePhone(phone);
  async function save() {
    setBusy(true);
    try {
      const c = await apiPost("/customers", { name: name.trim(), phone: `+91${phone10}`, address });
      await choose({ id: c.id, name: c.name, phone: c.phone, purchase_count: 0 });
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={change ? t("Change customer") : t("New order")} close={!change} onBack={() => router.back()} testID="customer-close" />
      <View style={{ paddingHorizontal: spacing.lg, gap: spacing.md, paddingBottom: spacing.md }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
          <View style={{ width: 32, height: 32, borderRadius: 16, backgroundColor: colors.green, alignItems: "center", justifyContent: "center" }}>
            <T v="bold" color={colors.white}>1</T>
          </View>
          <T v="title">{t("Who is buying?")}</T>
        </View>
        <NetStatus />
        {!adding && (
          <SearchBox value={typed} onChangeText={setTyped} placeholder={t("Name or phone number")} autoFocus
            loading={found.isFetching} testID="customer-search" />
        )}
      </View>

      {adding ? (
        <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, paddingTop: spacing.xs, gap: spacing.lg }} bottomOffset={24}
          keyboardShouldPersistTaps="handled">
          <T v="bodyLarge">{t("New customer")}</T>
          <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" autoFocus={!name} testID="customer-new-name" />
          <PhoneField label={t("Phone number (WhatsApp)")} value={phone} onChangeText={setPhone} testID="customer-new-phone"
            error={phone.length === 10 && !phone10 ? t("Enter a 10-digit mobile number") : undefined} />
          <Field label={t("Address (optional)")} value={address} onChangeText={setAddress} multiline testID="customer-new-address" />
          <Button title={t("Save and continue")} icon="arrow-right" onPress={save} loading={busy} disabled={!name.trim() || !phone10} testID="customer-new-save" />
          <Button title={t("Back to search")} variant="outline" size="md" onPress={() => setAdding(false)} />
        </KeyboardAwareScrollView>
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={{ paddingHorizontal: spacing.lg, paddingBottom: spacing.xl, gap: spacing.sm }}
          ListHeaderComponent={
            <View style={{ gap: spacing.sm, marginBottom: spacing.xs }}>
              <Pressable onPress={startAdding} testID="customer-add"
                style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.lg, minHeight: 64, borderRadius: radius.lg,
                  borderWidth: 2, borderStyle: "dashed", borderColor: colors.green, backgroundColor: pressed ? colors.greenTint : colors.surface })}>
                <Icon name="account-plus" size={28} color={colors.green} />
                <View style={{ flex: 1 }}>
                  <T v="bold" color={colors.greenDark}>{typed.trim() ? t("Add “{{q}}” as a new customer", { q: typed.trim() }) : t("New customer")}</T>
                  <T v="small">{t("Name and phone number")}</T>
                </View>
              </Pressable>
              {noMatch && <T v="small">{t("No customer found with that name or number.")}</T>}
              {showDrafts && drafts.isSuccess && (
                rows.length > 0 ? <T v="label">{t("Today's draft orders")}</T>
                  : <T v="small" testID="drafts-none">{t("No draft orders today. Search for the customer above, or add a new one.")}</T>
              )}
              {showDrafts && drafts.isLoading && <ActivityIndicator color={colors.green} style={{ marginTop: spacing.md }} />}
              {!showDrafts && !q && !found.isPlaceholderData && results.length > 0 && <T v="label">{t("Recent customers")}</T>}
            </View>
          }
          renderItem={({ item: { customer: c, draft } }) => (
            <Pressable onPress={() => choose(c)} testID={draft ? `draft-${c.name}` : `customer-pick-${c.name}`}
              style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 68, borderRadius: radius.lg,
                borderWidth: 1, borderColor: cart.customer?.id === c.id ? colors.green : colors.line, backgroundColor: pressed ? colors.page : colors.surface })}>
              <View style={{ width: 44, height: 44, borderRadius: 22, backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
                <T v="number" color={colors.greenDark}>{c.name.charAt(0).toUpperCase()}</T>
              </View>
              <View style={{ flex: 1 }}>
                <T v="bold">{c.name}</T>
                {draft ? (
                  <T v="small">{t("{{items}} · {{total}}", { items: plural(draft.lines, "1 item", "{{n}} items"), total: formatCurrency(draft.total) })}</T>
                ) : <T v="small">{formatPhone(c.phone)}</T>}
                {!!draft && (
                  <T v="caption">{draft.updated_by
                    ? t("{{name}} at {{time}}", { name: draft.updated_by, time: formatTime(draft.updated_at) })
                    : formatTime(draft.updated_at)}</T>
                )}
              </View>
              {!draft && !!c.purchase_count && <T v="caption">{plural(c.purchase_count, "1 order", "{{n}} orders")}</T>}
              {opening === c.id ? <ActivityIndicator color={colors.green} /> : <Icon name="chevron-right" size={24} color={colors.muted} />}
            </Pressable>
          )}
        />
      )}
    </Screen>
  );
}
