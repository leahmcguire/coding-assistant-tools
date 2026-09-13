---
name: expo-store-release
description: Release gatekeeper for Expo / EAS mobile apps going to TestFlight, the App Store, or Google Play. Use it whenever the user wants to ship, release, submit, or upload the app, asks whether it is ready for TestFlight or App Review, prepares a version or production build, sets up a Play internal testing release, fixes a rejected or stuck upload (ITMS errors, Missing Compliance, icon or purpose-string rejections), asks you to run eas build or eas submit, or wants a security check before shipping, even if they never say audit. It checks Apple and Google Play requirements, reviews mobile security (secrets in EXPO_PUBLIC vars, keystores, cleartext traffic, token storage), distinguishes testing builds from final store submissions, presents findings as a plan, applies only approved fixes, and hands the user the build-and-submit command. It never runs builds or submissions itself. Not for OTA update debugging, CI setup, store listing copy, backend-only reviews, or apps that don't use Expo.
---

# Expo store release

You prepare a release. The user ships it.

Your job has four parts: audit the app for the target store and build type, present what you found as a plan, apply only the fixes the user approves, then give them the command that builds and submits. You do not run that command, and you don't run any EAS command that builds, submits, publishes an update, or changes credentials: `eas build`, `eas submit`, `eas update`, `eas credentials`, or `scripts/store-release.sh`.

Why this boundary is firm: a build spends paid EAS build minutes and uses up a build number that can never be reused. A submission uploads a binary to Apple or Google under the user's developer account, which can't be undone. `eas update` pushes code straight to real users' phones. Every one of those is the user's decision, and they need to be at the terminal to answer credential prompts anyway. The project's permission rules deny these commands, and `store-release.sh` refuses to start inside an agent session. Don't try to route around either (`npx eas-cli`, `bash -c`, unsetting env vars, copying the script). If the user asks you to run the build, explain the boundary in one sentence and give them the command.

Read-only EAS commands that need the network (`eas whoami`, `eas env:list`, `eas build:list`) are the user's to run too. If you need what they would show, ask the user to run them with `! <command>` so the output lands in the conversation.

## Workflow

### 1. Pin down the target

You need three things before auditing:

| Question | Values | If unstated |
|---|---|---|
| Platform | `ios`, `android`, `all` | Infer from the request ("TestFlight" means ios, "Play" means android); otherwise ask |
| Mode | `testing` or `final` | Ask. The two differ a lot; see below |
| App directory | the folder with `app.json` / `app.config.*` and `eas.json` | Find it; in a monorepo it's rarely the repo root |

- **testing**: the build goes to testers. That means TestFlight (internal or external), a Google Play internal or closed testing track, or an internal-distribution build installed directly on devices. Store-listing work doesn't apply yet, but anything that breaks the build, fails Apple's processing, or crashes on launch still blocks.
- **final**: the build is headed for App Store review or the Play production track. Everything in testing applies, plus review-guideline requirements and the store-console paperwork.

Read `references/build-modes.md` for how each mode maps to build profiles, submit profiles, and tracks. Read it before recommending any `eas.json` change.

### 2. Run the preflight script

```bash
python3 <skill-dir>/scripts/preflight.py --app-dir <app-dir> --platform <ios|android|all> --mode <testing|final>
```

Add `--build-profile` or `--submit-profile` if the project doesn't use the defaults: `production` for building, and for submitting, `production` in final mode or `testing` in testing mode. Add `--json` if you want to process the findings programmatically.

If the app uses `app.config.js` or `app.config.ts`, the script can't read the config statically and will say so. First resolve the config into a scratch file with `npx expo config --type public --json > <scratch>/expo-config.json`, run from the app directory. That command evaluates the config file and changes nothing. Then pass `--expo-config <scratch>/expo-config.json`.

The script is read-only and offline, and it runs in seconds. It reports `blocker`, `warning`, `manual` (something only the user can do in App Store Connect or Play Console), and `info` findings. Exit code 1 means it found blockers.

### 3. Audit past the script

The script is pattern matching, so treat its output as leads to confirm, not verdicts. Then read the references for the target and work through their judgment checks:

- `references/apple.md` for `ios` or `all`
- `references/google.md` for `android` or `all`
- `references/security.md` always

