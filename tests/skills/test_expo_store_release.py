from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "expo-store-release"


def load_preflight() -> ModuleType:
    spec = importlib.util.spec_from_file_location("preflight", SKILL / "scripts" / "preflight.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["preflight"] = module
    spec.loader.exec_module(module)
    return module


preflight = load_preflight()


def png(width: int, height: int, alpha: bool) -> bytes:
    color_type = 6 if alpha else 2

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body).to_bytes(4, "big")
        return len(body).to_bytes(4, "big") + kind + body + crc

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, color_type, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"") + chunk(b"IEND", b"")


def good_app() -> dict[str, Any]:
    return {
        "expo": {
            "name": "Dinner Circle",
            "slug": "dinner-circle",
            "version": "1.0.0",
            "icon": "./icon.png",
            "plugins": [],
            "ios": {
                "bundleIdentifier": "tech.example",
                "infoPlist": {"ITSAppUsesNonExemptEncryption": False},
            },
            "android": {"package": "tech.example"},
        }
    }


def good_eas() -> dict[str, Any]:
    return {
        "cli": {"appVersionSource": "remote"},
        "build": {
            "base": {"node": "24.0.0"},
            "production": {
                "extends": "base",
                "distribution": "store",
                "autoIncrement": True,
                "env": {"EXPO_PUBLIC_API_URL": "https://api.example.com"},
            },
            "preview": {"extends": "base", "distribution": "internal"},
        },
        "submit": {
            "testing": {
                "ios": {"ascAppId": "1", "appleTeamId": "T"},
                "android": {"track": "internal"},
            },
            "production": {
                "ios": {"ascAppId": "1", "appleTeamId": "T"},
                "android": {"track": "production", "releaseStatus": "draft"},
            },
        },
    }


def make_app(
    tmp_path: Path,
    app: dict[str, Any] | None = None,
    eas: dict[str, Any] | None = None,
    deps: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    alpha_icon: bool = False,
    git: bool = True,
) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.json").write_text(json.dumps(app or good_app()))
    (root / "eas.json").write_text(json.dumps(eas or good_eas()))
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"expo": "~54.0.0", **(deps or {})}})
    )
    (root / "icon.png").write_bytes(png(1024, 1024, alpha_icon))
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / ".gitignore").write_text("*.jks\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def findings(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    platform: str = "all",
    mode: str = "final",
) -> tuple[int, dict[str, str]]:
    code = preflight.main(
        ["--app-dir", str(root), "--platform", platform, "--mode", mode, "--json", *extra]
    )
    data = json.loads(capsys.readouterr().out)
    return code, {f["id"]: f["severity"] for f in data["findings"]}


def test_clean_app_has_no_blockers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, found = findings(make_app(tmp_path), capsys)
    assert code == 0, found
    assert preflight.BLOCKER not in found.values()


def test_missing_purpose_string_blocks_even_testing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_app(tmp_path, deps={"expo-image": "~3.0.0"})
    code, found = findings(root, capsys, platform="ios", mode="testing")
    assert code == 1
    assert found["apple.purpose-string.NSPhotoLibraryUsageDescription"] == preflight.BLOCKER


def test_auto_applied_plugin_gets_generic_default_not_blocker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = good_app()
    app["expo"]["ios"]["infoPlist"]["NSPhotoLibraryUsageDescription"] = "Only for meal photos."
    root = make_app(tmp_path, app=app, deps={"expo-image-picker": "~17.0.0"})
    code, found = findings(root, capsys, platform="ios")
    assert code == 0, found
    assert found["apple.default-purpose.NSCameraUsageDescription"] == preflight.WARNING
    assert "apple.purpose-string.NSCameraUsageDescription" not in found


def test_purpose_string_from_plugin_option_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = good_app()
    app["expo"]["plugins"] = [
        ["expo-speech-recognition", {"microphonePermission": "For hands-free ingredient entry."}]
    ]
    app["expo"]["ios"]["infoPlist"].update(
        NSPhotoLibraryUsageDescription="Only if you add a meal photo.",
        NSCameraUsageDescription="Only if you take a meal photo.",
        NSSpeechRecognitionUsageDescription="To recognise ingredient names you say.",
    )
    root = make_app(
        tmp_path, app=app, deps={"expo-image-picker": "1", "expo-speech-recognition": "1"}
    )
    _, found = findings(root, capsys, platform="ios")
    assert not [k for k in found if "purpose" in k]


