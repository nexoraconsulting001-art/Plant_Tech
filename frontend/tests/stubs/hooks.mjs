// Module hooks for the Node tests that load app modules with native imports (cart-sync.ts, api.ts):
// "@/src/..." resolves to frontend/src/<name>.ts, and the React Native, React Query, i18next and phone-storage
// imports get the small stand-ins in app-modules.mjs. Registered by the test file (module.register).
const SRC = new URL("../../src/", import.meta.url);
const STUB = new URL("./app-modules.mjs", import.meta.url).href;
const STUBBED = new Set(["react-native", "@tanstack/react-query", "i18next", "@/src/utils/storage"]);

export async function resolve(specifier, context, next) {
  if (STUBBED.has(specifier)) return { url: STUB, shortCircuit: true };
  if (specifier.startsWith("@/src/")) return { url: new URL(`${specifier.slice(6)}.ts`, SRC).href, shortCircuit: true };
  return next(specifier, context);
}