Two rules while you do this:

- **Confirm each finding before reporting it.** Open the file and line. A "missing account deletion" finding may be a false positive if deletion lives under a name the regex didn't catch. A "secret in bundle" hit might be a public OAuth client ID. Drop false positives, and mention briefly that you dropped them and why, so the user can trust the rest.
- **Stay read-only until the user approves the plan.** No edits yet, not even obvious ones.

### 4. Present the plan and ask

Use this structure. Keep each item to a few lines. The user decides from this, so the consequence of each item matters more than its mechanism.

```markdown
## Release audit: <platform>, <mode> (build profile `<name>`, submit profile `<name>`)

### Blockers
1. **<title>**: <what is wrong, file:line or config key>
   Why: <what happens if shipped as-is: build fails, Apple rejects processing with ITMS-xxxxx, Play rejects the upload, secret exposed to anyone who unzips the app>
   Fix: <the concrete change you propose>

### Warnings
(same shape)

### Security
(same shape; findings from references/security.md not already listed above)

### Only you can do these
- <App Store Connect / Play Console / credential rotation / account items, with where to click>

### Checked and fine
<one line listing what passed, so the user knows it was examined>
```

Then ask which fixes to apply: all, a subset (by number), or none. If you have a multi-select question tool, use it. Stop here until the user answers.

Some fixes aren't yours to make even with approval. Say so in the plan instead of offering them:

- **Exposed secrets need rotating, not just deleting.** Once a key has shipped in a build or sits in git history, removing it from the file doesn't make it safe. Propose removing it from the bundle, and tell the user they must rotate it with the provider.
- **Credentials and keystores**: don't move, regenerate, or delete signing keys, `.p8` or `.p12` files, or service-account JSON. Losing an Android upload keystore can lock an app out of updates. Recommend the change and let the user do it.
- **Store-console and account settings** (privacy labels, Data safety form, age rating, tester groups) live on Apple's and Google's websites. List them under "Only you can do these".

### 5. Apply the approved fixes

- Edit the source of truth: `app.json` or `app.config.*`, `eas.json`, and app source. Never edit the generated `ios/` or `android/` folders in a managed (CNG) project, because `expo prebuild` regenerates them on every EAS build and the change silently disappears. If those folders are committed (a bare workflow), editing them is correct. Check `.gitignore` to tell which kind of project it is.
- Keep changes minimal and match the surrounding style.
- Re-run `preflight.py` afterwards and confirm the blockers you fixed are gone.
- Run the project's own lint and tests if it has them (check CLAUDE.md, `package.json` scripts, or a Makefile). A release fix that breaks the test suite isn't done.

### 6. Summarize and hand off

End with this structure:

````markdown
## Fixed
- `<file>`: <change> (<finding it resolves>)

## Still open
- <anything not fixed, and why: declined, needs rotation, needs a console step>

## Before you build
- <ordered manual steps that must happen first, or "none">

## Build command
Run this yourself from the repo root. It re-runs the preflight, shows what it will do, and asks for confirmation:

```bash
<path-to-skill>/scripts/store-release.sh --app-dir <app-dir> --platform <p> --mode <m>
```

It runs: `eas build --platform <p> --profile <build> --auto-submit-with-profile <submit>`

## After the build
- <what happens next for this platform and mode, from references/build-modes.md>
````

For the script path, use the path the user will see from their repo root. If the skill is installed at `.claude/skills/expo-store-release`, give `.claude/skills/expo-store-release/scripts/store-release.sh`. Otherwise give the absolute path of this skill's directory. Include the raw `eas` command so the user knows exactly what the script does. If blockers remain open, say that the script's preflight will stop on them.

## Script reference

`scripts/store-release.sh` is written for a person at a terminal:

```
store-release.sh --platform ios|android|all --mode testing|final
                 [--app-dir DIR] [--build-profile NAME] [--submit-profile NAME]
                 [--no-submit] [--message TEXT] [--skip-preflight] [--dry-run]
```

`--no-submit` builds without uploading. Use it for internal-distribution builds such as a `preview` profile. `--dry-run` prints the command and exits. `--skip-preflight` exists for emergencies. Don't suggest it to get past blockers.
