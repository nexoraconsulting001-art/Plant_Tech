// Stand-ins for the native and library modules cart-sync.ts and api.ts import (see hooks.mjs). One module for all
// of them: each import takes only the names it needs.
export const AppState = { addEventListener: () => ({ remove() {} }) };

export const onlineManager = { online: true, isOnline() { return this.online; }, subscribe: () => () => {} };

const fill = (text, params = {}) => text.replace(/\{\{(\w+)\}\}/g, (_, k) => String(params[k] ?? ""));
export default { t: fill };

export const saved = new Map(); // what the app kept on the phone
export const storage = {
  getItem: async (k, fallback) => (saved.has(k) ? saved.get(k) : fallback),
  setItem: async (k, v) => void saved.set(k, v),
  removeItem: async (k) => void saved.delete(k),
};
