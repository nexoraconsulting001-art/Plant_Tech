// Reload when the user comes back to a screen (tabs stay mounted, so data would go stale).
// Pass the query itself (useRefreshOnFocus(q)) to reload only what is out of date: older than its staleTime, or
// marked by a change elsewhere (e.g. a bill, see afterBill in order.ts). Passing q.refetch reloads on every return.
// A reload already on its way is not started again.
import { useCallback, useEffect, useRef } from "react";
import { useFocusEffect } from "expo-router";

type Refetch = (opts?: { cancelRefetch?: boolean }) => unknown;

export function useRefreshOnFocus(query: Refetch | { refetch: Refetch; isStale: boolean }) {
  const first = useRef(true);
  const latest = useRef(query);
  useEffect(() => {
    latest.current = query;
  });
  useFocusEffect(
    useCallback(() => {
      if (first.current) {
        first.current = false;
        return;
      }
      const q = latest.current;
      if (typeof q === "function") q({ cancelRefetch: false });
      else if (q.isStale) q.refetch({ cancelRefetch: false });
    }, []),
  );
}
