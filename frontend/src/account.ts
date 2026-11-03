// Deleting your own account: DELETE /api/account (server: backend/routers/account.py).
// The request carries a body (the typed phone number and the password or PIN), which apiDelete() does not send,
// and a wrong password answers 401, which through api.ts would log the person out; so it is sent here, with the
// same login token and the same translated error messages as api.ts.
// TODO(WP-12): once api.ts's apiDelete takes a body and an option to skip the 401 log-out, use it instead.
import i18n from "i18next";

import { API, ApiError } from "@/src/api";
import { storage } from "@/src/utils/storage";

const TOKEN_KEY = "nurserypos_token_v1"; // the same key as src/auth.tsx

// id_token: a Google-only owner (no password) confirms with a fresh Google sign-in instead.
export type DeleteAccountIn = { confirm_phone?: string; password?: string; id_token?: string; reason?: string };
// Owner: the shop closes now and is erased after the waiting period. Employee: their login is erased at once.
export type DeleteAccountOut = { status?: "scheduled" | "erased" | string; erase_after?: string; grace_days?: number };

export async function deleteAccount(body: DeleteAccountIn): Promise<DeleteAccountOut> {
  const token = await storage.secureGet<string | null>(TOKEN_KEY, null);
  let res: Response;
  try {
    res = await fetch(`${API}/account`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(i18n.t("No internet connection. Please check your connection and try again."), 0);
  }
  if (!res.ok) {
    let msg = i18n.t("Something went wrong. Please try again. (Error {{status}})", { status: res.status });
    let code = "";
    let params: Record<string, unknown> = {};
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") {
        code = data.code ?? "";
        params = data.params ?? {};
        msg = i18n.t(data.key ?? data.detail, params);
      }
    } catch {}
    throw new ApiError(msg, res.status, code, params);
  }
  return res.json().catch(() => ({}));
}
