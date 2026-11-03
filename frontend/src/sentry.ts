// Crash and error reports (Sentry). Off unless the build has EXPO_PUBLIC_SENTRY_DSN (an EAS environment
// variable per environment); without it nothing is sent and every function here does nothing.
//
// No personal data leaves the phone: sendDefaultPii is off (no IP address), the user is only their id and
// shop id, and every report and breadcrumb is scrubbed first: phone numbers, emails and UPI IDs, GSTINs and
// login tokens are masked; fields named like a password, PIN, token, phone or address are dropped; query
// strings are cut off URLs (searches carry customer names). Native crashes are reported by the native SDK and
// carry only the stack, the device and these same (already scrubbed) breadcrumbs.
// Source maps: uploaded by the EAS build (the @sentry/react-native/expo plugin, SENTRY_AUTH_TOKEN /
// SENTRY_ORG / SENTRY_PROJECT as EAS variables); for `eas update`, run `npx sentry-expo-upload-sourcemaps dist`.
import * as Sentry from "@sentry/react-native";
import { isRunningInExpoGo } from "expo";
import Constants from "expo-constants";

const DSN = (process.env.EXPO_PUBLIC_SENTRY_DSN ?? "").trim();
export const sentryEnabled = DSN.length > 0;

const DROP_KEYS = new Set([
  "password", "current_password", "new_password", "pin", "otp", "token", "id_token", "access_token", "authorization",
  "cookie", "cookies", "phone", "confirm_phone", "email", "address", "gstin", "upi_id", "upi_ids", "image_base64",
]);
const MASKS: [RegExp, string][] = [
  [/Bearer\s+[\w.~+/=-]+/gi, "Bearer [token]"],
  [/eyJ[\w-]+\.[\w-]+\.[\w-]+/g, "[token]"], // a JWT on its own
  [/[\w.+-]+@[\w-]+(\.[\w-]+)*/g, "[email or UPI ID]"],
  [/\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b/g, "[GSTIN]"],
  [/(^|\D)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)/g, "$1[phone]"], // Indian mobile numbers, with or without +91
];

export function scrubText(text: string): string {
  return MASKS.reduce((s, [re, mask]) => s.replace(re, mask), text);
}

export function scrubUrl(url: string): string {
  return scrubText(url.split(/[?#]/)[0]);
}

// Masks every string inside a value and drops personal fields, without changing the shape around them.
export function scrub<T>(value: T, depth = 0): T {
  if (typeof value === "string") return scrubText(value) as T;
  if (!value || typeof value !== "object" || depth > 8) return value;
  if (Array.isArray(value)) return value.map((v) => scrub(v, depth + 1)) as T;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = DROP_KEYS.has(k.toLowerCase()) ? "[removed]" : k === "url" && typeof v === "string" ? scrubUrl(v) : scrub(v, depth + 1);
  }
  return out as T;
}

function scrubEvent<E extends Sentry.Event>(event: E): E {
  if (event.message) event.message = scrubText(event.message);
  for (const ex of event.exception?.values ?? []) if (ex.value) ex.value = scrubText(ex.value);
  if (event.request) {
    const { url, method } = event.request;
    event.request = { method, url: url ? scrubUrl(url) : undefined }; // no headers, cookies, body or query
  }
  if (event.extra) event.extra = scrub(event.extra);
  if (event.contexts) event.contexts = scrub(event.contexts);
  if (event.tags) event.tags = scrub(event.tags);
  if (event.breadcrumbs) event.breadcrumbs = event.breadcrumbs.map(scrubBreadcrumb);
  event.user = event.user?.id ? { id: event.user.id } : undefined;
  delete event.server_name;
  return event;
}

function scrubBreadcrumb(crumb: Sentry.Breadcrumb): Sentry.Breadcrumb {
  return { ...crumb, message: crumb.message ? scrubText(crumb.message) : crumb.message, data: crumb.data ? scrub(crumb.data) : crumb.data };
}

const navigation = Sentry.reactNavigationIntegration({ enableTimeToInitialDisplay: !isRunningInExpoGo() });

if (sentryEnabled) {
  Sentry.init({
    dsn: DSN,
    environment: (Constants.expoConfig?.extra?.appVariant as string | undefined) ?? (__DEV__ ? "development" : "production"),
    sendDefaultPii: false,
    attachScreenshot: false,
    attachViewHierarchy: false,
    tracesSampleRate: Number(process.env.EXPO_PUBLIC_SENTRY_TRACES_SAMPLE_RATE) || 0,
    enableNativeFramesTracking: !isRunningInExpoGo(),
    integrations: [navigation],
    beforeSend: scrubEvent,
    beforeSendTransaction: scrubEvent,
    beforeBreadcrumb: scrubBreadcrumb,
  });
}

// The root layout hands over expo-router's navigation container, so reports say which screen was open.
export function trackNavigation(ref: Parameters<typeof navigation.registerNavigationContainer>[0]) {
  if (sentryEnabled && ref) navigation.registerNavigationContainer(ref);
}

// Who was logged in: ids only (no name or phone), so a report can be matched to a shop that asks for help.
export function setReportUser(user: { id: string; role: string; business?: { id: string } | null } | null) {
  if (!sentryEnabled) return;
  Sentry.setUser(user ? { id: user.id } : null);
  Sentry.setTag("business_id", user?.business?.id ?? null);
  Sentry.setTag("role", user?.role ?? null);
}

// A crash caught by an error boundary (React does not pass it on, so Sentry would not see it otherwise).
export function reportError(error: unknown, componentStack?: string | null) {
  if (!sentryEnabled) return;
  Sentry.captureException(error, componentStack ? { contexts: { react: { componentStack } } } : undefined);
}

export const wrapRoot: typeof Sentry.wrap = sentryEnabled ? Sentry.wrap : (component) => component;
