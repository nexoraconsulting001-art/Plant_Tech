// Long lists (Products, Customers) load a page at a time from the server's paged lists (src/paging.ts) as they scroll.
import { useCallback, useEffect, useMemo } from "react";
import { keepPreviousData, useInfiniteQuery, useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";

import { apiGet } from "@/src/api";
import { asPage, mergePages, nextCursor, queryString, type Page, type Params } from "@/src/paging";

function trimToFirstPage(qc: QueryClient, queryKey: readonly unknown[]) {
  const state = qc.getQueryState(queryKey);
  qc.setQueryData<InfiniteData<Page<unknown>, string>>(queryKey,
    (d) => (d && d.pages.length > 1 ? { pages: d.pages.slice(0, 1), pageParams: d.pageParams.slice(0, 1) } : d),
    { updatedAt: state?.dataUpdatedAt }); // as old as it was: still reloaded when stale
}

export function usePagedList<T extends { id: string }>(key: readonly unknown[], path: string, params: Params,
  { limit, enabled = true }: { limit: number; enabled?: boolean }) {
  const qc = useQueryClient();
  const paramsKey = JSON.stringify(params);
  // Under the screen's key (so invalidating ["products"] reaches it), never the bare key: other screens keep arrays there.
  const queryKey = useMemo(() => [...key, "page", JSON.parse(paramsKey), limit] as const, [key, paramsKey, limit]);
  const q = useInfiniteQuery<Page<T>, Error, InfiniteData<Page<T>, string>, readonly unknown[], string>({
    queryKey,
    queryFn: async ({ pageParam }) => asPage<T>(await apiGet(`${path}${queryString({ ...params, limit, cursor: pageParam })}`)),
    initialPageParam: "",
    getNextPageParam: nextCursor,
    placeholderData: keepPreviousData, // a new search keeps the old rows on screen until its first page arrives
    enabled,
  });

  const items = useMemo(() => mergePages(q.data?.pages ?? []), [q.data]);
  const total = q.data?.pages[0]?.total;

  const { hasNextPage, isFetching, isFetchNextPageError, fetchNextPage } = q;
  // The next page, when there is one and nothing is loading (FlashList's onEndReached can fire repeatedly).
  const more = useCallback(() => {
    if (hasNextPage && !isFetching && !isFetchNextPageError) fetchNextPage();
  }, [hasNextPage, isFetching, isFetchNextPageError, fetchNextPage]);

  // A list left for another (a search typed, a filter changed) keeps only its first page: coming back to it, a
  // stale list reloads every page it holds, which after a long scroll would be dozens of requests.
  useEffect(() => () => trimToFirstPage(qc, queryKey), [qc, queryKey]);

  // Pull to refresh starts again from the first page instead of reloading every page scrolled through.
  const refresh = useCallback(async () => {
    trimToFirstPage(qc, queryKey);
    await qc.refetchQueries({ queryKey, exact: true });
  }, [qc, queryKey]);

  // Coming back to the screen (useRefreshOnFocus): reload the pages on screen, only if they are older than the
  // staleTime (src/query-client.ts), so hopping between a customer and the list doesn't reload it every time.
  const refreshIfStale = useCallback(() => qc.refetchQueries({ queryKey, exact: true, stale: true }), [qc, queryKey]);

  return { ...q, items, total, more, refresh, refreshIfStale };
}
