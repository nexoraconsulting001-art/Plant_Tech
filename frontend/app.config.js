// Dynamic config on top of app.json.
// APP_VARIANT (set by the EAS build profile) builds a separate app that installs next to the real one:
//   staging     → "Hara Hisaab QA", talks to the staging server ("staging" profile)
//   development → "Hara Hisaab Dev", a dev client that loads JS from `npx expo start` ("development" profile)
//
// Phone notifications on Android need Firebase (FCM): a google-services.json, used only when there is one
// (docs/PUSH-SETUP.md). First found wins: the EAS file variable GOOGLE_SERVICES_JSON, then
// google-services.<variant>.json, then google-services.json next to this file. Without it the app builds
// as before and simply gets no notifications. These files are kept out of git (.gitignore).
//
// The server address, EXPO_PUBLIC_BACKEND_URL, is baked into the app when it is built, so an EAS build without
// it stops here with a message instead of shipping an app that talks to nowhere (metro.config.js does the same
// for `eas update` / `expo export`). development and staging set it in eas.json; production and prod-apk take it
// from the EAS "production" environment (eas env:create), so no server address is written in this repo for them,
// and they refuse an http:// address or one that is an IP address (sslip.io / nip.io included): a released app
// cannot be moved to another server later. ALLOW_IP_BACKEND_URL=1 lifts that last check for a one-off test build.
const fs = require("fs");
const path = require("path");

const VARIANTS = {
  staging: { name: "Hara Hisaab QA", scheme: "nurserypos-qa", id: "com.planteck.nurserypos.qa" },
  development: { name: "Hara Hisaab Dev", scheme: "nurserypos-dev", id: "com.planteck.nurserypos.dev" },
};

const PRODUCTION_PROFILES = ["production", "prod-apk"];

function checkBackendUrl(profile) {
  const url = (process.env.EXPO_PUBLIC_BACKEND_URL || "").trim();
  const where = PRODUCTION_PROFILES.includes(profile)
    ? 'the EAS "production" environment: eas env:create --environment production --name EXPO_PUBLIC_BACKEND_URL --value https://api.<domain> --visibility plaintext'
    : `"env" of the "${profile}" profile in eas.json`;
  if (!url) throw new Error(`EXPO_PUBLIC_BACKEND_URL is not set for the "${profile}" build. Set it in ${where}.`);
  let host;
  try {
    const u = new URL(url);
    if (u.protocol !== "https:") throw new Error("not https");
    host = u.hostname;
  } catch {
    throw new Error(`EXPO_PUBLIC_BACKEND_URL must be an https:// address (got "${url}") for the "${profile}" build.`);
  }
  const ipLike = /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(":") || /(^|\.)(sslip|nip)\.io$/.test(host);
  if (PRODUCTION_PROFILES.includes(profile) && ipLike && process.env.ALLOW_IP_BACKEND_URL !== "1") {
    throw new Error(`EXPO_PUBLIC_BACKEND_URL for "${profile}" is an IP address (${host}). Use the domain (https://api.<domain>) in ${where}.`);
  }
}

function googleServicesFile(variantKey) {
  const candidates = [
    process.env.GOOGLE_SERVICES_JSON,
    variantKey && path.join(__dirname, `google-services.${variantKey}.json`),
    path.join(__dirname, "google-services.json"),
  ];
  return candidates.find((f) => f && fs.existsSync(f));
}

module.exports = ({ config }) => {
  if (process.env.EAS_BUILD_PROFILE) checkBackendUrl(process.env.EAS_BUILD_PROFILE);
  const variantKey = process.env.APP_VARIANT;
  const variant = VARIANTS[variantKey];
  // Which app this is (production / staging / development), for Sentry's environment (src/sentry.ts).
  config = { ...config, extra: { ...config.extra, appVariant: variant ? variantKey : "production" } };
  const firebase = googleServicesFile(variant ? variantKey : undefined);
  const android = { ...config.android, ...(variant ? { package: variant.id } : {}), ...(firebase ? { googleServicesFile: firebase } : {}) };
  if (!variant) return { ...config, android };
  return {
    ...config,
    name: variant.name,
    scheme: variant.scheme,
    android,
    ios: { ...config.ios, bundleIdentifier: variant.id },
  };
};
