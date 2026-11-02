# Phone notifications: one-time setup

The owner gets two kinds of notification on their phone: **low stock** (a product reaches its "Low-stock alert at"
level, runs out, or is sold below zero; once a day per product) and **today's sales summary** (once a day, 9 pm
by default). Employees never get them. The owner turns each on or off, and picks the summary time, in
**More → Notifications**. The same screen has **Send a test notification**.

The code is in place. Before notifications reach a phone, the steps below are needed. They are done once, by
the owner, in their own Google and Expo accounts. Keys stay in those accounts: nothing secret goes into git.

## What works without this setup
- **Browser and old phones:** the Home "Low stock" card, the More → Notifications screen and its settings work
  anywhere.
- **Phones without notifications:** a phone with the current QA app (1.1.0), or a new build without Firebase,
  simply gets no notifications. Nothing breaks.
- **New app build needed:** notifications need a new APK, because `expo-notifications` is native code. An
  over-the-air update is not enough.

## 1. Firebase (Android delivery, free)
1. Go to https://console.firebase.google.com and create a project, e.g. "Hara Hisaab". Google Analytics is not
   needed.
2. **Add app → Android** three times, one per app variant, with these package names:
   - `com.planteck.nurserypos` (the real app)
   - `com.planteck.nurserypos.qa` (Hara Hisaab QA)
   - `com.planteck.nurserypos.dev` (Hara Hisaab Dev)
3. **Download `google-services.json`** (Project settings → General → Your apps). One file covers all three apps
   once they are added.
4. **Give it to EAS** (preferred; nothing in git):
   `npx eas-cli@latest env:create --name GOOGLE_SERVICES_JSON --type file --value ./google-services.json --visibility sensitive --environment preview --environment production`
   from `frontend/`. `app.config.js` uses it when present.
   - For a local build, put the file at `frontend/google-services.json`, or per variant as
     `frontend/google-services.staging.json` / `google-services.development.json`.
   - These names are in `.gitignore`.
5. **Service account key for FCM V1:** Firebase → Project settings → Service accounts → **Generate new private
   key**. This one is secret: never commit it.
6. **Upload it to Expo:** `npx eas-cli@latest credentials` → Android → the app (do this for each of the three
   package names) → Google Service Account → **Push Notifications (FCM V1)** → upload the key file. Then delete
   the downloaded copy.

## 2. The server
In `deploy/.env` on the VM, staging first:
```
PUSH_ENABLED=1
```
- **Restart:** re-run the deploy so the container picks it up.
- **Check:** the log line "Phone notifications: on" shows it is running.
- **Without the flag:** the server stores phones and settings, but sends nothing.
- **`EXPO_ACCESS_TOKEN`:** only needed if "Enhanced push security" is switched on for the Expo project. Create
  the token at expo.dev → Account settings → Access tokens.

## 3. A new build, then a check
1. **Build:** a new QA APK (only after the owner's browser check and go-ahead, as always).
2. **Log in as the owner on the phone.** Android 13+ asks "Allow Hara Hisaab to send notifications?" once.
   Allow it.
3. **Test:** More → Notifications shows "This phone gets notifications". Tap **Send a test notification**. It
   should arrive within a few seconds, and tapping it opens the Notifications screen.
4. **Low stock:** set a product's "Low-stock alert at" to just below its stock, then bill one. A "Low stock"
   notification arrives. Tapping it opens Products with the Low filter. It comes once a day per product.
5. **Summary:** set the summary time to the next half hour, make a bill and wait. Tapping the summary opens
   Sales reports for that day.

## iOS (later)
iOS needs an Apple Developer account and an APNs key. `npx eas-cli@latest credentials` → iOS → Push
Notifications sets it up, and EAS can create the key itself. The app code is the same.

## How it works (for developers)
- **App** (`src/push.ts`):
  - After an owner logs in on a real phone, it asks for permission once, gets the Expo push token
    (projectId from `app.json`) and sends it with the app language to `POST /api/push-tokens`.
  - Logging out, or an employee logging in on that phone, removes it again (`POST /api/push-tokens/remove`).
- **Server:**
  - `push.py` sends through `https://exp.host/--/api/v2/push/send` in the background, so a sale never waits.
    It drops phones that Expo reports as `DeviceNotRegistered`.
  - `notify.py` decides what to send. It writes a marker in `push_marks` for each send, so restarts and
    several workers never send twice. Markers are removed after 45 days.
  - The daily summary is checked every minute by a loop started in `server.py`.
- **Language:** each phone gets the text in the app language it was signed up with (English or Hindi). The
  Hindi texts are in `notify.py`, not in `hi.json`.
