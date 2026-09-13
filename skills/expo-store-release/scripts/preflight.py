#!/usr/bin/env python3
"""Read-only release preflight for Expo apps bound for the App Store or Google Play.

Reads app.json (or a resolved `npx expo config --type public --json` dump), eas.json,
package.json, app source and git state, then reports findings grouped by area. It never
modifies files, never runs EAS, and never touches the network.

Exit status: 0 = no blockers, 1 = blockers found, 2 = could not run.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BLOCKER = "blocker"
WARNING = "warning"
MANUAL = "manual"
INFO = "info"
SEVERITY_ORDER = (BLOCKER, WARNING, MANUAL, INFO)
AREAS = (("common", "Common"), ("apple", "Apple"), ("google", "Google"), ("security", "Security"))

# Google raises the minimum target API level every August 31. API 35 has been required since
# 2025-08-31; API 36 is expected from 2026-08-31. Confirm at
# https://developer.android.com/google/play/requirements/target-sdk and update both constants.
PLAY_MIN_TARGET_SDK = 35
PLAY_NEXT_TARGET_SDK = 36
# Default Android targetSdkVersion per Expo SDK major version.
EXPO_SDK_TARGET_SDK = {50: 34, 51: 34, 52: 35, 53: 35, 54: 36}

PRUNE_DIRS = {
    "node_modules",
    ".git",
    ".expo",
    "dist",
    "web-build",
    "coverage",
    "build",
    "ios",
    "android",
    "Pods",
    ".gradle",
    ".kotlin",
    ".next",
    ".turbo",
    "__mocks__",
    "__tests__",
}
SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
TEST_FILE_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
MAX_SCAN_BYTES = 400_000


class ConfigError(Exception):
    """The project can't be read well enough to audit."""


@dataclass
class Finding:
    id: str
    area: str
    severity: str
    title: str
    detail: str
    fix: str = ""
    where: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def add(self, **kwargs: str) -> None:
        self.findings.append(Finding(**kwargs))

    def has_blockers(self) -> bool:
        return any(f.severity == BLOCKER for f in self.findings)


@dataclass(frozen=True)
class PurposeString:
    key: str
    plugin_option: str | None  # config-plugin option that writes this key, if any


# iOS usage-description keys each module links APIs for. Apple's static scan rejects a
# binary that links the API without the key (ITMS-90683), whether or not the app calls it.
IOS_PURPOSE_STRINGS: dict[str, tuple[PurposeString, ...]] = {
    "expo-camera": (
        PurposeString("NSCameraUsageDescription", "cameraPermission"),
        PurposeString("NSMicrophoneUsageDescription", "microphonePermission"),
    ),
    "expo-image-picker": (
        PurposeString("NSPhotoLibraryUsageDescription", "photosPermission"),
        PurposeString("NSCameraUsageDescription", "cameraPermission"),
        PurposeString("NSMicrophoneUsageDescription", "microphonePermission"),
    ),
    "expo-media-library": (
        PurposeString("NSPhotoLibraryUsageDescription", "photosPermission"),
        PurposeString("NSPhotoLibraryAddUsageDescription", "savePhotosPermission"),
    ),
    # expo-image resolves ph:// asset URLs through PHPhotoLibrary and has no config plugin.
    "expo-image": (PurposeString("NSPhotoLibraryUsageDescription", None),),
    "expo-location": (
        PurposeString("NSLocationWhenInUseUsageDescription", "locationWhenInUsePermission"),
    ),
    "expo-contacts": (PurposeString("NSContactsUsageDescription", "contactsPermission"),),
    "expo-calendar": (
        PurposeString("NSCalendarsFullAccessUsageDescription", "calendarPermission"),
        PurposeString("NSRemindersFullAccessUsageDescription", "remindersPermission"),
    ),
    "expo-av": (PurposeString("NSMicrophoneUsageDescription", "microphonePermission"),),
    "expo-audio": (PurposeString("NSMicrophoneUsageDescription", "microphonePermission"),),
    "expo-speech-recognition": (
        PurposeString("NSMicrophoneUsageDescription", "microphonePermission"),
        PurposeString("NSSpeechRecognitionUsageDescription", "speechRecognitionPermission"),
    ),
    "expo-local-authentication": (PurposeString("NSFaceIDUsageDescription", "faceIDPermission"),),
    "expo-tracking-transparency": (
        PurposeString("NSUserTrackingUsageDescription", "userTrackingPermission"),
    ),
    "expo-sensors": (PurposeString("NSMotionUsageDescription", "motionPermission"),),
}
# Expo prebuild applies these modules' config plugins even when they aren't listed in `plugins`
# (@expo/prebuild-config withDefaultPlugins), so their permission keys get generic defaults
# rather than going missing.
AUTO_APPLIED_PLUGINS = frozenset(
    {
        "expo-av",
        "expo-calendar",
        "expo-camera",
        "expo-contacts",
        "expo-image-picker",
        "expo-local-authentication",
        "expo-location",
        "expo-media-library",
        "expo-sensors",
    }
)
GENERIC_PURPOSE_RE = re.compile(
    r"\$\(PRODUCT_NAME\)|^allow .{0,40} to (access|use) your [\w ]+\.?$", re.IGNORECASE
)

SOCIAL_LOGIN_DEPS = (
    "expo-auth-session",
    "@react-native-google-signin/google-signin",
    "react-native-fbsdk-next",
    "react-native-app-auth",
)
ACCOUNT_CREATION_RE = re.compile(
    r"signUp|sign[-_ ]up|createAccount|createUser|registerUser|signInWithOtp|signInWithIdToken",
    re.IGNORECASE,
)
ACCOUNT_DELETION_RE = re.compile(
    r"delete[-_ ]?(my[-_ ]?)?account|deleteAccount|deleteUser|delete[-_ ]user|close[-_ ]account",
    re.IGNORECASE,
)

RESTRICTED_ANDROID_PERMISSIONS = {
    "READ_SMS": "SMS and Call Log policy",
    "RECEIVE_SMS": "SMS and Call Log policy",
    "SEND_SMS": "SMS and Call Log policy",
    "RECEIVE_MMS": "SMS and Call Log policy",
    "READ_CALL_LOG": "SMS and Call Log policy",
    "WRITE_CALL_LOG": "SMS and Call Log policy",
    "PROCESS_OUTGOING_CALLS": "SMS and Call Log policy",
    "MANAGE_EXTERNAL_STORAGE": "All files access policy",
    "QUERY_ALL_PACKAGES": "Package visibility policy",
    "ACCESS_BACKGROUND_LOCATION": "Location permissions policy",
    "REQUEST_INSTALL_PACKAGES": "Request install packages policy",
    "READ_MEDIA_IMAGES": "Photo and video permissions policy",
    "READ_MEDIA_VIDEO": "Photo and video permissions policy",
    "USE_EXACT_ALARM": "Exact alarm policy",
    "SCHEDULE_EXACT_ALARM": "Exact alarm policy",
    "USE_FULL_SCREEN_INTENT": "Full-screen intent policy",
    "BIND_ACCESSIBILITY_SERVICE": "Accessibility API policy",
    "SYSTEM_ALERT_WINDOW": "Overlay permission scrutiny",
}
# Permissions modules merge into the Android manifest that belong in the Data safety form.
MODULE_ANDROID_PERMISSIONS = {
    "expo-media-library": ("READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO"),
    "expo-camera": ("CAMERA", "RECORD_AUDIO"),
    "expo-av": ("RECORD_AUDIO",),
    "expo-audio": ("RECORD_AUDIO",),
    "expo-speech-recognition": ("RECORD_AUDIO",),
    "expo-location": ("ACCESS_COARSE_LOCATION", "ACCESS_FINE_LOCATION"),
    "expo-contacts": ("READ_CONTACTS", "WRITE_CONTACTS"),
    "expo-calendar": ("READ_CALENDAR", "WRITE_CALENDAR"),
}

