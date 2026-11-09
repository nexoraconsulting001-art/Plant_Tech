// API client. Every request carries the login token; a 401 logs the user out.
// Server messages are shown in the app's language: `key` + `params` when the server sends them
// (messages with numbers or names), else the English `detail` is itself the translation key.
// Every request has a time limit (AbortSignal.timeout): 10 s, 45 s for photo uploads, 75 s for a plant photo and 180 s
// for a supplier bill or a paper order (the server gives up on Claude after 50 s and 150 s, ai_client.TOTAL_TIMEOUT, and gives the
// scan back, so the app always gets the answer or that error). No reply in time is a TimeoutError (code "timeout"),
// no connection an ApiError with code "network"; both have status 0.
// A caller's own signal (React Query passes one) cancels a request that is no longer needed, e.g. an older search.
// A server that is overloaded answers 503 at once (code "busy", or "db_busy" when the database was too slow) with
// Retry-After: the request is sent again after that wait, at most BUSY_TRIES times in all, when it is safe to repeat
// (sync-policy.ts busyRetry; a bill keeps its Idempotency-Key). If it is still busy, the server's short message shows.
import i18n from "i18next";

import { BUSY_TRIES, busyRetry, busyWaitMs, retryAfterMs } from "@/src/sync-policy";

const BASE = process.env.EXPO_PUBLIC_BACKEND_URL;
export const API = `${BASE}/api`;

const TIMEOUT_MS = 10_000;
const UPLOAD_TIMEOUT_MS = 45_000; // a photo going up on a slow connection
const AI_TIMEOUT_MS = 75_000; // Claude identifying a plant (server limit 50 s)
const BILL_TIMEOUT_MS = 180_000; // Claude reading a supplier bill or a customer's paper order of several pages (server limit 150 s)
const timeoutFor = (path: string) =>
  path.startsWith("/ai/scan-invoice") || path.startsWith("/ai/scan-order") ? BILL_TIMEOUT_MS
    : path.startsWith("/ai/") ? AI_TIMEOUT_MS
      : path.startsWith("/upload") ? UPLOAD_TIMEOUT_MS
        : TIMEOUT_MS;

let token: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setApiToken(t: string | null) {
  token = t;
}

// The saved login being read back as the app loads (auth.tsx). Requests wait for it, so a screen that asks the server
// first (a web page reloaded on one of its screens) sends the token instead of being refused with "Please log in".
let loginRead: Promise<void> | null = null;
export function waitForLogin(reading: Promise<unknown>) {
  const gate = reading.then(() => {}, () => {});
  loginRead = gate;
  gate.then(() => {
    if (loginRead === gate) loginRead = null;
  });
}

export function setOnUnauthorized(fn: (() => void) | null) {
  onUnauthorized = fn;
}

export class ApiError extends Error {
  retryAfterMs: number | null = null; // the server's Retry-After, in ms
  // An earlier try of this request (request() sends a 503 again) got an answer that may mean it was done: a 503
  // other than "busy". The last try's "busy" then doesn't mean it was not done (a bill: order.ts).
  maybeDone = false;
  constructor(message: string, public status: number, public code = "", public params: Record<string, unknown> = {}) {
    super(message);
  }
}

// The server was too busy to answer (503), even after the tries request() makes by itself.
export const serverBusy = (e: unknown) => e instanceof ApiError && e.status === 503;

// The server didn't answer in time. The request may still have reached it (see the bill's Idempotency-Key in order.ts).
export class TimeoutError extends ApiError {
  constructor() {
    super(i18n.t("The network is slow. Please try again."), 0, "timeout");
  }
}

// No answer at all (offline or timed out): the request may or may not have reached the server.
export const noReply = (e: unknown) => e instanceof ApiError && e.status === 0;

export type RequestOptions = { signal?: AbortSignal; timeoutMs?: number; headers?: Record<string, string> };

const failed = (status: number) => i18n.t("Something went wrong. Please try again. (Error {{status}})", { status });

// Waits ms, or until the caller cancels (then fails with the cancel reason, as fetch would).
function pause(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const stop = () => {
      clearTimeout(timer);
      reject(signal!.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", stop);
      resolve();
    }, ms);
    signal?.addEventListener("abort", stop, { once: true });
  });
}

