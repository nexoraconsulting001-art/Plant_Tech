// What the app learns from its build and from the server before anything else.
// - BACKEND_URL: baked in at build time (eas.json / EAS environment variables; app.config.js and metro.config.js
//   refuse to build without it). Empty only in a broken local setup: the app then says so (AppGate).
// - GET /api/config (public, no login): the oldest app version the server still serves, feature flags, and
//   optionally the privacy policy / terms / store links. Read at start and again after half an hour.
// - Privacy policy and terms: the server's privacy_url / terms_url when it sends them, else the build's
//   EXPO_PUBLIC_PRIVACY_URL / EXPO_PUBLIC_TERMS_URL. No address is written in the code.
import { Platform } from "react-native";
import Constants from "expo-constants";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/src/api";

export const BACKEND_URL = (process.env.EXPO_PUBLIC_BACKEND_URL ?? "").trim();
export const APP_VERSION = Constants.expoConfig?.version ?? "";
const PRIVACY_URL = (process.env.EXPO_PUBLIC_PRIVACY_URL ?? "").trim();
const TERMS_URL = (process.env.EXPO_PUBLIC_TERMS_URL ?? "").trim();

export type AppConfig = {
  min_app_version?: string;
  features?: Record<string, unknown>;
  privacy_url?: string; // optional: lets the server change the links without a new build
  terms_url?: string;
  update_url?: string; // optional: where "Update" goes (e.g. a QA download page); default: the Play Store
};

export function useAppConfig() {
  return useQuery<AppConfig>({
    queryKey: ["app-config"],
    queryFn: () => apiGet("/config"),
    enabled: !!BACKEND_URL,
    staleTime: 30 * 60 * 1000,
    retry: 1,
  });
}

// "1.10.0" > "1.2.0"; missing parts count as 0 and anything after a number in a part ("0-beta") is ignored.
export function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((p) => parseInt(p, 10) || 0);
  const pb = b.split(".").map((p) => parseInt(p, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
}

// True only when the server names a minimum and this app is older: no answer (offline) never blocks billing.
export function isTooOld(config: AppConfig | undefined, version = APP_VERSION): boolean {
  const min = config?.min_app_version;
  return !!min && !!version && compareVersions(version, min) < 0;
}

const https = (url?: string) => (url && /^https:\/\//i.test(url) ? url : "");

export function updateUrl(config: AppConfig | undefined): string {
  const fromServer = https(config?.update_url);
  if (fromServer) return fromServer;
  const pkg = Constants.expoConfig?.android?.package;
  return Platform.OS === "android" && pkg ? `https://play.google.com/store/apps/details?id=${pkg}` : "";
}

export function useLegalLinks(): { privacy: string; terms: string } {
  const { data } = useAppConfig();
  return { privacy: https(data?.privacy_url) || https(PRIVACY_URL), terms: https(data?.terms_url) || https(TERMS_URL) };
}
