// The server's paged lists (GET /products and /customers with `limit`): {items, next_cursor, total?}.
// next_cursor goes back as `cursor` for the next page (null on the last one); `total` (how many match) comes with
// the first page only. A server without pages answers the whole array, which is taken as one last page.
// No imports here, so scripts/test-paging.mjs can run it under plain Node.

export type Page<T> = { items: T[]; next_cursor: string | null; total?: number };
export type Params = Record<string, string | number | boolean | null | undefined>;

// "?a=1&b=x" from the params that are set (empty, false and null ones are left out).
export function queryString(params: Params): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export function asPage<T>(data: Page<T> | T[]): Page<T> {
  return Array.isArray(data) ? { items: data, next_cursor: null, total: data.length } : data;
}

// The cursor of the page after `last`, or undefined when it was the last page.
export function nextCursor(last: Page<unknown>): string | undefined {
  return last.next_cursor || undefined;
}

// The rows of the pages loaded so far, one per id (a row can move between two pages while they load).
export function mergePages<T extends { id: string }>(pages: Page<T>[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const page of pages) {
    for (const it of page.items) {
      if (!seen.has(it.id)) {
        seen.add(it.id);
        out.push(it);
      }
    }
  }
  return out;
}
