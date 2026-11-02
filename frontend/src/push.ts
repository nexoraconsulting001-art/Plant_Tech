// Phone notifications for the shop owner: low stock and the daily sales summary (sent by the server, notify.py).
// After an owner logs in on a real phone, the app asks once for permission (Android 13+ asks the person),
// gets the phone's Expo push token and sends it to the server with the app's language. Logging out, or an
// employee logging in on the phone, removes it again. Nothing happens on the web, on a simulator, when the
// permission is refused, or in a build without Firebase (docs/PUSH-SETUP.md): the app works as before.
// Tapping a notification opens the matching screen (usePushTaps, in the tabs layout).
import { useEffect } from "react";
import { Platform } from "react-native";
import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { useRouter } from "expo-router";
import i18n from "i18next";

import { apiPost } from "@/src/api";
import { storage } from "@/src/utils/storage";

const NATIVE = Platform.OS !== "web";
const CHANNEL = "alerts"; // the server sends to this Android channel (push.py)
const TOKEN_KEY = "nurserypos_push_token_v1"; // the token this phone signed up, so logging out can remove it
const ASKED_KEY = "nurserypos_push_asked_v1"; // asked once by itself; after that only from More → Notifications

export type PushState = "on" | "off" | "unavailable";

if (NATIVE) {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
  });
}

// Whether this phone can get notifications at all, and whether they are allowed.
export async function pushState(): Promise<PushState> {
  if (!NATIVE || !Device.isDevice) return "unavailable";
  try {
    return (await Notifications.getPermissionsAsync()).status === "granted" ? "on" : "off";
  } catch {
    return "unavailable";
  }
}

// Sign this phone up for the owner's notifications. `ask`: show the permission prompt if it may be shown.
export async function registerPush(ask: boolean): Promise<PushState> {
  if (!NATIVE || !Device.isDevice) return "unavailable";
  try {
    if (Platform.OS === "android") {
      // Android 13+ shows the permission prompt only once the app has a channel
      await Notifications.setNotificationChannelAsync(CHANNEL, {
        name: i18n.t("Shop alerts"), importance: Notifications.AndroidImportance.HIGH,
      });
    }
    let { status, canAskAgain } = await Notifications.getPermissionsAsync();
    if (status !== "granted" && ask && canAskAgain) ({ status } = await Notifications.requestPermissionsAsync());
    if (status !== "granted") return "off";
    const projectId = Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
    const { data } = await Notifications.getExpoPushTokenAsync({ projectId });
    await apiPost("/push-tokens", { token: data, lang: i18n.language === "hi" ? "hi" : "en", platform: Platform.OS });
    await storage.setItem(TOKEN_KEY, data);
    return "on";
  } catch {
    return "unavailable"; // no Firebase in this build, or offline: try again next time
  }
}

// Stop this phone's notifications (logging out, or someone other than an owner logged in on it).
// The saved token is forgotten first, so a failing request (expired login) is never repeated.
export async function unregisterPush() {
  if (!NATIVE) return;
  const token = await storage.getItem<string | null>(TOKEN_KEY, null);
  if (!token) return;
  await storage.removeItem(TOKEN_KEY);
  apiPost("/push-tokens/remove", { token }).catch(() => {});
}

// After login, and when the language changes: owners sign the phone up, anyone else takes it off.
export async function syncPush(isOwner: boolean) {
  if (!NATIVE) return;
  if (!isOwner) return unregisterPush();
  const asked = await storage.getItem<boolean>(ASKED_KEY, false);
  if (!asked) await storage.setItem(ASKED_KEY, true);
  await registerPush(!asked);
}

type PushData = { type?: string; stock?: string; date?: string };
const handled = new Set<string>(); // each tap is acted on once, also when the tabs are shown again

// Tapping a notification: low stock → Products with that stock filter; the summary → that day's sales report.
export function usePushTaps(enabled: boolean) {
  const router = useRouter();
  useEffect(() => {
    if (!NATIVE || !enabled) return;
    const open = (r: Notifications.NotificationResponse | null) => {
      if (!r || handled.has(r.notification.request.identifier)) return;
      handled.add(r.notification.request.identifier);
      try {
        Notifications.clearLastNotificationResponse(); // not again on the next start
      } catch {}
      const d = (r.notification.request.content.data ?? {}) as PushData;
      if (d.type === "stock" && ["low", "out", "below"].includes(d.stock ?? "")) {
        router.push({ pathname: "/(tabs)/products", params: { stock: d.stock, at: String(Date.now()) } });
      } else if (d.type === "summary") {
        router.push({ pathname: "/more/reports", params: d.date ? { date: d.date } : {} });
      } else if (d.type === "test") {
        router.push("/more/notifications");
      }
    };
    try {
      open(Notifications.getLastNotificationResponse()); // the tap that opened the app
    } catch {}
    const sub = Notifications.addNotificationResponseReceivedListener(open);
    return () => sub.remove();
  }, [enabled, router]);
}
