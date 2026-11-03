// metro.config.js
// Sentry's Expo config wraps Expo's default one: it gives every bundle a debug ID so the source maps the EAS
// build uploads (the @sentry/react-native/expo plugin in app.json) match the crashes Sentry receives.
const { getSentryExpoConfig } = require("@sentry/react-native/metro");
const path = require('path');
const { FileStore } = require('metro-cache');

// A release bundle (EAS build, `eas update`, `expo export`) has the server address baked in: refuse to make one
// without it rather than ship an app that cannot reach any server. See app.config.js for where it is set.
if (process.env.NODE_ENV === "production" && !(process.env.EXPO_PUBLIC_BACKEND_URL || "").trim()) {
  throw new Error(
    "EXPO_PUBLIC_BACKEND_URL is not set. Release bundles need the server address: eas.json for development and " +
    "staging, the EAS \"production\" environment for production (eas update --environment production).",
  );
}

const config = getSentryExpoConfig(__dirname);

// Use a stable on-disk store (shared across web/android)
const root = process.env.METRO_CACHE_ROOT || path.join(__dirname, '.metro-cache');
config.cacheStores = [
  new FileStore({ root: path.join(root, 'cache') }),
];


// // Exclude unnecessary directories from file watching
// config.watchFolders = [__dirname];
// config.resolver.blacklistRE = /(.*)\/(__tests__|android|ios|build|dist|.git|node_modules\/.*\/android|node_modules\/.*\/ios|node_modules\/.*\/windows|node_modules\/.*\/macos)(\/.*)?$/;

// // Alternative: use a more aggressive exclusion pattern
// config.resolver.blacklistRE = /node_modules\/.*\/(android|ios|windows|macos|__tests__|\.git|.*\.android\.js|.*\.ios\.js)$/;

// Reduce the number of workers to decrease resource usage
config.maxWorkers = 2;

module.exports = config;
