// Products: search plus Category, Stock and Sort filters. A product stays here at 0 stock
// (marked Out of stock) until the owner deletes it. Search runs on the server, so Hindi, Hinglish and
// English names all find a product ("गुलाब", "gulab", "rose"). Stock below zero (sold without enough
// stock, owner only) shows in red and has its own filter. Home's cards and the low-stock notification open a filter
// (?stock=below / low / out).
// The list comes from the server a page at a time, by name (GET /products?limit=&cursor=&category=&low=), and the
// counts from GET /products/counts. Filters the server doesn't have ("In stock"; Out and Below zero narrow its
// `low`) are applied to each page as it arrives; a sort other than by name needs every product, so it loads them all.
import { useEffect, useMemo, useRef, useState } from "react";
import { ActivityIndicator, Pressable, RefreshControl, ScrollView, View } from "react-native";
import { FlashList, type FlashListRef } from "@shopify/flash-list";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useCategories } from "@/src/components/category-picker";
import { MoreFooter, RowGap } from "@/src/components/paged-list";
import { FilterChip, OptionRow, Sheet } from "@/src/components/sheet";
import { Badge, BottomBar, Button, EmptyState, ErrorView, LoadingView, Screen, SearchBox, T, Thumb } from "@/src/components/ui";
import { formatCurrency, formatQty } from "@/src/format";
import { usePagedList } from "@/src/hooks/usePagedList";
import { useRefreshOnFocus } from "@/src/hooks/useRefreshOnFocus";
import { colors, radius, spacing } from "@/src/theme";
import type { Product } from "@/src/types";

type StockFilter = "" | "in" | "low" | "out" | "below";
type Sort = "name" | "price-up" | "price-down" | "stock-up" | "new";
type Listed = Product & { thumb_path?: string | null }; // the small picture, when the server names one
type Counts = { all: number; low: number; out: number; below: number };

const KEY = ["products"] as const;
const PAGE = 50; // by name, as the list scrolls
const SEARCH = 100; // the best matches of a search (one page)
const ALL_PAGE = 200; // the server's largest page, while loading every product for a sort
const FILL = 20; // with a filter applied on the phone, load on until at least this many rows show

