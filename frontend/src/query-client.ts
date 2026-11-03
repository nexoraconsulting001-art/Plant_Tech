// One QueryClient for the whole app; the provider in app/_layout.tsx uses this instance.
// Retries only network/server errors: a 403 or 404 won't fix itself.
// Online state comes from NetInfo and "focused" from the app being in the foreground (React Query's React Native
// guide): while offline, queries wait instead of failing and run again when the phone is back online. Coming back to
// the app reloads what is on screen only if it is over a minute old (returning from WhatsApp after a bill reloads nothing).
import { AppState, Platform } from "react-native";
import NetInfo from "@react-native-community/netinfo";
import { focusManager, onlineManager, QueryClient } from "@tanstack/react-query";

const FOCUS_REFRESH_AFTER_MS = 60_000;

// isConnected is null until NetInfo knows: counted as online, so nothing waits at start.
onlineManager.setEventListener((setOnline) => NetInfo.addEventListener((s) => setOnline(s.isConnected !== false)));

// On the web React Query follows the page's own visibility.
if (Platform.OS !== "web") {
  focusManager.setEventListener((setFocused) => {
    const sub = AppState.addEventListener("change", (s) => setFocused(s === "active"));
    return () => sub.remove();
  });
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (count, error: any) => count < 2 && !(error?.status >= 400 && error?.status < 500),
      refetchOnWindowFocus: (q) => Date.now() - q.state.dataUpdatedAt > FOCUS_REFRESH_AFTER_MS,
    },
  },
});
