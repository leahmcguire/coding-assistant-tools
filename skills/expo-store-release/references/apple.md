# Apple: App Store and TestFlight requirements

Checks for `--platform ios` and `all`. The preflight script covers what it can detect from config. This file covers what it can't, plus the reasons behind each check, so you can explain consequences to the user.

## Contents
- Failures during Apple's processing (block testing and final)
- Review guidelines (block final)
- App Store Connect items only the user can do
- Judgment checks to do by reading code

## Failures during Apple's processing (block testing and final)

These pass `eas build` and then fail after upload, which is the most frustrating kind of failure: the build minutes and the build number are gone.

| Check | Rule | What Apple does |
|---|---|---|
| Purpose strings | Every privacy-sensitive API **linked into the binary** needs its `NS…UsageDescription` key, even if the app never calls it | Rejects processing with **ITMS-90683: Missing purpose string** |
| App icon | 1024×1024 PNG, **no alpha channel** (an RGBA PNG that is visually opaque still fails) | ITMS-90717: Invalid App Store icon |
| Bundle identifier | Matches the App Store Connect record, reverse-DNS | Upload rejected |
| Build number | Never used before for this version | Upload rejected as a duplicate |
| Version | `expo.version` is at most three dot-separated integers | Upload rejected |
| Privacy manifest | Required-reason APIs (UserDefaults, file timestamps, boot time, disk space) declared | ITMS-91053 warning email; can become blocking |

### Purpose strings: where they come from in Expo

- Put them in `ios.infoPlist` in app.json, **or** pass them as config-plugin options (for example `["expo-image-picker", { "photosPermission": "…", "cameraPermission": "…" }]`).
- A plugin with no options writes a generic default such as "Allow $(PRODUCT_NAME) to access your camera". That passes processing but invites a guideline 5.1.1 rejection at App Review, because Apple wants the string to say *why* the app needs access. Write a specific string for final.
- **Some plugins run even when they aren't listed.** Expo prebuild auto-applies the config plugins of `expo-av`, `expo-calendar`, `expo-camera`, `expo-contacts`, `expo-image-picker`, `expo-local-authentication`, `expo-location`, `expo-media-library` and `expo-sensors` when those packages are installed. Their keys aren't missing, just generic. Don't report them as processing blockers. If a generated `ios/<App>/Info.plist` exists, check it: it shows what the last prebuild wrote. Modules without a plugin (`expo-image`), or with a plugin Expo doesn't auto-apply (`expo-speech-recognition`, `expo-audio`, `expo-tracking-transparency`), do need the key set explicitly.
- Modules can link APIs the app doesn't use. `expo-image` references the photo library to support `ph://` URLs. `expo-image-picker` links camera, photo library, and microphone APIs. When the app doesn't use a capability, the key is still required. Write an honest string ("…only if you choose to take a photo of your meal").
- Never edit a generated `ios/<App>/Info.plist` in a managed project. EAS regenerates it.

### Export compliance

`ios.infoPlist.ITSAppUsesNonExemptEncryption: false` answers the encryption question automatically. Without it, every build waits at "Missing Compliance" in TestFlight until someone answers by hand. `false` is correct when the app only uses HTTPS or TLS, OS-provided crypto, and hashing (`expo-crypto` digests). Ask the user before setting it if the app implements its own encryption (end-to-end messaging, custom ciphers).

### Privacy manifest

Expo SDK 50+ merges the privacy manifests that modules ship, so most apps need nothing extra. If the app or a non-Expo native dependency uses required-reason APIs, declare them under `ios.privacyManifests` in app.json. After upload, Apple emails ITMS-91053 naming any missing categories. Ask the user whether they've seen that email for an earlier build.

## Review guidelines (block final)

| Guideline | Requirement | How to check |
|---|---|---|
| 4.8 Sign in with Apple | If the app offers any third-party or social login (Google, Facebook), it must also offer Sign in with Apple, or another login meeting 4.8's privacy criteria | Find the sign-in screen; confirm an Apple button renders on iOS. `expo-apple-authentication` must be listed in `plugins` or `ios.usesAppleSignIn: true` must be set, or the entitlement is missing and sign-in fails at runtime |
| 5.1.1(v) Account deletion | Apps that support account creation must let users start deletion **inside the app** | Find the settings or profile screen. A "contact us to delete" email link isn't enough |
| 5.1.1(i) Privacy policy | A privacy policy URL in App Store Connect **and** reachable in the app | Search source for a privacy link |
| 2.1 App completeness | Reviewers must be able to use the app. Login walls need demo credentials in the review notes; no placeholder content, broken links, or "coming soon" screens | Look for TODO or lorem text in user-visible strings and dead-end routes |
| 2.3 Accurate metadata | Screenshots show the real app; no references to other platforms ("Android", "Play Store") in the UI or metadata | `grep -ri "android\|play store"` in user-visible strings; platform-gated code is fine |
| 3.1.1 In-app purchase | Digital goods or subscriptions must use Apple IAP; no links to external payment for digital content | Look for Stripe checkout or payment web links offering digital features |
| 4.2 Minimum functionality | Not just a wrapped website | A WebView-only app is at risk |
| 5.1.2 Data use | App Tracking Transparency prompt before tracking; `NSUserTrackingUsageDescription` if `expo-tracking-transparency` is used | Look for analytics or ad SDKs that fingerprint or share IDFA |
| 4.5.4 Push notifications | Must not be required for the app to function; no marketing pushes without opt-in | Find the notification permission request and check the app works when it's denied |
| iPad | If `ios.supportsTablet: true`, the app must work on iPad and 13" iPad screenshots are required | Check the app.json flag |

## App Store Connect items only the user can do

List the relevant ones under "Only you can do these" in the plan.

**Testing (TestFlight):**
- An App Store Connect app record with this bundle ID must exist before the first `eas submit`
- Internal testers must be App Store Connect team members with a role
- External testing: Test Information (what to test, feedback email, privacy policy URL) and Beta App Review

**Final (App Store):**
- Screenshots: 6.9" iPhone set is required (Apple scales it for smaller sizes); 13" iPad if `supportsTablet`
- Description, keywords, support URL, privacy policy URL, copyright
- App Privacy questionnaire (the "nutrition label"). It must match what the app and its SDKs actually collect, including crash reporting and analytics
- Age rating questionnaire
- Review notes: demo account and password if sign-in is required, and how to reach gated features
- Pricing and availability; content rights declaration
- Choose manual or automatic release after approval

## Judgment checks to do by reading code

- **Sign-in works without the backend being "local"**: find how the API base URL is chosen. A build that falls back to `localhost` when an env var is missing ships a dead app.
- **Deep links and universal links**: if `ios.associatedDomains` is set, the domain must serve `apple-app-site-association`. Mention it; don't fetch it.
- **Permissions requested on launch**: Apple prefers requesting in context. Requesting camera or notifications at cold start without explanation is a common rejection reason.
- **Crash on first launch without network**: reviewers sometimes test on restricted networks. Look for unguarded fetches in root layouts.
- **Hidden debug UI**: dev menus, feature-flag panels, or "test mode" toggles reachable in production builds (not gated on `__DEV__`).
