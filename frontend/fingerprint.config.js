// The runtime fingerprint (app.json: runtimeVersion policy "fingerprint") decides which over-the-air updates fit a
// build. It leaves out the Firebase file (google-services.json, notifications): EAS gives that file to the build
// server as the file variable GOOGLE_SERVICES_JSON, but not to the machine that starts the build (GitHub's "App
// build" job), so with the file the two fingerprints differed and EAS stopped the build ("Runtime version calculated
// on local machine not equal to runtime version calculated during build", 9 Nov 2026). Updates are JavaScript only
// and never depend on that file, so leaving it out is safe. Check: `npx expo-updates fingerprint:generate
// --platform android`, with and without GOOGLE_SERVICES_JSON set, gives the same hash.
const path = require("path");

const ignorePaths = ["**/google-services*.json"]; // a copy next to app.config.js (local builds)
const firebase = (process.env.GOOGLE_SERVICES_JSON || "").trim();
if (firebase) {
  // where EAS put the file variable on the build server (outside the project, any name)
  ignorePaths.push(path.relative(__dirname, path.resolve(firebase)).split(path.sep).join("/"));
}

/** @type {import('expo/fingerprint').Config} */
module.exports = { ignorePaths };
