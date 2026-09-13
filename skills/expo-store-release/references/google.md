# Google: Play Store requirements

Checks for `--platform android` and `all`. The preflight script covers what it can detect from config. This file covers what it can't, plus the reasons behind each check.

## Contents
- Upload requirements (block testing and final)
- Policy requirements (block final)
- Play Console items only the user can do
- Judgment checks to do by reading code

## Upload requirements (block testing and final)

| Check | Rule | Consequence |
|---|---|---|
| App bundle | Play accepts **AAB only** for new apps. `android.buildType: "apk"` on a store profile is wrong | Upload rejected |
| Package name | `android.package` is a valid Java package; can't start with `com.example`; **permanent once uploaded** | Upload rejected, or stuck with a bad ID forever |
| Target API level | New apps and updates must target a recent API level. Google raises the bar every **August 31** (API 35 from 2025-08-31; API 36 expected from 2026-08-31, so confirm at developer.android.com/google/play/requirements/target-sdk) | Upload rejected |
| Version code | Higher than every previous upload, across all tracks | Upload rejected |
| Signing | The upload key must match the one registered with Play App Signing | Upload rejected |
| First upload | The Play API can't create an app's first release, so the first AAB must be uploaded by hand in Play Console | `eas submit` fails for a brand-new app |

### Target SDK in Expo

Each Expo SDK sets a default `targetSdkVersion`: SDK 52 and 53 target 35, SDK 54 targets 36. An `expo-build-properties` plugin entry can override it, including downward, which is the usual way an app ends up below the requirement. If the app is below the requirement, the fix is upgrading the Expo SDK, which is a big change. Flag it and let the user plan it rather than bumping `targetSdkVersion` alone, because native modules may not support the newer API level.

### Signing

EAS-managed credentials (`eas credentials`) are the default and the safest choice. If a local keystore (`*.jks` or `*.keystore`) exists:
- It must never be committed. Even a gitignored copy sitting inside the repo is one `git add -f` away from exposure.
- **Losing the upload key** means a Play support request to reset it, and days without releases. The user should keep a backup outside the repo, in a password manager or secure storage.
- Never delete, move, or regenerate a keystore yourself.

## Policy requirements (block final)

| Policy | Requirement | How to check |
|---|---|---|
| Restricted permissions | SMS and Call Log, All files access (`MANAGE_EXTERNAL_STORAGE`), `QUERY_ALL_PACKAGES`, background location, `REQUEST_INSTALL_PACKAGES`, exact alarms, full-screen intent, and accessibility services each need a **Permissions Declaration** in Play Console, and many get rejected unless core to the app | Preflight lists them from `android.permissions` and a generated manifest if present |
| Photo and video permissions | `READ_MEDIA_IMAGES` or `READ_MEDIA_VIDEO` are allowed only when broad media access is core. Otherwise use the system photo picker (`expo-image-picker` uses it without the permission) | Check whether `expo-media-library` is really needed |
| Unused permissions | Expo merges every installed module's permissions into the manifest. Remove unused ones with `android.blockedPermissions` so the Data safety form stays honest and reviews stay simple | Compare module list with actual feature use |
| Data safety form | Must accurately declare collection and sharing by the app **and its SDKs** (analytics, crash reporting, auth providers) | Inventory SDKs in package.json for the user |
| Account deletion | Apps with account creation must offer deletion **in the app and** through a **web URL** listed in the Data safety form | In-app: find the settings screen. Web: ask the user for the URL |
| Privacy policy | Required for all apps; URL in Play Console and accessible in the app | Search source for a privacy link |
| Families / target audience | If the target audience includes children, the Families policy applies (ad SDK limits, no precise location) | Ask the user the intended age range |
| Foreground services | Each `FOREGROUND_SERVICE_*` type needs a declaration and justification (API 34+) | Check merged permissions |
| Credentials for review | If the app needs sign-in, provide working test credentials under App access | Only the user can supply them |

## Play Console items only the user can do

**Testing (internal track):**
- The app must exist in Play Console, and the **first AAB is uploaded by hand**
- Create an internal testing release and a tester email list; share the opt-in link
- A Google Service Account with the Play Android Developer API enabled, and its key given to EAS (`eas credentials`), is needed for `eas submit`

**Final (production track):**
- Store listing: app name (30 characters), short description (80), full description, 512×512 icon, 1024×500 feature graphic, at least 2 phone screenshots
- Content rating questionnaire, target audience and content, ads declaration
- Data safety form, privacy policy URL, account-deletion web URL
- App access: reviewer credentials
- **Personal developer accounts created after 2023-11-13**: a closed test with at least 12 opted-in testers for 14 consecutive days is required before applying for production access
- Countries or regions and pricing; managed publishing if the user wants to control launch timing

## Judgment checks to do by reading code

- **Back button and gestures**: Android back must not exit mid-flow or trap the user. Check custom `BackHandler` usage.
- **Edge-to-edge**: with `edgeToEdgeEnabled` (default on newer SDKs), content must respect insets. Look for screens not using safe-area handling.
- **Google sign-in client IDs**: an Android OAuth client must be registered with the **Play App Signing** SHA-1 (from Play Console), not only the upload key SHA-1. Otherwise sign-in works in internal builds and fails in Play-installed builds. Flag it if Google sign-in is present; only the user can verify it in Google Cloud Console.
- **Notification permission**: Android 13+ requires runtime `POST_NOTIFICATIONS`. Check the app requests it in context and works when it's denied.
- **Platform-specific references**: no "App Store" or iOS-only wording shown on Android.