LOCAL_URL_RE = re.compile(
    r"^https?://(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|"
    r"[^/:]*\.local\b|[^/:]*ngrok)",
    re.IGNORECASE,
)
SECRET_NAME_RE = re.compile(
    r"SECRET|PRIVATE|PASSWORD|PASSWD|SERVICE_ROLE|ADMIN_KEY|ACCESS_KEY_ID", re.IGNORECASE
)
SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), BLOCKER),
    ("Stripe live secret key", re.compile(r"\bsk_live_[0-9A-Za-z]{10,}"), BLOCKER),
    ("Stripe test secret key", re.compile(r"\bsk_test_[0-9A-Za-z]{10,}"), WARNING),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), BLOCKER),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), BLOCKER),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), BLOCKER),
    ("LLM provider API key", re.compile(r"\bsk-(ant-|proj-)[A-Za-z0-9_-]{20,}"), BLOCKER),
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
HTTP_URL_RE = re.compile(r"""["'`](http://[^"'`\s]+)""")
ASYNC_STORAGE_TOKEN_RE = re.compile(
    r"AsyncStorage\.setItem\(\s*[\"'`][^\"'`]*(token|session|jwt|auth|password|secret)",
    re.IGNORECASE,
)
LOG_SECRET_RE = re.compile(
    r"console\.(log|info|debug|warn)\([^;\n]*\b(access_?token|refresh_?token|password|"
    r"authorization|id_?token|secret)\b",
    re.IGNORECASE,
)
WEBVIEW_WILDCARD_RE = re.compile(r"originWhitelist=\{\s*\[\s*[\"']\*[\"']\s*\]\s*\}")
WEBVIEW_FILE_ACCESS_RE = re.compile(
    r"(allowUniversalAccessFromFileURLs|allowFileAccessFromFileURLs)(=\{\s*true\s*\}|\s|/?>)"
)
ENV_USE_RE = re.compile(r"process\.env\.(EXPO_PUBLIC_[A-Z0-9_]+)")

SENSITIVE_FILES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.(jks|keystore)$"), "Android signing keystore"),
    (re.compile(r"\.p8$"), "Apple API or push key"),
    (re.compile(r"\.p12$"), "certificate bundle"),
    (re.compile(r"\.mobileprovision$"), "provisioning profile"),
    (re.compile(r"\.(pem|key)$"), "private key"),
    (re.compile(r"(^|/)credentials\.json$"), "EAS local credentials file"),
    (re.compile(r"(^|/)\.env(\.[\w-]+)?$"), "environment file"),
)
ENV_TEMPLATE_RE = re.compile(r"\.(example|sample|template|dist)$")


