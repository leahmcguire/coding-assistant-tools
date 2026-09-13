# Security review for a mobile release

Run this for every release, testing or final. A TestFlight or internal-track build is installable by other people and can be unzipped. Treat it as public.

## Contents
- The core fact: the bundle is public
- Checks the preflight script automates
- Checks to do by reading code
- Reporting security findings

## The core fact: the bundle is public

Anything compiled into the app can be extracted by anyone who installs it. Unzipping an IPA or AAB and running `strings` on the JS bundle takes minutes. That includes:

- every `EXPO_PUBLIC_*` variable, inlined at build time
- everything under `expo.extra` in app config, readable at runtime via `expo-constants`
- string literals anywhere in JS or TS source
- files bundled as assets

So the question for every credential is: **is it designed to be public?**

| Designed to be public (fine in the bundle) | Never in the bundle |
|---|---|
| Supabase URL and **anon** key (with RLS actually enforced) | Supabase **service_role** key |
| Firebase web config, Google OAuth **client IDs** | OAuth **client secrets** |
| Stripe **publishable** key (`pk_`) | Stripe **secret** key (`sk_`) |
| Sentry DSN, analytics write keys | AWS access keys, database URLs with passwords |
| Google Maps API key **restricted to the app's package or bundle ID** | Unrestricted Google API keys; LLM provider keys (OpenAI, Anthropic) |

A secret in the bundle has to be moved behind a backend endpoint **and rotated**. Removing it from the next build doesn't help: earlier builds and git history still contain it. Tell the user plainly that rotation is theirs to do.

## Checks the preflight script automates

Confirm each hit by reading the file before reporting it.

- Secret-looking **names** in `EXPO_PUBLIC_*` variables and `expo.extra` (`SECRET`, `PRIVATE`, `PASSWORD`, `SERVICE_ROLE`)
- Secret **value** patterns in config and source: AWS key IDs, `sk_live_` and `sk_test_`, PEM private keys, GitHub and Slack tokens, LLM API keys, and JWTs whose payload says `role: service_role`
- Sensitive **files** in the app directory: keystores, `.p8`, `.p12`, `.pem`, provisioning profiles, `credentials.json`, `.env`, and Google service-account JSON. Tracked by git is a blocker. Untracked but not ignored is a warning, because EAS uploads every file that `.gitignore` or `.easignore` doesn't exclude. Ignored is informational, as a reminder to keep a backup outside the repo.
- **Cleartext traffic**: `http://` URLs compiled in; iOS `NSAllowsArbitraryLoads`; Android `usesCleartextTraffic`
- **Local or dev endpoints** compiled into a store build (`localhost`, private IPs, `.local`, ngrok)
- **Token storage** in AsyncStorage (unencrypted) instead of `expo-secure-store`
- **Logging** of tokens, passwords, or authorization headers
- **WebView** with `originWhitelist={['*']}` or file-access flags
- **OTA updates** (`expo-updates`) without code signing

## Checks to do by reading code

Scope these to what the app actually does. Skip sections that don't apply and say you skipped them.

### Auth and session
- Where are access and refresh tokens stored? Expect `expo-secure-store` (Keychain or Keystore). AsyncStorage and MMKV without encryption are readable on rooted or jailbroken devices and in some backups.
- Does sign-out clear stored tokens and in-memory caches (React Query or SWR caches, context state)?
- OAuth flows: is PKCE used (the `expo-auth-session` default)? Are `state` and nonce values validated? Is the redirect URI the app's scheme and not a wildcard?
- Is authorization enforced server-side? Client-side route guards are UX, not security. If the backend is in the same repo, spot-check that endpoints the app calls check the caller's identity. Don't audit the whole backend; mention it if it looks thin.

### Deep links
- List every route reachable by `scheme://` link or universal/app link. Can a link trigger a state change (accept an invite, delete something, sign in with a token in the URL) without user confirmation?
- Are tokens passed in deep-link URLs? URLs end up in logs and in other apps' intent handlers.

### Network
- All API base URLs are `https://` in the store profile.
- Certificate pinning is optional for most apps. Only raise it for finance, health, or other high-value data.
- Error handling doesn't surface raw server errors, stack traces, or internal URLs to users.

### Data on device
- Sensitive data (health, location history, messages) cached unencrypted on disk?
- Screenshots or app switcher: for sensitive screens (payment, one-time codes), consider hiding content when the app goes to the background.
- Clipboard: one-time codes and secrets copied to the clipboard can be read by other apps. Check `expo-clipboard` usage.

### Production leftovers
- Debug screens, test accounts, hard-coded credentials, or feature flags that bypass auth, not gated on `__DEV__`
- Verbose `console.log` of API responses containing personal data (visible in device logs)
- `expo-dev-client` in dependencies is fine. It's only included when a profile sets `developmentClient: true`.

### Dependencies
- Offer to run `npm audit --omit=dev` (or the project's package manager equivalent). It contacts the npm registry, so ask before running it. Report only high and critical advisories in runtime dependencies, with the upgrade path.
- Flag abandoned native modules (no release in 2+ years) that handle auth, crypto, or storage.

### Permissions (privacy is part of security)
- Every requested permission should map to a feature. Unused permissions widen the attack surface and must be declared anyway (the Apple privacy label and Google's Data safety form).

## Reporting security findings

- Put confirmed security issues under **Security** in the plan, most severe first.
- For each item, state who could exploit it and how: "anyone who downloads the TestFlight build can extract the service-role key and read every user's data". The consequence is what lets the user prioritize.
- Never paste full secret values into the conversation. Show the first 4 characters and where the value lives.
- Rotation, provider-dashboard changes, and git-history rewrites go under "Only you can do these".
