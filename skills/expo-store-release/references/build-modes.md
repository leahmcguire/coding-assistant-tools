# Build modes: testing vs final

How "testing" and "final" map onto EAS build profiles, submit profiles, and each store's release channels.

## Contents
- The mapping
- Recommended eas.json shape
- Build numbers
- What happens after the build

## The mapping

| | iOS testing | iOS final | Android testing | Android final |
|---|---|---|---|---|
| Build profile | `production` (store-signed) | `production` | `production` (AAB) | `production` (AAB) |
| `distribution` | `store` | `store` | `store` | `store` |
| Submit profile | `testing`, or `production` if absent | `production` | `testing` (`track: internal`) | `production` (`track: production`) |
| Lands in | App Store Connect, then TestFlight | App Store Connect, then TestFlight | Play Console internal testing | Play Console production |
| Review | none for internal testers; Beta App Review for external | App Review, started by the user in App Store Connect | none for internal; review for closed/open | Play review |
| Who can install | internal: up to 100 App Store Connect team members; external: up to 10,000 by email or link | the public, after approval | up to 100 testers by email list | the public, after review and rollout |

**iOS testing and final use the same binary.** TestFlight requires a store-signed build (`distribution: "store"`). An `internal` (ad hoc) build can't be uploaded to App Store Connect. The difference between the modes is what happens after upload, and how strictly this skill checks review guidelines. `eas submit` never starts App Review. The user does that in the App Store Connect web interface.

**Android testing and final differ in the submit track.** A testing submission pointed at the `production` track publishes to the public once review passes, which is the most expensive mistake this skill guards against. Keep a separate submit profile for testing.

### Internal distribution builds (no store)

A profile with `distribution: "internal"` (commonly `preview`) produces an ad hoc IPA for registered iOS devices or an APK for Android. Nothing is uploaded to a store. Use it for quick device testing. Run with `--no-submit`. These builds can't be used for final submission, and an APK can't be uploaded to Play.

`developmentClient: true` builds include the dev menu and expect a Metro bundler. Never submit one to a store.

## Recommended eas.json shape

```json
{
  "cli": { "appVersionSource": "remote" },
  "build": {
    "production": {
      "distribution": "store",
      "autoIncrement": true,
      "env": { "EXPO_PUBLIC_API_URL": "https://api.example.com" }
    },
    "preview": { "distribution": "internal" }
  },
  "submit": {
    "testing": {
      "ios": { "ascAppId": "1234567890", "appleTeamId": "ABCDE12345" },
      "android": { "track": "internal", "releaseStatus": "completed" }
    },
    "production": {
      "ios": { "ascAppId": "1234567890", "appleTeamId": "ABCDE12345" },
      "android": { "track": "production", "releaseStatus": "draft" }
    }
  }
}
```

- `ascAppId` and `appleTeamId` aren't secrets. Pinning them stops `eas submit` from prompting.
- `releaseStatus: "draft"` for production means the release sits in Play Console until the user rolls it out, which gives a last human checkpoint. `completed` sends it to review immediately.
- `serviceAccountKeyPath` is optional. If absent, EAS uses the Google Service Account key stored through `eas credentials`, which keeps the JSON key out of the repo. Prefer that.
- Build profiles support `extends`. The preflight script resolves it.

When you add a `submit.testing` profile, copy the `ios` block from `production` rather than inventing IDs.

## Build numbers

Every upload needs a build number the store has never seen: `CFBundleVersion` on iOS, `versionCode` on Android. A build rejected during processing still consumes its number.

- `cli.appVersionSource: "remote"` together with `autoIncrement: true` on the store profile is the low-maintenance setup. EAS tracks and bumps the number.
- With `appVersionSource: "local"`, `ios.buildNumber` and `android.versionCode` in app.json must be bumped by hand or with `autoIncrement`.
- The user-facing `expo.version` (1.0.0) must go up for each new App Store *version*. Multiple builds of the same version are fine for TestFlight.

## What happens after the build

### iOS, testing
1. EAS uploads the build. App Store Connect shows it as *Processing* for 5–20 minutes.
2. Apple emails if processing fails (for example ITMS-90683, a missing purpose string). The email names the cause. That build number is burned.
3. Internal testers: TestFlight tab, then Internal Testing, add testers, assign the build. No review.
4. External testers: fill in Test Information (what to test, contact email, privacy policy URL), then submit for Beta App Review (usually about a day).
5. TestFlight builds expire after 90 days.

### iOS, final
1. The same processing step as above.
2. App Store tab, then create a version, attach the build, and complete the listing: screenshots, description, keywords, support URL, privacy policy URL, App Privacy answers, age rating, and review notes with a demo account if sign-in is required.
3. **Add for Review**, then **Submit to App Review**. Choose manual or automatic release.

### Android, testing
1. **The first-ever upload for an app must be done by hand in Play Console.** The Play API can't create an app's first release. Build with `--no-submit`, download the AAB from the EAS build page, and upload it to Testing, then Internal testing. After that, `eas submit` works.
2. Internal testing: add testers by email list and share the opt-in link. Available within minutes, no review.

### Android, final
1. The production track requires a completed store listing, content rating, target audience, Data safety form, app access instructions (credentials for reviewers), and ads declaration.
2. **Personal developer accounts created after 2023-11-13** must run a closed test with at least 12 testers for 14 consecutive days before production access is granted. Organization accounts are exempt.
3. With `releaseStatus: "draft"`, open Play Console, then Production, review the draft release, and start the rollout (staged rollout percentages are available).