const matches: Record<StockFilter, (p: Product) => boolean> = {
  "": () => true,
  in: (p) => !p.low_stock,
  low: (p) => p.low_stock && !p.out_of_stock,
  out: (p) => p.out_of_stock,
  below: (p) => p.stock < 0,
};
const SORTS_BY: Record<Sort, ((a: Product, b: Product) => number) | null> = {
  name: null, // as the server sends them: by name, or a search's best matches first
  "price-up": (a, b) => a.selling_price - b.selling_price,
  "price-down": (a, b) => b.selling_price - a.selling_price,
  "stock-up": (a, b) => a.stock - b.stock,
  new: (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
};

export default function Products() {
  const { t } = useTranslation();
  const router = useRouter();
  const { can } = useAuth();
  const { stock: asked, at } = useLocalSearchParams<{ stock?: string; at?: string }>(); // at: each tap on Home sets it again
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [stock, setStock] = useState<StockFilter>("");
  const [openedAt, setOpenedAt] = useState<string | undefined>(undefined); // the Home tap already applied
  if ((asked === "below" || asked === "low" || asked === "out") && at !== openedAt) {
    setOpenedAt(at);
    setStock(asked);
  }
  const [sort, setSort] = useState<Sort>("name");
  const [sheet, setSheet] = useState<"" | "category" | "stock" | "sort">("");
  const cats = useCategories();
  const [typed, setTyped] = useState(""); // what was typed, a moment after typing stops
  useEffect(() => {
    const id = setTimeout(() => setTyped(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);
  const term = search.trim() ? typed : "";

  const needAll = !term && sort !== "name";
  const q = usePagedList<Listed>(KEY, "/products",
    { search: term, category, low: stock === "low" || stock === "out" || stock === "below" }, // out and below are within low
    { limit: term ? SEARCH : needAll ? ALL_PAGE : PAGE });
  const counted = useQuery<Counts>({ queryKey: ["products", "counts"], queryFn: () => apiGet("/products/counts"), retry: false });
  const inCategory = useQuery<Counts>({
    queryKey: ["products", "counts", category], queryFn: () => apiGet(`/products/counts?category_id=${encodeURIComponent(category)}`),
    enabled: !!category, retry: false,
  });
  useRefreshOnFocus(q.refreshIfStale);
  const list = useRef<FlashListRef<Listed>>(null);
  useEffect(() => list.current?.scrollToOffset({ offset: 0, animated: false }), [term, category, stock, sort]); // new filters start at the top
  useRefreshOnFocus(counted.refetch);

  const shown = useMemo(() => {
    // The category and stock filters are checked here too: a server without pages sends every product.
    const list = q.items.filter((p) => (!category || p.category_id === category) && matches[stock](p));
    const by = SORTS_BY[sort];
    return by ? [...list].sort(by) : list;
  }, [q.items, category, stock, sort]);

  // Load on by itself: every page for a sort, or, while a filter on the phone hides most rows, until enough show
  // (scrolling never reaches a next page when the last one added nothing to the screen).
  const pages = q.data?.pages;
  const lastAdded = pages?.length
    ? pages[pages.length - 1].items.filter((p) => (!category || p.category_id === category) && matches[stock](p)).length : 0;
  const onPhone = stock === "in" || stock === "out" || stock === "below";
  const { more } = q;
  useEffect(() => {
    if (needAll || (onPhone && (shown.length < FILL || lastAdded === 0))) more();
  }, [needAll, onPhone, shown.length, lastAdded, more]);

  const local = (f: StockFilter) => q.items.filter(matches[f]).length; // until the server's counts arrive
  const counts = {
    all: counted.data?.all ?? (q.hasNextPage || category || stock || term ? undefined : q.items.length),
    low: counted.data?.low ?? local("low"),
    out: counted.data?.out ?? local("out"),
    below: counted.data?.below ?? local("below"),
  };
  // How many products match the filters: a search's matches, or the server's counts for the category and stock filter.
  const scope = category ? inCategory.data : counted.data;
  const matching = term || !scope ? (q.hasNextPage ? undefined : shown.length)
    : stock === "" ? scope.all : stock === "in" ? scope.all - scope.low - scope.out : scope[stock];

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
  const hasAny = (counted.data?.all ?? q.items.length) > 0;

  return (
    <Screen edgeBottom={false}>
      <View style={{ paddingHorizontal: spacing.lg, paddingTop: spacing.md, paddingBottom: spacing.md, gap: spacing.md, backgroundColor: colors.surface,
        borderBottomWidth: 1, borderBottomColor: colors.line }}>
        <T v="title">{t("Products")}</T>
        <SearchBox value={search} onChangeText={setSearch} placeholder={t("Search name or code")} loading={!!term && q.isFetching && !q.isFetchingNextPage} testID="products-search" />
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: spacing.sm }}>
          <FilterChip label={t("Category")} value={catName} onPress={() => setSheet("category")} testID="products-filter-category" />
          <FilterChip label={t("Stock")} value={stock ? STOCK.find((x) => x.v === stock)!.label : ""}
            onPress={() => setSheet("stock")} testID="products-filter-stock" />
          <FilterChip label={t("Sort")} value={sort !== "name" ? SORTS.find((x) => x.v === sort)!.label : ""}
            onPress={() => setSheet("sort")} testID="products-filter-sort" />
        </ScrollView>
      </View>

      {q.isLoading ? <LoadingView /> : q.isError && !q.items.length ? <ErrorView message={q.error.message} onRetry={q.refetch} /> : (
        <FlashList
          ref={list}
          data={shown}
          keyExtractor={(p) => p.id}
          contentContainerStyle={{ padding: spacing.lg }}
          ItemSeparatorComponent={RowGap}
          onEndReached={q.more}
          onEndReachedThreshold={0.5}
          refreshControl={<RefreshControl refreshing={q.isRefetching && !q.isFetchingNextPage}
            onRefresh={() => { counted.refetch(); q.refresh(); }} tintColor={colors.green} />}
          ListHeaderComponent={hasAny ? (
            <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm, minHeight: 32,
              marginBottom: spacing.sm }}>
              <T v="label" style={{ flexShrink: 1 }} testID="products-count">
                {filtered
                  ? (matching === undefined ? "" : counts.all === undefined ? t("{{n}} products", { n: matching })
                    : t("{{n}} of {{all}} products", { n: matching, all: counts.all }))
                  : counts.all === undefined ? ""
                    : t("{{all}} products · {{low}} low · {{out}} out of stock", { all: counts.all, low: counts.low, out: counts.out })}
              </T>
              {filtered && (
                <Pressable onPress={() => { setSearch(""); setCategory(""); setStock(""); }} hitSlop={8} testID="products-clear">
                  <T v="bold" color={colors.red}>{t("Clear all")}</T>
                </Pressable>
              )}
            </View>
          ) : null}
          ListEmptyComponent={q.isPlaceholderData || q.hasNextPage ? <ActivityIndicator color={colors.green} style={{ paddingVertical: spacing.xl }} /> : (
            <EmptyState icon="sprout" title={hasAny ? t("Nothing matches") : t("No products yet")}
              subtitle={hasAny ? t("Try different filters, or tap Clear all.") : t("Add the plants, pots and other items you sell.")} />
          )}
          ListFooterComponent={<MoreFooter loading={q.isFetchingNextPage} failed={q.isFetchNextPageError} onRetry={() => q.fetchNextPage()} />}
          renderItem={({ item: p }) => (
            <Pressable testID={`product-${p.name}`} onPress={() => router.push({ pathname: "/products/[id]", params: { id: p.id } })}
              style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 76, borderRadius: radius.lg,
                backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, opacity: pressed ? 0.85 : 1 })}>
              <Thumb path={p.image_path} thumbPath={p.thumb_path} size={56} />
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
