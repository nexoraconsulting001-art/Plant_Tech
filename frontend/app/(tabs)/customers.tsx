// Customers: saved automatically when a bill has a phone number, or added here. A shop can have tens of
// thousands, so the list comes 50 at a time as it scrolls (every customer is reachable) and search runs on
// the server: a name from the start of any word, or 3+ digits from the start or end of a phone number.
import { useEffect, useRef, useState } from "react";
import { Pressable, RefreshControl, View } from "react-native";
import { FlashList, type FlashListRef } from "@shopify/flash-list";
import { useRouter } from "expo-router";
import { useTranslation } from "react-i18next";

import { MoreFooter, RowGap } from "@/src/components/paged-list";
import { Button, EmptyState, ErrorView, LoadingView, Screen, SearchBox, T } from "@/src/components/ui";
import { formatCurrency, formatPhone } from "@/src/format";
import { usePagedList } from "@/src/hooks/usePagedList";
import { useRefreshOnFocus } from "@/src/hooks/useRefreshOnFocus";
import { colors, radius, spacing } from "@/src/theme";
import type { Customer } from "@/src/types";

const PAGE = 50;
const KEY = ["customers"] as const;

export default function Customers() {
  const { t } = useTranslation();
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [typed, setTyped] = useState(""); // what was typed, a moment after typing stops
  useEffect(() => {
    const id = setTimeout(() => setTyped(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);
  const term = search.trim() ? typed : "";
  const q = usePagedList<Customer>(KEY, "/customers", { search: term }, { limit: PAGE });
  const list = useRef<FlashListRef<Customer>>(null);
  useEffect(() => list.current?.scrollToOffset({ offset: 0, animated: false }), [term]); // a new search starts at the top
  useRefreshOnFocus(q.refreshIfStale);
  const total = q.total ?? (q.hasNextPage ? undefined : q.items.length);

  return (
    <Screen edgeBottom={false}>
      <View style={{ paddingHorizontal: spacing.lg, paddingTop: spacing.md, paddingBottom: spacing.md, gap: spacing.md, backgroundColor: colors.surface }}>
        <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
          <T v="title">{t("Customers")}</T>
          <Button title={t("Add")} icon="account-plus" size="md" onPress={() => router.push("/customers/new")} testID="customers-add" />
        </View>
        <SearchBox value={search} onChangeText={setSearch} placeholder={t("Name or phone")} loading={!!term && q.isFetching && !q.isFetchingNextPage}
          testID="customers-search" />
      </View>
      {q.isLoading ? <LoadingView /> : q.isError && !q.items.length ? <ErrorView message={q.error.message} onRetry={q.refetch} /> : (
        <FlashList
          ref={list}
          data={q.items}
          keyExtractor={(c) => c.id}
          contentContainerStyle={{ padding: spacing.lg }}
          ItemSeparatorComponent={RowGap}
          onEndReached={q.more}
          onEndReachedThreshold={0.5}
          refreshControl={<RefreshControl refreshing={q.isRefetching && !q.isFetchingNextPage} onRefresh={q.refresh} tintColor={colors.green} />}
          ListHeaderComponent={total ? (
            <T v="label" style={{ marginBottom: spacing.sm }} testID="customers-count">
              {total === 1 ? t("1 customer") : t("{{n}} customers", { n: total })}
            </T>
          ) : null}
          ListEmptyComponent={<EmptyState icon="account-group" title={term ? t("Nothing matches") : t("No customers yet")}
            subtitle={term ? undefined : t("Customers are saved when you start a new order and will appear here.")} />}
          ListFooterComponent={<MoreFooter loading={q.isFetchingNextPage} failed={q.isFetchNextPageError} onRetry={() => q.fetchNextPage()} />}
          renderItem={({ item: c }) => (
            <Pressable testID={`customer-${c.name}`} onPress={() => router.push({ pathname: "/customers/[id]", params: { id: c.id } })}
              style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 72, borderRadius: radius.lg,
                backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, opacity: pressed ? 0.85 : 1 })}>
              <View style={{ width: 48, height: 48, borderRadius: 24, backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
                <T v="number" color={colors.greenDark}>{c.name.charAt(0).toUpperCase()}</T>
              </View>
              <View style={{ flex: 1, minWidth: 0 }}>
                <T v="bold">{c.name}</T>
                <T v="small">{formatPhone(c.phone) || t("No phone")}</T>
              </View>
              <View style={{ alignItems: "flex-end" }}>
                <T v="bold">{formatCurrency(c.total_purchases)}</T>
                <T v="caption">{c.purchase_count === 1 ? t("1 visit") : t("{{n}} visits", { n: c.purchase_count })}</T>
              </View>
            </Pressable>
          )}
        />
      )}
    </Screen>
  );
}