# ---------------------------------------------------------------------------------------------
# Loading


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_profile(profiles: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return the named eas.json profile with its `extends` chain merged in."""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = name
    while current is not None:
        if current in seen:
            raise ConfigError(f"eas.json profile `{name}` has an `extends` cycle")
        seen.add(current)
        profile = profiles.get(current)
        if not isinstance(profile, dict):
            if current == name:
                return None
            raise ConfigError(f"eas.json profile `{name}` extends missing profile `{current}`")
        chain.append(profile)
        parent = profile.get("extends")
        current = parent if isinstance(parent, str) else None
    resolved: dict[str, Any] = {}
    for profile in reversed(chain):
        resolved = deep_merge(resolved, profile)
    resolved.pop("extends", None)
    return resolved


def load_expo_config(app_dir: Path, expo_config: Path | None) -> tuple[dict[str, Any], str]:
    if expo_config is not None:
        data = load_json(expo_config)
        expo = data.get("expo", data) if isinstance(data, dict) else None
        if not isinstance(expo, dict):
            raise ConfigError(f"{expo_config} does not contain an Expo config object")
        return expo, str(expo_config)
    dynamic = [
        p.name for p in app_dir.glob("app.config.*") if p.suffix in {".js", ".ts", ".cjs", ".mjs"}
    ]
    if dynamic:
        raise ConfigError(
            f"{dynamic[0]} is evaluated at build time and can't be read statically. From the app "
            "directory run `npx expo config --type public --json > <scratch>/expo-config.json`, "
            "then pass --expo-config <scratch>/expo-config.json"
        )
    app_json = app_dir / "app.json"
    if not app_json.exists():
        raise ConfigError(f"no app.json or app.config.* in {app_dir}")
    data = load_json(app_json)
    expo = data.get("expo", data) if isinstance(data, dict) else None
    if not isinstance(expo, dict):
        raise ConfigError("app.json has no `expo` object")
    return expo, "app.json"


def normalize_plugins(raw: Any) -> dict[str, dict[str, Any] | None]:
    """Map plugin name -> options dict, or None when listed without options."""
    plugins: dict[str, dict[str, Any] | None] = {}
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            plugins[entry] = None
        elif isinstance(entry, list) and entry and isinstance(entry[0], str):
            options = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else None
            plugins[entry[0]] = options
    return plugins


def git_tracked(app_dir: Path) -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(app_dir), "ls-files", "-z"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return {p for p in result.stdout.decode(errors="replace").split("\0") if p}


def git_ignored(app_dir: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(app_dir), "check-ignore", "-z", "--stdin"],
            input="\0".join(paths).encode(),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {p for p in result.stdout.decode(errors="replace").split("\0") if p}


def walk_files(app_dir: Path) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for name in files:
            found.append(os.path.relpath(os.path.join(root, name), app_dir))
            if len(found) >= 50_000:
                return found
    return found


def read_sources(app_dir: Path, files: list[str]) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for rel in files:
        if Path(rel).suffix not in SOURCE_EXTS or TEST_FILE_RE.search(rel):
            continue
        if Path(rel).name in {"babel.config.js", "metro.config.js", "jest.config.js"}:
            continue
        path = app_dir / rel
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            sources.append((rel, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return sources


def png_info(path: Path) -> tuple[int, int, bool] | None:
    """Return (width, height, has_alpha) for a PNG, or None if it isn't one."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, width, height, alpha = 8, 0, 0, False
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if chunk == b"IHDR" and len(body) >= 10:
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            alpha = body[9] in (4, 6)
        elif chunk == b"tRNS":
            alpha = True
        elif chunk == b"IDAT":
            break
        pos += 12 + length
    return width, height, alpha


def jwt_role(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        role = json.loads(decoded).get("role")
    except (IndexError, ValueError, AttributeError):
        return None
    return role if isinstance(role, str) else None


def mask(value: str) -> str:
    return f"{value[:4]}… ({len(value)} chars)"


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def flatten(value: Any, prefix: str) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        items: list[tuple[str, str]] = []
        for key, child in value.items():
            items.extend(flatten(child, f"{prefix}.{key}"))
        return items
    if isinstance(value, list):
        return [pair for i, child in enumerate(value) for pair in flatten(child, f"{prefix}[{i}]")]
    return [(prefix, str(value))] if isinstance(value, (str, int, float)) else []


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
    return values


def expo_sdk_major(version: str | None) -> int | None:
    match = re.search(r"(\d+)", version or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------------------------
# Context


@dataclass
class Context:
    app_dir: Path
    platforms: set[str]
    mode: str
    expo: dict[str, Any]
    config_label: str
    eas: dict[str, Any] | None
    build_profile_name: str
    build_profile: dict[str, Any] | None
    submit_profile_names: dict[str, str | None]
    submit_profiles: dict[str, dict[str, Any] | None]
    dependencies: dict[str, str]
    plugins: dict[str, dict[str, Any] | None]
    files: list[str]
    tracked: set[str] | None
    sources: list[tuple[str, str]]

    @property
    def final(self) -> bool:
        return self.mode == "final"

    @property
    def store_bound(self) -> bool:
        return (self.build_profile or {}).get("distribution", "store") != "internal"

    def sev(self, final: str, testing: str) -> str:
        return final if self.final else testing

    def has_module(self, name: str) -> bool:
        return name in self.dependencies or name in self.plugins


def choose_submit_profile(
    eas: dict[str, Any] | None, platform: str, mode: str, explicit: str | None
) -> str | None:
    """Pick the submit profile a release of this platform and mode should use."""
    if explicit:
        return explicit
    if mode == "final":
        return "production"
    submit = (eas or {}).get("submit") or {}
    if "testing" in submit:
        return "testing"
    # TestFlight receives store builds whichever profile uploads them; a Play production track
    # does not, so Android testing has no safe fallback.
    return "production" if platform == "ios" else None


def build_context(args: argparse.Namespace) -> Context:
    app_dir = Path(args.app_dir).resolve()
    if not app_dir.is_dir():
        raise ConfigError(f"{app_dir} is not a directory")
    platforms = {"ios", "android"} if args.platform == "all" else {args.platform}
    expo, config_label = load_expo_config(
        app_dir, Path(args.expo_config) if args.expo_config else None
    )

    eas_path = app_dir / "eas.json"
    eas = load_json(eas_path) if eas_path.exists() else None
    build_profile = None
    submit_names: dict[str, str | None] = {}
    submit_profiles: dict[str, dict[str, Any] | None] = {}
    if isinstance(eas, dict):
        build_profile = resolve_profile(eas.get("build") or {}, args.build_profile)
        for platform in platforms:
            name = choose_submit_profile(eas, platform, args.mode, args.submit_profile)
            submit_names[platform] = name
            submit_profiles[platform] = (
                resolve_profile(eas.get("submit") or {}, name) if name else None
            )

    pkg_path = app_dir / "package.json"
    pkg = load_json(pkg_path) if pkg_path.exists() else {}
    dependencies = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}

    files = walk_files(app_dir)
    return Context(
        app_dir=app_dir,
        platforms=platforms,
        mode=args.mode,
        expo=expo,
        config_label=config_label,
        eas=eas if isinstance(eas, dict) else None,
        build_profile_name=args.build_profile,
        build_profile=build_profile,
        submit_profile_names=submit_names,
        submit_profiles=submit_profiles,
        dependencies={k: str(v) for k, v in dependencies.items()},
        plugins=normalize_plugins(expo.get("plugins")),
        files=files,
        tracked=git_tracked(app_dir),
        sources=read_sources(app_dir, files),
    )


# ---------------------------------------------------------------------------------------------
# Common checks


def check_app_identity(ctx: Context, report: Report) -> None:
    version = ctx.expo.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+){0,2}", version):
        report.add(
            id="common.version",
            area="common",
            severity=BLOCKER,
            title="expo.version is missing or not numeric",
            detail=f"Found {version!r}. Both stores need a version of at most three dot-separated "
            "integers (e.g. 1.0.0).",
            fix="Set expo.version to a value like 1.0.0.",
            where=f"{ctx.config_label} expo.version",
        )
    name, slug = ctx.expo.get("name"), ctx.expo.get("slug")
    if isinstance(name, str) and (name == slug or re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", name)):
        report.add(
            id="common.display-name",
            area="common",
            severity=ctx.sev(WARNING, INFO),
            title="App display name looks like a slug",
            detail=f"expo.name is {name!r}; it is the name shown under the home-screen icon.",
            fix="Set expo.name to the human-readable app name.",
            where=f"{ctx.config_label} expo.name",
        )


def check_build_profile(ctx: Context, report: Report) -> None:
    if ctx.eas is None:
        report.add(
            id="common.eas-json",
            area="common",
            severity=BLOCKER,
            title="No eas.json",
            detail="EAS Build needs eas.json to know how to build and sign the app.",
            fix="The user runs `eas build:configure` (interactive; talks to EAS).",
        )
        return
    profile = ctx.build_profile
    if profile is None:
        available = ", ".join(sorted((ctx.eas.get("build") or {}).keys())) or "none"
        report.add(
            id="common.build-profile",
            area="common",
            severity=BLOCKER,
            title=f"Build profile `{ctx.build_profile_name}` not found",
            detail=f"Profiles in eas.json: {available}.",
            fix="Pass --build-profile, or add the profile to eas.json.",
            where="eas.json build",
        )
        return
    where = f"eas.json build.{ctx.build_profile_name}"

    if not ctx.store_bound:
        if ctx.final:
            report.add(
                id="common.internal-distribution",
                area="common",
                severity=BLOCKER,
                title="Final submission from an internal-distribution profile",
                detail="`distribution: internal` builds are ad hoc IPAs or APKs that neither "
                "store accepts.",
                fix="Use a profile with distribution `store` (usually `production`).",
                where=where,
            )
        else:
            report.add(
                id="common.internal-build",
                area="common",
                severity=INFO,
                title="Internal-distribution build: not uploaded to a store",
                detail="Installs directly on registered devices (iOS) or as an APK (Android). "
                "Submission checks were skipped. Build with --no-submit.",
                where=where,
            )
    if ctx.store_bound and profile.get("developmentClient"):
        report.add(
            id="common.dev-client",
            area="common",
            severity=BLOCKER,
            title="Store build includes the development client",
            detail="`developmentClient: true` ships the dev menu and expects a Metro bundler; "
            "the app won't run standalone and will be rejected.",
            fix="Remove developmentClient from this profile.",
            where=where,
        )
    if ctx.store_bound and "ios" in ctx.platforms and (profile.get("ios") or {}).get("simulator"):
        report.add(
            id="apple.simulator-build",
            area="apple",
            severity=BLOCKER,
            title="Store profile builds for the iOS simulator",
            detail="Simulator builds can't be uploaded to App Store Connect.",
            fix="Remove ios.simulator from this profile.",
            where=f"{where}.ios.simulator",
        )

    cli = ctx.eas.get("cli") or {}
    source = cli.get("appVersionSource")
    if source is None:
        report.add(
            id="common.app-version-source",
            area="common",
            severity=WARNING,
            title="cli.appVersionSource is not set",
            detail="Without it, EAS falls back to local build numbers and warns; a repeated build "
            "number is rejected on upload.",
            fix='Set "cli": {"appVersionSource": "remote"} and autoIncrement: true on the store '
            "profile.",
            where="eas.json cli",
        )
    if ctx.store_bound and not profile.get("autoIncrement"):
        manual_numbers = (ctx.expo.get("ios") or {}).get("buildNumber") or (
            ctx.expo.get("android") or {}
        ).get("versionCode")
        if source == "remote" or not manual_numbers:
            report.add(
                id="common.auto-increment",
                area="common",
                severity=WARNING,
                title="Build number is not auto-incremented",
                detail="Every upload needs a build number the store hasn't seen; a rejected "
                "build still burns its number.",
                fix="Set autoIncrement: true on this profile.",
                where=f"{where}.autoIncrement",
            )
    else:
        report.passed.append("build profile shape (distribution, dev client, build numbers)")


def check_env(ctx: Context, report: Report) -> None:
    profile = ctx.build_profile or {}
    where = f"eas.json build.{ctx.build_profile_name}.env"
    sources: list[tuple[str, dict[str, str]]] = [
        (where, {k: str(v) for k, v in (profile.get("env") or {}).items()})
    ]
    for rel in ctx.files:
        name = Path(rel).name
        committed_dotenv = (
            name.startswith(".env")
            and not name.endswith(".local")
            and not ENV_TEMPLATE_RE.search(name)
            and ctx.tracked is not None
            and rel in ctx.tracked
        )
        if committed_dotenv:
            try:
                text = (ctx.app_dir / rel).read_text(encoding="utf-8")
            except OSError:
                continue
            sources.append((rel, parse_dotenv(text)))

    bad_urls = False
    for label, values in sources:
        for key, value in values.items():
            if LOCAL_URL_RE.match(value):
                bad_urls = True
                report.add(
                    id=f"common.local-url.{key}",
                    area="common",
                    severity=ctx.sev(BLOCKER, WARNING),
                    title=f"{key} points at a local or tunnel address",
                    detail=f"{value} is compiled into the build; testers' phones can't reach it.",
                    fix="Use the deployed https URL for this profile.",
                    where=f"{label} {key}",
                )
            elif value.startswith("http://"):
                bad_urls = True
                report.add(
                    id=f"security.cleartext-env.{key}",
                    area="security",
                    severity=ctx.sev(BLOCKER, WARNING),
                    title=f"{key} uses cleartext http://",
                    detail=f"{value}: iOS App Transport Security blocks it by default and traffic "
                    "can be read or modified in transit.",
                    fix="Use https://.",
                    where=f"{label} {key}",
                )
    if not bad_urls:
        report.passed.append("build-time URLs are https and non-local")

    used: dict[str, str] = {}
    for rel, text in ctx.sources:
        for match in ENV_USE_RE.finditer(text):
            used.setdefault(match.group(1), rel)
    if not used:
        return
    if "environment" in profile:
        report.add(
            id="common.eas-environment",
            area="common",
            severity=INFO,
            title="Some build variables live on EAS servers",
            detail=f"Profile uses EAS environment `{profile['environment']}`; this script can't "
            f"see those values. App reads: {', '.join(sorted(used))}.",
            fix="Ask the user to run `! eas env:list --environment "
            f"{profile['environment']}` to confirm they are set.",
            where=f"eas.json build.{ctx.build_profile_name}.environment",
        )
        return
    defined = {key for _, values in sources for key in values}
    for key in sorted(set(used) - defined):
        report.add(
            id=f"common.undefined-env.{key}",
            area="common",
            severity=WARNING,
            title=f"{key} is read by the app but not defined for this build",
            detail=f"First used in {used[key]}. EXPO_PUBLIC_ values are inlined at build time; an "
            "undefined one compiles to `undefined` unless the code has a fallback.",
            fix=f"Add {key} to {where}, or confirm the code's fallback is correct for production.",
            where=used[key],
        )


def check_account_deletion(ctx: Context, report: Report) -> None:
    creates = any(
        ACCOUNT_CREATION_RE.search(text) or re.search(r"sign-?up|register", rel, re.IGNORECASE)
        for rel, text in ctx.sources
    )
    if not creates:
        return
    if any(ACCOUNT_DELETION_RE.search(text) for _, text in ctx.sources):
        report.passed.append(
            "in-app account deletion code found by name (confirm it calls an authenticated endpoint)"
        )
        return
    report.add(
        id="common.account-deletion",
        area="common",
        severity=ctx.sev(BLOCKER, INFO),
        title="No in-app account deletion found",
        detail="The app appears to create accounts. Apple (guideline 5.1.1(v)) and Google Play "
        "require users to be able to delete their account from inside the app; Play also needs a "
        "web deletion URL.",
        fix="Confirm by searching the settings or profile screen; if absent, add a delete-account "
        "flow backed by a server endpoint.",
    )


# ---------------------------------------------------------------------------------------------
# Apple


def check_apple(ctx: Context, report: Report) -> None:
    ios = ctx.expo.get("ios") or {}
    info_plist = ios.get("infoPlist") or {}
    cfg = ctx.config_label

    bundle_id = ios.get("bundleIdentifier")
    if not isinstance(bundle_id, str) or not re.fullmatch(
        r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", bundle_id
    ):
        report.add(
            id="apple.bundle-id",
            area="apple",
            severity=BLOCKER,
            title="ios.bundleIdentifier missing or invalid",
            detail=f"Found {bundle_id!r}; it must be reverse-DNS and match the App Store Connect "
            "record. It can't change after the first upload.",
            fix="Set ios.bundleIdentifier (e.g. com.company.app).",
            where=f"{cfg} expo.ios",
        )
    else:
        report.passed.append(f"bundle identifier {bundle_id}")

    check_icon(ctx, report, ios)
    check_purpose_strings(ctx, report, info_plist)

    if "ITSAppUsesNonExemptEncryption" not in info_plist:
        report.add(
            id="apple.export-compliance",
            area="apple",
            severity=WARNING,
            title="Export compliance is not pre-answered",
            detail="Each build will wait at 'Missing Compliance' in TestFlight until someone "
            "answers the encryption question by hand.",
            fix="If the app only uses HTTPS, OS crypto and hashing, set "
            "ios.infoPlist.ITSAppUsesNonExemptEncryption to false. Confirm with the user first.",
            where=f"{cfg} expo.ios.infoPlist",
        )

    ats = info_plist.get("NSAppTransportSecurity") or {}
    if ats.get("NSAllowsArbitraryLoads"):
        report.add(
            id="security.ats-arbitrary-loads",
            area="security",
            severity=ctx.sev(BLOCKER, WARNING),
            title="App Transport Security is disabled",
            detail="NSAllowsArbitraryLoads allows cleartext HTTP everywhere; App Review asks for "
            "justification and traffic can be intercepted.",
            fix="Remove it; use NSExceptionDomains for any specific host that truly needs HTTP.",
            where=f"{cfg} expo.ios.infoPlist.NSAppTransportSecurity",
        )

    apple_auth = ctx.has_module("expo-apple-authentication")
    social = [dep for dep in SOCIAL_LOGIN_DEPS if dep in ctx.dependencies]
    if social and not apple_auth:
        report.add(
            id="apple.sign-in-with-apple",
            area="apple",
            severity=ctx.sev(BLOCKER, WARNING),
            title="Third-party login without Sign in with Apple",
            detail=f"Found {', '.join(social)}. Guideline 4.8 requires Sign in with Apple (or an "
            "equivalent privacy-preserving login) when social login is offered.",
            fix="Confirm the dependency is used for social login; if so add "
            "expo-apple-authentication.",
        )
    if (
        apple_auth
        and "expo-apple-authentication" not in ctx.plugins
        and not ios.get("usesAppleSignIn")
    ):
        report.add(
            id="apple.apple-sign-in-entitlement",
            area="apple",
            severity=BLOCKER,
            title="Sign in with Apple entitlement is not configured",
            detail="expo-apple-authentication is installed but neither listed in plugins nor "
            "enabled with ios.usesAppleSignIn, so the entitlement is missing and sign-in fails.",
            fix='Add "expo-apple-authentication" to plugins.',
            where=f"{cfg} expo.plugins",
        )

    if not ios.get("privacyManifests"):
        report.add(
            id="apple.privacy-manifest",
            area="apple",
            severity=INFO,
            title="No app-level privacy manifest entries",
            detail="Expo merges module manifests, which usually suffices. Apple emails ITMS-91053 "
            "after upload if required-reason API declarations are missing.",
            fix="Only add ios.privacyManifests if the user has received ITMS-91053.",
        )
    if ios.get("supportsTablet"):
        report.add(
            id="apple.tablet",
            area="apple",
            severity=ctx.sev(MANUAL, INFO),
            title="iPad is supported",
            detail="ios.supportsTablet is true: the app must work well on iPad and App Store "
            "Connect requires 13-inch iPad screenshots.",
            fix="Confirm iPad layouts, or set supportsTablet to false.",
        )

    if ctx.store_bound:
        check_apple_submit(ctx, report)


def check_icon(ctx: Context, report: Report, ios: dict[str, Any]) -> None:
    icon = ios.get("icon") or ctx.expo.get("icon")
    if isinstance(icon, dict):  # iOS 18 light/dark/tinted variants
        icon = icon.get("light") or icon.get("any")
    if not isinstance(icon, str):
        report.add(
            id="apple.icon-missing",
            area="apple",
            severity=BLOCKER,
            title="No app icon configured",
            detail="Apple requires a 1024x1024 marketing icon.",
            fix="Set expo.icon to a 1024x1024 PNG without transparency.",
            where=f"{ctx.config_label} expo.icon",
        )
        return
    info = png_info(ctx.app_dir / icon)
    if info is None:
        report.add(
            id="apple.icon-unreadable",
            area="apple",
            severity=BLOCKER,
            title="App icon is missing or not a PNG",
            detail=f"{icon} could not be read as a PNG.",
            fix="Point expo.icon at a 1024x1024 PNG.",
            where=f"{ctx.config_label} expo.icon",
        )
        return
    width, height, alpha = info
    if (width, height) != (1024, 1024):
        report.add(
            id="apple.icon-size",
            area="apple",
            severity=BLOCKER,
            title="App icon is not 1024x1024",
            detail=f"{icon} is {width}x{height}.",
            fix="Export the icon at 1024x1024.",
            where=icon,
        )
    if alpha and ctx.store_bound:
        report.add(
            id="apple.icon-alpha",
            area="apple",
            severity=BLOCKER,
            title="iOS app icon has an alpha channel",
            detail=f"{icon} has transparency data. App Store Connect rejects it (ITMS-90717) even "
            "if every pixel is opaque.",
            fix="Re-export the PNG without alpha, or set ios.icon to an opaque copy.",
            where=icon,
        )
    if (width, height) == (1024, 1024) and not alpha:
        report.passed.append("iOS icon is 1024x1024 without alpha")


def check_purpose_strings(ctx: Context, report: Report, info_plist: dict[str, Any]) -> None:
    checked: set[str] = set()
    missing = False
    for module, strings in IOS_PURPOSE_STRINGS.items():
        if module not in ctx.dependencies:
            continue
        listed = module in ctx.plugins or module in AUTO_APPLIED_PLUGINS
        options = ctx.plugins.get(module) or {}
        for purpose in strings:
            if purpose.key in checked:
                continue
            value = info_plist.get(purpose.key)
            option = options.get(purpose.plugin_option) if purpose.plugin_option else None
            provided_by_other_plugin = any(
                isinstance(opts, dict)
                and any(
                    p.plugin_option and isinstance(opts.get(p.plugin_option), str)
                    for p in IOS_PURPOSE_STRINGS.get(name, ())
                    if p.key == purpose.key
                )
                for name, opts in ctx.plugins.items()
                if name != module
            )
            if isinstance(value, str) or isinstance(option, str) or provided_by_other_plugin:
                checked.add(purpose.key)
                text = (
                    value if isinstance(value, str) else option if isinstance(option, str) else ""
                )
                if text and GENERIC_PURPOSE_RE.search(text):
                    report.add(
                        id=f"apple.generic-purpose.{purpose.key}",
                        area="apple",
                        severity=ctx.sev(WARNING, INFO),
                        title=f"{purpose.key} is a generic string",
                        detail=f"{text!r} doesn't say why the app needs access; App Review "
                        "rejects vague purpose strings (guideline 5.1.1).",
                        fix="Rewrite it to name the feature that uses it.",
                        where=f"{ctx.config_label} expo.ios.infoPlist",
                    )
                continue
            if option is False:
                checked.add(purpose.key)
                continue
            if listed and purpose.plugin_option:
                checked.add(purpose.key)
                report.add(
                    id=f"apple.default-purpose.{purpose.key}",
                    area="apple",
                    severity=ctx.sev(WARNING, INFO),
                    title=f"{purpose.key} uses {module}'s generic default",
                    detail=f"{module}'s config plugin runs without `{purpose.plugin_option}` "
                    "(Expo applies it even when it isn't listed), so it writes 'Allow "
                    "$(PRODUCT_NAME) to access…'. That passes processing, but App Review may "
                    "reject it as vague.",
                    fix=f"Pass `{purpose.plugin_option}` with a specific reason in the {module} "
                    "plugin options, or set the key in ios.infoPlist.",
                    where=f"{ctx.config_label} expo.plugins",
                )
                continue
            checked.add(purpose.key)
            missing = True
            report.add(
                id=f"apple.purpose-string.{purpose.key}",
                area="apple",
                severity=BLOCKER,
                title=f"Missing iOS purpose string {purpose.key}",
                detail=f"{module} links APIs that need this key. Apple rejects the upload during "
                "processing (ITMS-90683) whether or not the app calls them; this fails TestFlight "
                "too.",
                fix=f"Add {purpose.key} to ios.infoPlist with an honest, specific reason"
                + (
                    f" (or pass `{purpose.plugin_option}` to the {module} plugin)"
                    if purpose.plugin_option
                    else ""
                )
                + ". Never edit the generated ios/ folder.",
                where=f"{ctx.config_label} expo.ios.infoPlist",
            )
    if checked and not missing:
        report.passed.append("iOS purpose strings for installed modules")


def check_apple_submit(ctx: Context, report: Report) -> None:
    name = ctx.submit_profile_names.get("ios")
    profile = ctx.submit_profiles.get("ios") or {}
    ios_submit = profile.get("ios") or {}
    if not ios_submit.get("ascAppId"):
        report.add(
            id="apple.asc-app-id",
            area="apple",
            severity=WARNING,
            title="Submit profile doesn't pin the App Store Connect app",
            detail=f"submit.{name}.ios.ascAppId is unset, so `eas submit` prompts for the app "
            "record and fails in non-interactive runs.",
            fix="Add ascAppId (App Store Connect > App Information > Apple ID) and appleTeamId. "
            "Neither is secret. Ask the user for the values.",
            where=f"eas.json submit.{name}.ios",
        )
    if ctx.final:
        report.add(
            id="apple.store-console-final",
            area="apple",
            severity=MANUAL,
            title="App Store Connect: listing and review information",
            detail="Screenshots (6.9-inch iPhone; 13-inch iPad if supported), description, "
            "keywords, support and privacy policy URLs, App Privacy questionnaire matching the app "
            "and its SDKs, age rating, review notes with a demo account if sign-in is required. "
            "After upload: attach the build to a version and Submit to App Review.",
        )
    else:
        report.add(
            id="apple.store-console-testing",
            area="apple",
            severity=MANUAL,
            title="TestFlight: tester setup",
            detail="An App Store Connect app record for this bundle ID must exist before the first "
            "submit. Internal testers must be team members; external testers need Test "
            "Information and Beta App Review.",
        )


# ---------------------------------------------------------------------------------------------
# Google


def check_google(ctx: Context, report: Report) -> None:
    android = ctx.expo.get("android") or {}
    cfg = ctx.config_label

    package = android.get("package")
    if not isinstance(package, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", package
    ):
        report.add(
            id="google.package",
            area="google",
            severity=BLOCKER,
            title="android.package missing or invalid",
            detail=f"Found {package!r}. It must be a valid Java package name and is permanent "
            "after the first upload.",
            fix="Set android.package (e.g. com.company.app).",
            where=f"{cfg} expo.android",
        )
    elif package.startswith("com.example"):
        report.add(
            id="google.package-example",
            area="google",
            severity=BLOCKER,
            title="android.package uses com.example",
            detail="Google Play rejects packages under com.example.",
            fix="Choose a package you own; it can't change after the first upload.",
            where=f"{cfg} expo.android.package",
        )
    else:
        report.passed.append(f"Android package {package}")

    adaptive = android.get("adaptiveIcon") or {}
    for key in ("foregroundImage", "backgroundImage", "monochromeImage"):
        path = adaptive.get(key)
        if isinstance(path, str) and not (ctx.app_dir / path).exists():
            report.add(
                id=f"google.adaptive-icon.{key}",
                area="google",
                severity=BLOCKER,
                title=f"Adaptive icon {key} file is missing",
                detail=f"{path} does not exist.",
                fix="Fix the path or add the image.",
                where=f"{cfg} expo.android.adaptiveIcon",
            )

    profile = ctx.build_profile or {}
    if ctx.store_bound and (profile.get("android") or {}).get("buildType") == "apk":
        report.add(
            id="google.apk",
            area="google",
            severity=BLOCKER,
            title="Store profile builds an APK",
            detail="Google Play only accepts Android App Bundles (AAB) for new apps.",
            fix="Remove android.buildType (the default is app-bundle) from this profile.",
            where=f"eas.json build.{ctx.build_profile_name}.android.buildType",
        )

    check_target_sdk(ctx, report)
    check_android_permissions(ctx, report, android)
    if ctx.store_bound:
        check_google_submit(ctx, report)


def check_target_sdk(ctx: Context, report: Report) -> None:
    build_props = ctx.plugins.get("expo-build-properties") or {}
    override = (build_props.get("android") or {}).get("targetSdkVersion")
    sdk = expo_sdk_major(ctx.dependencies.get("expo"))
    if isinstance(override, int):
        target, source = override, "expo-build-properties android.targetSdkVersion"
    elif sdk is not None:
        target = EXPO_SDK_TARGET_SDK.get(sdk, PLAY_NEXT_TARGET_SDK if sdk > 54 else 33)
        source = f"Expo SDK {sdk} default"
    else:
        return
    if target < PLAY_MIN_TARGET_SDK:
        report.add(
            id="google.target-sdk",
            area="google",
            severity=BLOCKER,
            title=f"Android target API {target} is below Play's minimum",
            detail=f"{source} targets API {target}; Play requires at least {PLAY_MIN_TARGET_SDK} "
            "for new apps and updates.",
            fix="Upgrade the Expo SDK (plan it; native modules may need updates) rather than "
            "raising targetSdkVersion alone.",
        )
    elif target < PLAY_NEXT_TARGET_SDK:
        report.add(
            id="google.target-sdk-next",
            area="google",
            severity=WARNING,
            title=f"Android target API {target} may be below the current requirement",
            detail=f"{source} targets API {target}. Google raises the minimum every August 31; API "
            f"{PLAY_NEXT_TARGET_SDK} is expected from 2026-08-31.",
            fix="Confirm at developer.android.com/google/play/requirements/target-sdk; upgrade "
            "the Expo SDK if needed.",
        )
    else:
        report.passed.append(f"Android target API {target}")


def check_android_permissions(ctx: Context, report: Report, android: dict[str, Any]) -> None:
    def short(name: str) -> str:
        return name.rsplit(".", 1)[-1]

    blocked = {short(p) for p in android.get("blockedPermissions") or [] if isinstance(p, str)}
    declared = {short(p) for p in android.get("permissions") or [] if isinstance(p, str)}
    manifest = ctx.app_dir / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    from_manifest: set[str] = set()
    if manifest.exists():
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"<uses-permission\b[^>]*>", text):
                tag = match.group(0)
                name = re.search(r'android:name="([^"]+)"', tag)
                if name and 'tools:node="remove"' not in tag:
                    from_manifest.add(short(name.group(1)))
        except OSError:
            pass

    for perm in sorted((declared | from_manifest) - blocked):
        policy = RESTRICTED_ANDROID_PERMISSIONS.get(perm)
        if policy is None:
            continue
        origin = (
            "android.permissions"
            if perm in declared
            else "generated android/ manifest (may be stale)"
        )
        report.add(
            id=f"google.restricted-permission.{perm}",
            area="google",
            severity=ctx.sev(BLOCKER, WARNING),
            title=f"Restricted permission {perm}",
            detail=f"From {origin}. Falls under Play's {policy}: needs a Permissions Declaration "
            "in Play Console and is often rejected unless core to the app.",
            fix=f"If the app doesn't need it, add {perm} to android.blockedPermissions.",
            where=f"{ctx.config_label} expo.android",
        )

    if "permissions" not in android:
        added = {
            module: [p for p in perms if p not in blocked]
            for module, perms in MODULE_ANDROID_PERMISSIONS.items()
            if module in ctx.dependencies
        }
        added = {m: p for m, p in added.items() if p}
        if added:
            listing = "; ".join(f"{m}: {', '.join(p)}" for m, p in sorted(added.items()))
            report.add(
                id="google.merged-permissions",
                area="google",
                severity=ctx.sev(WARNING, INFO),
                title="Android permissions come from installed modules",
                detail=f"No android.permissions allowlist, so modules merge their own: {listing}. "
                "Each must be justified in the Data safety form.",
                fix="Add unused ones to android.blockedPermissions.",
                where=f"{ctx.config_label} expo.android",
            )


def check_google_submit(ctx: Context, report: Report) -> None:
    name = ctx.submit_profile_names.get("android")
    profile = ctx.submit_profiles.get("android")
    android_submit = (profile or {}).get("android")
    if name is None or not isinstance(android_submit, dict):
        testing_hint = (
            'Add "submit": {"testing": {"android": {"track": "internal"}}} to eas.json.'
            if not ctx.final
            else 'Add "android": {"track": "production", "releaseStatus": "draft"} to '
            "submit.production."
        )
        report.add(
            id="google.submit-profile",
            area="google",
            severity=BLOCKER,
            title="No Android submit profile for this mode",
            detail=(
                f"submit.{name}.android is not defined."
                if name
                else "There is no `testing` submit profile, and Android testing must not fall back "
                "to `production`: that could publish to the production track."
            ),
            fix=testing_hint,
            where="eas.json submit",
        )
    else:
        track = android_submit.get("track", "internal")
        where = f"eas.json submit.{name}.android"
        if not ctx.final and track == "production":
            report.add(
                id="google.testing-to-production",
                area="google",
                severity=BLOCKER,
                title="Testing submission targets the production track",
                detail=f"submit.{name}.android.track is production; this 'testing' release would "
                "go to the public after review.",
                fix="Use track internal (or alpha/beta) in the testing submit profile.",
                where=where,
            )
        elif ctx.final and track != "production":
            report.add(
                id="google.final-track",
                area="google",
                severity=WARNING,
                title=f"Final submission targets the `{track}` track",
                detail="The release won't reach the production track.",
                fix="Set track to production in the production submit profile.",
                where=where,
            )
        else:
            report.passed.append(f"Android submit track `{track}` matches the mode")
        if ctx.final and "releaseStatus" not in android_submit:
            report.add(
                id="google.release-status",
                area="google",
                severity=INFO,
                title="Production release status defaults to completed",
                detail="The release goes to review immediately and publishes on approval.",
                fix='Consider releaseStatus: "draft" so the user starts the rollout by hand.',
                where=where,
            )
        key_path = android_submit.get("serviceAccountKeyPath")
        if isinstance(key_path, str):
            rel = os.path.normpath(key_path)
            if ctx.tracked is not None and rel in ctx.tracked:
                report.add(
                    id="security.service-account-tracked",
                    area="security",
                    severity=BLOCKER,
                    title="Google service account key is committed",
                    detail=f"{key_path} grants publishing rights to the Play account.",
                    fix="The user must revoke this key in Google Cloud, remove it from git, and "
                    "upload a new key with `eas credentials`.",
                    where=key_path,
                )
            elif not (ctx.app_dir / key_path).exists():
                report.add(
                    id="google.service-account-path",
                    area="google",
                    severity=INFO,
                    title="serviceAccountKeyPath file not found locally",
                    detail=f"{key_path} doesn't exist. Fine if the key is stored on EAS with "
                    "`eas credentials`; otherwise eas submit fails.",
                    where=where,
                )

    report.add(
        id="google.first-upload",
        area="google",
        severity=MANUAL,
        title="Play Console: first upload and setup",
        detail="The Play API can't create an app's first release; the first AAB must be uploaded "
        "by hand in Play Console. eas submit also needs a Google Service Account key.",
    )
    if ctx.final:
        report.add(
            id="google.store-console-final",
            area="google",
            severity=MANUAL,
            title="Play Console: listing and policy forms",
            detail="Store listing (512 px icon, 1024x500 feature graphic, 2+ screenshots), content "
            "rating, target audience, Data safety form matching the app and its SDKs, privacy "
            "policy URL, account-deletion web URL, app access credentials for reviewers, ads "
            "declaration. Personal accounts created after 2023-11-13 need a 12-tester, 14-day "
            "closed test before production access.",
        )


# ---------------------------------------------------------------------------------------------
# Security


def check_security(ctx: Context, report: Report) -> None:
    check_public_config_secrets(ctx, report)
    check_source_patterns(ctx, report)
    check_sensitive_files(ctx, report)

    build_props = ctx.plugins.get("expo-build-properties") or {}
    if "android" in ctx.platforms and (build_props.get("android") or {}).get(
        "usesCleartextTraffic"
    ):
        report.add(
            id="security.android-cleartext",
            area="security",
            severity=ctx.sev(BLOCKER, WARNING),
            title="Android cleartext traffic is enabled",
            detail="usesCleartextTraffic lets the app send unencrypted HTTP.",
            fix="Remove it from expo-build-properties.",
            where=f"{ctx.config_label} expo.plugins",
        )
    if "expo-updates" in ctx.dependencies:
        updates = ctx.expo.get("updates") or {}
        if not updates.get("codeSigningCertificate"):
            report.add(
                id="security.updates-signing",
                area="security",
                severity=INFO,
                title="OTA updates are not code-signed",
                detail="expo-updates is installed without updates.codeSigningCertificate; the app "
                "trusts any update served from the configured URL.",
                fix="Consider EAS Update code signing for apps handling sensitive data.",
            )
        if not ctx.expo.get("runtimeVersion"):
            report.add(
                id="common.runtime-version",
                area="common",
                severity=WARNING,
                title="expo-updates without runtimeVersion",
                detail="Without a runtime version policy an OTA update can reach a binary with "
                "incompatible native code and crash it.",
                fix='Set "runtimeVersion": {"policy": "appVersion"}.',
                where=f"{ctx.config_label} expo",
            )


def secret_findings_for_value(
    ctx: Context, report: Report, value: str, where: str, public: bool
) -> bool:
    found = False
    for label, pattern, severity in SECRET_VALUE_PATTERNS:
        for match in pattern.finditer(value):
            found = True
            report.add(
                id=f"security.secret.{label.lower().replace(' ', '-')}.{where}",
                area="security",
                severity=severity,
                title=f"{label} {'in the app bundle' if public else 'committed'}",
                detail=f"{mask(match.group(0))} at {where}. Anyone who installs the build can "
                "extract bundled values.",
                fix="Move the call behind a backend endpoint; the user must rotate the key.",
                where=where,
            )
    for match in JWT_RE.finditer(value):
        if jwt_role(match.group(0)) == "service_role":
            found = True
            report.add(
                id=f"security.service-role-jwt.{where}",
                area="security",
                severity=BLOCKER,
                title="Supabase service_role key found",
                detail=f"{mask(match.group(0))} at {where}. It bypasses row-level security: "
                "anyone who extracts it can read and write every table.",
                fix="Remove it from the app; the user must rotate it in the Supabase dashboard.",
                where=where,
            )
    return found


def check_public_config_secrets(ctx: Context, report: Report) -> None:
    found = False
    for path, value in flatten(ctx.expo.get("extra") or {}, "expo.extra"):
        found |= secret_findings_for_value(ctx, report, value, path, public=True)
        leaf = path.rsplit(".", 1)[-1]
        if SECRET_NAME_RE.search(leaf):
            found = True
            report.add(
                id=f"security.secret-name.{path}",
                area="security",
                severity=BLOCKER,
                title=f"Secret-looking value in {path}",
                detail=f"expo.extra is readable at runtime by anyone with the build ({mask(value)}).",
                fix="Confirm what it is; if secret, move it server-side and rotate it.",
                where=f"{ctx.config_label} {path}",
            )
    env = (ctx.build_profile or {}).get("env") or {}
    for key, raw in env.items():
        value = str(raw)
        public = key.startswith("EXPO_PUBLIC_")
        where = f"eas.json build.{ctx.build_profile_name}.env.{key}"
        found |= secret_findings_for_value(ctx, report, value, where, public=public)
        if SECRET_NAME_RE.search(key):
            found = True
            report.add(
                id=f"security.secret-name.{key}",
                area="security",
                severity=BLOCKER if public else WARNING,
                title=f"{key} looks like a secret"
                + (" and is inlined into the bundle" if public else " committed in eas.json"),
                detail=(
                    "EXPO_PUBLIC_ variables are compiled into the JavaScript bundle."
                    if public
                    else "eas.json is committed; secrets belong in EAS environment variables "
                    "with secret visibility."
                ),
                fix="Confirm what it is; if secret, move it out and rotate it.",
                where=where,
            )
    if not found:
        report.passed.append("no secrets detected in expo.extra or build env")


def check_source_patterns(ctx: Context, report: Report) -> None:
    uses_webview = "react-native-webview" in ctx.dependencies
    for rel, text in ctx.sources:
        secret_findings_for_value(ctx, report, text, rel, public=True)
        for match in ENV_USE_RE.finditer(text):
            if SECRET_NAME_RE.search(match.group(1)):
                report.add(
                    id=f"security.public-secret-env.{match.group(1)}.{rel}",
                    area="security",
                    severity=BLOCKER,
                    title=f"App reads {match.group(1)}",
                    detail=f"{rel}:{line_of(text, match.start())}. EXPO_PUBLIC_ values are public; "
                    "the name suggests a secret.",
                    fix="Confirm what it is; if secret, move it server-side and rotate it.",
                    where=f"{rel}:{line_of(text, match.start())}",
                )
        for match in HTTP_URL_RE.finditer(text):
            url = match.group(1)
            if LOCAL_URL_RE.match(url) or "schemas.android.com" in url or "www.w3.org" in url:
                continue
            report.add(
                id=f"security.http-url.{rel}:{line_of(text, match.start())}",
                area="security",
                severity=WARNING,
                title="Cleartext http:// URL in source",
                detail=f"{url} at {rel}:{line_of(text, match.start())}.",
                fix="Use https:// if the app calls it.",
                where=f"{rel}:{line_of(text, match.start())}",
            )
        checks: list[tuple[re.Pattern[str], str, str, str]] = [
            (
                ASYNC_STORAGE_TOKEN_RE,
                "security.asyncstorage-token",
                "Credential stored in AsyncStorage",
                "AsyncStorage is unencrypted. Use expo-secure-store (Keychain/Keystore) for tokens.",
            ),
            (
                LOG_SECRET_RE,
                "security.log-secret",
                "Credential may be logged",
                "Production console output is readable in device logs. Remove or gate on __DEV__.",
            ),
        ]
        if uses_webview:
            checks += [
                (
                    WEBVIEW_WILDCARD_RE,
                    "security.webview-origin",
                    "WebView allows any origin",
                    (
                        "originWhitelist ['*'] lets the WebView navigate anywhere, including to "
                        "attacker pages that can call injected bridges."
                    ),
                ),
                (
                    WEBVIEW_FILE_ACCESS_RE,
                    "security.webview-file-access",
                    "WebView file URL access",
                    "File-URL access flags let loaded content read local files.",
                ),
            ]
        for pattern, fid, title, fix in checks:
            for match in pattern.finditer(text):
                line = line_of(text, match.start())
                report.add(
                    id=f"{fid}.{rel}:{line}",
                    area="security",
                    severity=WARNING,
                    title=title,
                    detail=f"{rel}:{line}: {match.group(0)[:100]}",
                    fix=fix,
                    where=f"{rel}:{line}",
                )


def check_sensitive_files(ctx: Context, report: Report) -> None:
    candidates: list[tuple[str, str]] = []
    for rel in ctx.files:
        posix = rel.replace(os.sep, "/")
        if ENV_TEMPLATE_RE.search(posix):
            continue
        for pattern, label in SENSITIVE_FILES:
            if pattern.search(posix):
                candidates.append((rel, label))
                break
        else:
            if posix.endswith(".json") and not posix.endswith(
                ("package.json", "package-lock.json")
            ):
                path = ctx.app_dir / rel
                try:
                    if path.stat().st_size < 200_000:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        if '"private_key"' in text and '"client_email"' in text:
                            candidates.append((rel, "Google service account key"))
                except OSError:
                    continue
    if not candidates:
        report.passed.append("no keystores, keys or env files in the app directory")
        return
    if ctx.tracked is None:
        for rel, label in candidates:
            report.add(
                id=f"security.sensitive-file.{rel}",
                area="security",
                severity=WARNING,
                title=f"{label} in the app directory (not a git repo, can't tell if shared)",
                detail=f"{rel}. EAS uploads the project directory unless .easignore excludes it.",
                fix="Move it out of the project or add it to .easignore.",
                where=rel,
            )
        return
    ignored = git_ignored(ctx.app_dir, [rel for rel, _ in candidates if rel not in ctx.tracked])
    for rel, label in candidates:
        is_env = label == "environment file"
        if rel in ctx.tracked:
            report.add(
                id=f"security.sensitive-file.{rel}",
                area="security",
                severity=WARNING if is_env else BLOCKER,
                title=f"{label} is committed to git",
                detail=f"{rel} is tracked"
                + (
                    ". Check it holds no secrets."
                    if is_env
                    else ". Everyone with repo access, and the history forever, has it."
                ),
                fix="The user removes it from git and rotates or replaces the credential; "
                "consider EAS-managed credentials.",
                where=rel,
            )
        elif rel not in ignored:
            report.add(
                id=f"security.sensitive-file.{rel}",
                area="security",
                severity=WARNING,
                title=f"{label} is not gitignored",
                detail=f"{rel} is untracked but not ignored: one `git add .` from being committed, "
                "and EAS Build uploads it with the project.",
                fix="Add it to .gitignore (and .easignore if one exists).",
                where=rel,
            )
        elif not is_env:
            report.add(
                id=f"security.sensitive-file.{rel}",
                area="security",
                severity=INFO,
                title=f"{label} kept inside the repo (gitignored)",
                detail=f"{rel} is ignored, so it isn't committed or uploaded. Losing it can block "
                "releases (an Android upload key needs a Play support reset).",
                fix="The user should keep a backup outside the repo. Don't move or delete it.",
                where=rel,
            )


# ---------------------------------------------------------------------------------------------
# Output


def run_checks(ctx: Context) -> Report:
    report = Report()
    check_app_identity(ctx, report)
    check_build_profile(ctx, report)
    check_env(ctx, report)
    check_account_deletion(ctx, report)
    if "ios" in ctx.platforms:
        check_apple(ctx, report)
    if "android" in ctx.platforms:
        check_google(ctx, report)
    check_security(ctx, report)
    report.findings.sort(key=lambda f: (SEVERITY_ORDER.index(f.severity), f.area, f.id))
    return report


def render_text(ctx: Context, report: Report) -> str:
    submit = (
        ", ".join(f"{p}: {n or 'none'}" for p, n in sorted(ctx.submit_profile_names.items()))
        or "n/a"
    )
    distribution = (ctx.build_profile or {}).get("distribution", "store")
    counts = {s: sum(f.severity == s for f in report.findings) for s in SEVERITY_ORDER}
    lines = [
        f"Expo store preflight: platform={'+'.join(sorted(ctx.platforms))} mode={ctx.mode}",
        f"app: {ctx.app_dir}  config: {ctx.config_label}",
        (
            f"build profile: {ctx.build_profile_name} (distribution: {distribution})  "
            f"submit profile: {submit}"
        ),
        "  ".join(f"{s}: {counts[s]}" for s in SEVERITY_ORDER),
    ]
    for area, heading in AREAS:
        items = [f for f in report.findings if f.area == area]
        if not items:
            continue
        lines += ["", f"## {heading}"]
        for f in items:
            lines.append(f"[{f.severity.upper()}] {f.title}")
            lines.append(f"  {f.detail}")
            if f.where:
                lines.append(f"  where: {f.where}")
            if f.fix:
                lines.append(f"  fix:   {f.fix}")
    if report.passed:
        lines += ["", "## Passed", *(f"- {p}" for p in report.passed)]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-dir", default=".", help="directory holding app.json and eas.json")
    parser.add_argument("--platform", required=True, choices=["ios", "android", "all"])
    parser.add_argument("--mode", required=True, choices=["testing", "final"])
    parser.add_argument("--build-profile", default="production")
    parser.add_argument(
        "--submit-profile",
        default=None,
        help="default: production for final; testing (if defined) for testing",
    )
    parser.add_argument(
        "--expo-config",
        default=None,
        help="output of `npx expo config --type public --json`, for app.config.*",
    )
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument(
        "--print-submit-profile",
        action="store_true",
        help="print the submit profile store-release.sh should use, then exit",
    )
    return parser.parse_args(argv)


def print_submit_profile(args: argparse.Namespace) -> int:
    eas_path = Path(args.app_dir) / "eas.json"
    eas = load_json(eas_path) if eas_path.exists() else None
    platforms = ["ios", "android"] if args.platform == "all" else [args.platform]
    names = {p: choose_submit_profile(eas, p, args.mode, args.submit_profile) for p in platforms}
    if None in names.values():
        print(
            "no safe submit profile for Android testing: add submit.testing with "
            'android.track "internal" to eas.json, or pass --submit-profile',
            file=sys.stderr,
        )
        return 2
    if len(set(names.values())) > 1:
        print(
            f"platforms need different submit profiles ({names}); build them separately",
            file=sys.stderr,
        )
        return 2
    print(next(iter(names.values())))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.print_submit_profile:
            return print_submit_profile(args)
        ctx = build_context(args)
        report = run_checks(ctx)
    except ConfigError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"findings": [asdict(f) for f in report.findings], "passed": report.passed},
                indent=2,
            )
        )
    else:
        print(render_text(ctx, report))
    return 1 if report.has_blockers() else 0


if __name__ == "__main__":
    sys.exit(main())
