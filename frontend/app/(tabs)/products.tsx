// Products: search plus Category, Stock and Sort filters. A product stays here at 0 stock
// (marked Out of stock) until the owner deletes it. Search runs on the server, so Hindi, Hinglish and
// English names all find a product ("गुलाब", "gulab", "rose"). Stock below zero (sold without enough
// stock, owner only) shows in red and has its own filter; Home's "Sold below stock" opens it (?stock=below).
import { useEffect, useMemo, useState } from "react";
import { FlatList, Pressable, RefreshControl, ScrollView, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useCategories } from "@/src/components/category-picker";
import { FilterChip, OptionRow, Sheet } from "@/src/components/sheet";
import { Badge, BottomBar, Button, EmptyState, ErrorView, LoadingView, Screen, SearchBox, T, Thumb } from "@/src/components/ui";
import { formatCurrency, formatQty } from "@/src/format";
import { useRefreshOnFocus } from "@/src/hooks/useRefreshOnFocus";
import { colors, radius, spacing } from "@/src/theme";
import type { Product } from "@/src/types";

type StockFilter = "" | "in" | "low" | "out" | "below";
type Sort = "name" | "price-up" | "price-down" | "stock-up" | "new";

export default function Products() {
  const { t } = useTranslation();
  const router = useRouter();
  const { can } = useAuth();
  const { stock: asked, at } = useLocalSearchParams<{ stock?: string; at?: string }>(); // at: each tap on Home sets it again
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [stock, setStock] = useState<StockFilter>("");
  const [openedAt, setOpenedAt] = useState<string | undefined>(undefined); // the Home tap already applied
  if (asked === "below" && at !== openedAt) {
    setOpenedAt(at);
    setStock("below");
  }
  const [sort, setSort] = useState<Sort>("name");
  const [sheet, setSheet] = useState<"" | "category" | "stock" | "sort">("");
  const q = useQuery<Product[]>({ queryKey: ["products"], queryFn: () => apiGet("/products") });
  const cats = useCategories();
  useRefreshOnFocus(q.refetch);
  const [typed, setTyped] = useState(""); // what was typed, a moment after typing stops
  useEffect(() => {
    const id = setTimeout(() => setTyped(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);
  const term = search.trim() ? typed : "";
  const found = useQuery<Product[]>({
    queryKey: ["products", "search", term], queryFn: () => apiGet(`/products?search=${encodeURIComponent(term)}`),
    enabled: !!term, placeholderData: keepPreviousData,
  });

  const all = useMemo(() => q.data ?? [], [q.data]);
  const counts = {
    low: all.filter((p) => p.low_stock && !p.out_of_stock).length,
    out: all.filter((p) => p.out_of_stock).length,
    below: all.filter((p) => p.stock < 0).length,
  };
  const shown = useMemo(() => {
    const list = (term ? found.data ?? [] : all).filter((p) =>
      (!category || p.category_id === category)
      && (stock === "" || (stock === "in" && !p.low_stock) || (stock === "low" && p.low_stock && !p.out_of_stock) || (stock === "out" && p.out_of_stock)
        || (stock === "below" && p.stock < 0)));
    const by: Record<Sort, (a: Product, b: Product) => number> = {
      name: (a, b) => a.name.localeCompare(b.name),
      "price-up": (a, b) => a.selling_price - b.selling_price,
      "price-down": (a, b) => b.selling_price - a.selling_price,
      "stock-up": (a, b) => a.stock - b.stock,
      new: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    };
    return term && sort === "name" ? list : [...list].sort(by[sort]); // a search lists the best matches first
  }, [all, found.data, term, category, stock, sort]);

  const STOCK: { v: StockFilter; label: string; sub?: string }[] = [
    { v: "", label: t("All stock") },
    { v: "in", label: t("In stock"), sub: t("Above the alert level") },
    { v: "low", label: t("Low stock · {{n}}", { n: counts.low }), sub: t("At or below the low-stock alert level") },
    { v: "out", label: t("Out of stock · {{n}}", { n: counts.out }), sub: counts.below ? t("Includes stock below zero") : undefined },
    ...(counts.below || stock === "below"
      ? [{ v: "below" as const, label: t("Below zero · {{n}}", { n: counts.below }), sub: t("Sold without enough stock") }] : []),
  ];
  const SORTS: { v: Sort; label: string }[] = [
    { v: "name", label: t("Name A–Z") }, { v: "price-up", label: t("Price: low to high") }, { v: "price-down", label: t("Price: high to low") },
    { v: "stock-up", label: t("Stock: lowest first") }, { v: "new", label: t("Newest first") },
  ];
  const catName = t(cats.data?.find((c) => c.id === category)?.name ?? "");
  const filtered = !!(search || category || stock);

  return (
    <Screen edgeBottom={false}>
      <View style={{ paddingHorizontal: spacing.lg, paddingTop: spacing.md, paddingBottom: spacing.md, gap: spacing.md, backgroundColor: colors.surface,
        borderBottomWidth: 1, borderBottomColor: colors.line }}>
        <T v="title">{t("Products")}</T>
        <SearchBox value={search} onChangeText={setSearch} placeholder={t("Search name or code")} loading={found.isFetching} testID="products-search" />
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: spacing.sm }}>
          <FilterChip label={t("Category")} value={catName} onPress={() => setSheet("category")} testID="products-filter-category" />
          <FilterChip label={t("Stock")} value={stock ? STOCK.find((x) => x.v === stock)!.label : ""}
            onPress={() => setSheet("stock")} testID="products-filter-stock" />
          <FilterChip label={t("Sort")} value={sort !== "name" ? SORTS.find((x) => x.v === sort)!.label : ""}
            onPress={() => setSheet("sort")} testID="products-filter-sort" />
        </ScrollView>
      </View>

      {q.isLoading ? <LoadingView /> : q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : (
        <FlatList
          data={shown}
          keyExtractor={(p) => p.id}
          contentContainerStyle={{ padding: spacing.lg, gap: spacing.sm }}
          refreshControl={<RefreshControl refreshing={q.isRefetching} onRefresh={q.refetch} tintColor={colors.green} />}
          ListHeaderComponent={all.length ? (
            <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm, minHeight: 32 }}>
              <T v="label" style={{ flexShrink: 1 }} testID="products-count">
                {filtered ? t("{{n}} of {{all}} products", { n: shown.length, all: all.length })
                  : t("{{all}} products · {{low}} low · {{out}} out of stock", { all: all.length, low: counts.low, out: counts.out })}
              </T>
              {filtered && (
                <Pressable onPress={() => { setSearch(""); setCategory(""); setStock(""); }} hitSlop={8} testID="products-clear">
                  <T v="bold" color={colors.red}>{t("Clear all")}</T>
                </Pressable>
              )}
            </View>
          ) : null}
          ListEmptyComponent={
            <EmptyState icon="sprout" title={all.length ? t("Nothing matches") : t("No products yet")}
              subtitle={all.length ? t("Try different filters, or tap Clear all.") : t("Add the plants, pots and other items you sell.")} />
          }
          renderItem={({ item: p }) => (
            <Pressable testID={`product-${p.name}`} onPress={() => router.push({ pathname: "/products/[id]", params: { id: p.id } })}
              style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 76, borderRadius: radius.lg,
                backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, opacity: pressed ? 0.85 : 1 })}>
              <Thumb path={p.image_path} size={56} />
              <View style={{ flex: 1, minWidth: 0, gap: 2 }}>
                <T v="bold">{p.name}</T>
                <T v="small">{[formatCurrency(p.selling_price), p.category && t(p.category)].filter(Boolean).join(" · ")}</T>
              </View>
              <View style={{ alignItems: "flex-end" }}>
                {p.stock < 0 ? <><T v="number" color={colors.red}>{formatQty(p.stock)}</T><T v="caption" color={colors.red}>{t("below zero")}</T></>
                  : p.out_of_stock ? <Badge text={t("Out of stock")} tone="red" />
                  : p.low_stock ? <Badge text={t("Low · {{n}}", { n: formatQty(p.stock) })} tone="amber" />
                  : <><T v="number">{formatQty(p.stock)}</T><T v="caption">{t("in stock")}</T></>}
              </View>
            </Pressable>
          )}
        />
      )}

      {can("stock") && (
        <BottomBar style={{ flexDirection: "row" }}>
          <Button title={t("Add product")} icon="plus" size="md" style={{ flex: 1, paddingHorizontal: spacing.sm }} onPress={() => router.push("/products/new")} testID="products-add" />
          <Button title={t("Supplier bill")} icon="file-document-outline" variant="scan" size="md" style={{ flex: 1, paddingHorizontal: spacing.sm }}
            onPress={() => router.push("/more/supplier-bill")} testID="products-supplier-bill" />
        </BottomBar>
      )}

      <Sheet visible={sheet === "category"} onClose={() => setSheet("")} title={t("Category")}>
        <OptionRow label={t("All categories")} selected={!category} onPress={() => { setCategory(""); setSheet(""); }} />
        {(cats.data ?? []).map((c) => (
          <OptionRow key={c.id} label={t(c.name)} sub={c.product_count === 1 ? t("1 product") : t("{{n}} products", { n: c.product_count ?? 0 })}
            selected={category === c.id} onPress={() => { setCategory(c.id); setSheet(""); }} testID={`products-cat-${c.name}`} />
        ))}
      </Sheet>
      <Sheet visible={sheet === "stock"} onClose={() => setSheet("")} title={t("Stock")}>
        {STOCK.map((x) => (
          <OptionRow key={x.v || "all"} label={x.label} sub={x.sub} selected={stock === x.v} onPress={() => { setStock(x.v); setSheet(""); }}
            testID={`products-stock-${x.v || "all"}`} />
        ))}
      </Sheet>
      <Sheet visible={sheet === "sort"} onClose={() => setSheet("")} title={t("Sort")}>
        {SORTS.map((x) => (
          <OptionRow key={x.v} label={x.label} selected={sort === x.v} onPress={() => { setSort(x.v); setSheet(""); }} testID={`products-sort-${x.v}`} />
        ))}
      </Sheet>
    </Screen>
  );
}