async function request(method: string, path: string, body?: unknown, opts: RequestOptions = {}) {
  const keyed = !!opts.headers?.["Idempotency-Key"];
  let maybeDone = false;
  for (let attempt = 0; ; attempt++) {
    try {
      return await requestOnce(method, path, body, opts);
    } catch (e) {
      const again = e instanceof ApiError && attempt + 1 < BUSY_TRIES && busyRetry(e.status, e.code, method, keyed);
      if (!again || opts.signal?.aborted) {
        if (maybeDone && e instanceof ApiError) e.maybeDone = true;
        throw e;
      }
      if (e.code !== "busy") maybeDone = true; // "db_busy" or the proxy's own 503: it may have done its work
      await pause(busyWaitMs(attempt, e.retryAfterMs), opts.signal);
    }
  }
}

async function requestOnce(method: string, path: string, body?: unknown, opts: RequestOptions = {}) {
  if (loginRead) await loginRead;
  const headers: Record<string, string> = { ...opts.headers };
  const sent = token; // a reply to a request sent before the saved login came back must not log out
  if (sent) headers.Authorization = `Bearer ${sent}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const timer = AbortSignal.timeout(opts.timeoutMs ?? timeoutFor(path));
  const signal = opts.signal ? AbortSignal.any([opts.signal, timer]) : timer;
  const lost = (e: unknown): never => {
    if (opts.signal?.aborted) throw e; // cancelled by the caller: nobody is waiting for an answer
    if (timer.aborted) throw new TimeoutError();
    throw new ApiError(i18n.t("No internet connection. Please check your connection and try again."), 0, "network");
  };
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal });
  } catch (e) {
    return lost(e);
  }
  if (!res.ok) {
    let msg = failed(res.status);
    let code = "";
    let params: Record<string, unknown> = {};
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") {
        code = data.code ?? "";
        params = data.params ?? {};
        msg = i18n.t(data.key ?? data.detail, params);
      } else if (Array.isArray(data?.detail)) {
        msg = i18n.t("Please check the details and try again.");
      }
    } catch {}
    if (res.status === 401 && sent && sent === token && onUnauthorized) onUnauthorized();
    const err = new ApiError(msg, res.status, code, params);
    err.retryAfterMs = retryAfterMs(res.headers.get("Retry-After"));
    throw err;
  }
  try {
    return await res.json();
  } catch (e) {
    if (timer.aborted || opts.signal?.aborted) return lost(e); // the answer stopped half-way
    throw e;
  }
}

export const apiGet = (path: string, opts?: RequestOptions) => request("GET", path, undefined, opts);
export const apiPost = (path: string, body: unknown = {}, opts?: RequestOptions) => request("POST", path, body, opts);
export const apiPut = (path: string, body: unknown = {}, opts?: RequestOptions) => request("PUT", path, body, opts);
export const apiPatch = (path: string, body: unknown = {}, opts?: RequestOptions) => request("PATCH", path, body, opts);
export const apiDelete = (path: string, opts?: RequestOptions) => request("DELETE", path, undefined, opts);

// Upload a photo (base64 from useImageCapture); returns its storage path.
// JSON, not multipart: Expo's fetch rejects React Native's {uri,name,type} FormData parts.
export async function uploadImage(base64: string): Promise<string> {
  const data = await apiPost("/upload-base64", { image_base64: base64 });
  return data.path;
}

export function fileUrl(path?: string | null): string | undefined {
  return path ? `${API}/files/${path}` : undefined;
}

// The login, for a request this file doesn't make itself (a file download, src/export-file.ts).
export const authHeaders = (): Record<string, string> => (token ? { Authorization: `Bearer ${token}` } : {});

export async function fetchText(path: string): Promise<string> {
  const timer = AbortSignal.timeout(TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { headers: authHeaders(), signal: timer });
    if (res.ok) return await res.text();
  } catch {
    if (timer.aborted) throw new TimeoutError();
    throw new ApiError(i18n.t("No internet connection. Please check your connection and try again."), 0, "network");
  }
  throw new ApiError(failed(res.status), res.status);
}