def test_listed_plugin_without_option_is_generic_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = good_app()
    app["expo"]["plugins"] = ["expo-camera"]
    root = make_app(tmp_path, app=app, deps={"expo-camera": "1"})
    _, found = findings(root, capsys, platform="ios")
    assert found["apple.default-purpose.NSCameraUsageDescription"] == preflight.WARNING


def test_icon_with_alpha_blocks_ios(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, found = findings(make_app(tmp_path, alpha_icon=True), capsys, platform="ios")
    assert found["apple.icon-alpha"] == preflight.BLOCKER


def test_apk_store_profile_blocks_android(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    eas["build"]["production"]["android"] = {"buildType": "apk"}
    _, found = findings(make_app(tmp_path, eas=eas), capsys, platform="android")
    assert found["google.apk"] == preflight.BLOCKER


def test_android_testing_never_falls_back_to_production(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    del eas["submit"]["testing"]
    root = make_app(tmp_path, eas=eas)
    _, found = findings(root, capsys, platform="android", mode="testing")
    assert found["google.submit-profile"] == preflight.BLOCKER
    assert (
        preflight.main(
            [
                "--app-dir",
                str(root),
                "--platform",
                "ios",
                "--mode",
                "testing",
                "--print-submit-profile",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "production"
    assert (
        preflight.main(
            [
                "--app-dir",
                str(root),
                "--platform",
                "android",
                "--mode",
                "testing",
                "--print-submit-profile",
            ]
        )
        == 2
    )


def test_testing_profile_on_production_track_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    eas["submit"]["testing"]["android"]["track"] = "production"
    _, found = findings(make_app(tmp_path, eas=eas), capsys, platform="android", mode="testing")
    assert found["google.testing-to-production"] == preflight.BLOCKER


def test_internal_distribution_skips_submission_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    eas["submit"] = {}
    root = make_app(tmp_path, eas=eas)
    code, found = findings(root, capsys, "--build-profile", "preview", mode="testing")
    assert code == 0, found
    assert "common.internal-build" in found
    assert "google.submit-profile" not in found


def test_final_from_internal_profile_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, found = findings(make_app(tmp_path), capsys, "--build-profile", "preview")
    assert found["common.internal-distribution"] == preflight.BLOCKER


def test_extends_is_resolved() -> None:
    profile = preflight.resolve_profile(good_eas()["build"], "production")
    assert profile["node"] == "24.0.0"
    assert "extends" not in profile


def test_local_url_blocks_final_but_warns_testing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    eas["build"]["production"]["env"]["EXPO_PUBLIC_API_URL"] = "http://192.168.1.20:8000"
    root = make_app(tmp_path, eas=eas)
    _, final = findings(root, capsys, platform="ios")
    _, testing = findings(root, capsys, platform="ios", mode="testing")
    assert final["common.local-url.EXPO_PUBLIC_API_URL"] == preflight.BLOCKER
    assert testing["common.local-url.EXPO_PUBLIC_API_URL"] == preflight.WARNING


def test_service_role_jwt_in_public_env_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def b64(obj: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    token = (
        f"{b64({'alg': 'HS256'})}.{b64({'role': 'service_role', 'iss': 'supabase'})}.sig123456789"
    )
    eas = good_eas()
    eas["build"]["production"]["env"]["EXPO_PUBLIC_SUPABASE_KEY"] = token
    _, found = findings(make_app(tmp_path, eas=eas), capsys, platform="ios", mode="testing")
    assert any(k.startswith("security.service-role-jwt") for k in found)
    data_env = [k for k in found if k.startswith("security.service-role-jwt")]
    assert found[data_env[0]] == preflight.BLOCKER


def test_secret_name_in_public_env_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eas = good_eas()
    eas["build"]["production"]["env"]["EXPO_PUBLIC_STRIPE_SECRET"] = "whatever"
    _, found = findings(make_app(tmp_path, eas=eas), capsys, platform="ios", mode="testing")
    assert found["security.secret-name.EXPO_PUBLIC_STRIPE_SECRET"] == preflight.BLOCKER


def test_keystore_tracked_blocks_ignored_is_info(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_app(tmp_path, files={"upload.keystore": "x", "ignored.jks": "x"})
    _, found = findings(root, capsys, platform="android", mode="testing")
    assert found["security.sensitive-file.upload.keystore"] == preflight.BLOCKER
    assert found["security.sensitive-file.ignored.jks"] == preflight.INFO


def test_restricted_android_permission(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = good_app()
    app["expo"]["android"]["permissions"] = ["android.permission.READ_SMS", "CAMERA"]
    _, found = findings(make_app(tmp_path, app=app), capsys, platform="android")
    assert found["google.restricted-permission.READ_SMS"] == preflight.BLOCKER
    assert "google.restricted-permission.CAMERA" not in found


def test_old_expo_sdk_below_target_api(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_app(tmp_path)
    pkg = json.loads((root / "package.json").read_text())
    pkg["dependencies"]["expo"] = "~51.0.0"
    (root / "package.json").write_text(json.dumps(pkg))
    _, found = findings(root, capsys, platform="android")
    assert found["google.target-sdk"] == preflight.BLOCKER


def test_account_creation_without_deletion_blocks_final_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_app(tmp_path, files={"app/sign-up.tsx": "export function signUp() {}"})
    _, final = findings(root, capsys, platform="ios")
    _, testing = findings(root, capsys, platform="ios", mode="testing")
    assert final["common.account-deletion"] == preflight.BLOCKER
    assert testing["common.account-deletion"] == preflight.INFO


def test_source_security_patterns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = (
        "AsyncStorage.setItem('auth_token', t);\n"
        "console.log('refresh_token', refresh_token);\n"
        "<WebView originWhitelist={['*']} />\n"
        "fetch('http://api.example.com/v1');\n"
    )
    root = make_app(
        tmp_path, deps={"react-native-webview": "13"}, files={"lib/session.tsx": source}
    )
    _, found = findings(root, capsys, platform="ios", mode="testing")
    prefixes = {k.split(".lib/")[0] for k in found}
    assert {
        "security.asyncstorage-token",
        "security.log-secret",
        "security.webview-origin",
        "security.http-url",
    } <= prefixes


def test_dynamic_config_requires_resolved_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_app(tmp_path)
    (root / "app.config.ts").write_text("export default {}")
    assert preflight.main(["--app-dir", str(root), "--platform", "ios", "--mode", "final"]) == 2
    assert "npx expo config" in capsys.readouterr().err

    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps(good_app()["expo"]))
    code, _ = findings(root, capsys, "--expo-config", str(resolved), platform="ios")
    assert code == 0


def test_release_script_refuses_agents_before_doing_anything() -> None:
    """Static check only: the release script is never executed by tests."""
    text = (SKILL / "scripts" / "store-release.sh").read_text()
    guard_call = text.index("\nrefuse_agents\n")
    assert guard_call < text.index("while [[ $# -gt 0 ]]")
    assert guard_call < text.index('"${cmd[@]}"')
    for marker in ("CLAUDECODE", "AI_AGENT", "-t 0", "-t 1"):
        assert marker in text


def test_install_script_links_excludes_and_merges_deny(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    settings = target / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    script = str(REPO / "install-skill.sh")

    for _ in range(2):  # idempotent
        subprocess.run([script, "expo-store-release", str(target)], check=True, capture_output=True)
    link = target / ".claude" / "skills" / "expo-store-release"
    assert link.is_symlink() and link.resolve() == SKILL.resolve()
    exclude = (target / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.claude/skills/expo-store-release") == 1
    deny = json.loads(settings.read_text())["permissions"]["deny"]
    expected = json.loads((SKILL / "install.json").read_text())["deny"]
    assert deny == expected
    assert json.loads(settings.read_text())["permissions"]["allow"] == ["Bash(ls)"]

    subprocess.run(
        [script, "--uninstall", "expo-store-release", str(target)], check=True, capture_output=True
    )
    assert not link.exists()
    assert "deny" not in json.loads(settings.read_text())["permissions"]
    assert (
        "/.claude/skills/expo-store-release"
        not in (target / ".git" / "info" / "exclude").read_text()
    )
