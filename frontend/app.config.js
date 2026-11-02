// Dynamic config on top of app.json.
// APP_VARIANT (set by the EAS build profile) builds a separate app that installs next to the real one:
//   staging     → "Hara Hisaab QA", talks to the staging server ("staging" profile)
//   development → "Hara Hisaab Dev", a dev client that loads JS from `npx expo start` ("development" profile)
//
// Phone notifications on Android need Firebase (FCM): a google-services.json, used only when there is one
// (docs/PUSH-SETUP.md). First found wins: the EAS file variable GOOGLE_SERVICES_JSON, then
// google-services.<variant>.json, then google-services.json next to this file. Without it the app builds
// as before and simply gets no notifications. These files are kept out of git (.gitignore).
const fs = require("fs");
const path = require("path");

const VARIANTS = {
  staging: { name: "Hara Hisaab QA", scheme: "nurserypos-qa", id: "com.planteck.nurserypos.qa" },
  development: { name: "Hara Hisaab Dev", scheme: "nurserypos-dev", id: "com.planteck.nurserypos.dev" },
};

function googleServicesFile(variantKey) {
  const candidates = [
    process.env.GOOGLE_SERVICES_JSON,
    variantKey && path.join(__dirname, `google-services.${variantKey}.json`),
    path.join(__dirname, "google-services.json"),
  ];
  return candidates.find((f) => f && fs.existsSync(f));
}

module.exports = ({ config }) => {
  const variantKey = process.env.APP_VARIANT;
  const variant = VARIANTS[variantKey];
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
