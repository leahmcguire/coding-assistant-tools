#!/usr/bin/env bash
# store-release.sh: build, and optionally submit, an Expo app with EAS.
#
# For a person at a terminal. Coding agents must not run this: it spends EAS build
# minutes, consumes a build number, and uploads to App Store Connect or Google Play with
# your credentials. It refuses to start inside an agent session or without a terminal.
#
# Usage:
#   store-release.sh --platform ios|android|all --mode testing|final
#                    [--app-dir DIR] [--build-profile NAME] [--submit-profile NAME]
#                    [--no-submit] [--message TEXT] [--skip-preflight] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

refuse_agents() {
  if [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_CODE_ENTRYPOINT:-}" || -n "${AI_AGENT:-}" ]]; then
    echo "store-release.sh: refusing to run inside a coding-agent session." >&2
    echo "Run it yourself in a terminal." >&2
    exit 3
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "store-release.sh: needs an interactive terminal (stdin and stdout must be a TTY)." >&2
    exit 3
  fi
}

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

confirm() {
  local prompt="$1" expected="$2" reply
  read -r -p "$prompt" reply
  [[ "$reply" == "$expected" ]]
}

refuse_agents

platform="" mode="" app_dir="." build_profile="production" submit_profile=""
submit=1 skip_preflight=0 dry_run=0 message=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) platform="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    --app-dir) app_dir="${2:-}"; shift 2 ;;
    --build-profile) build_profile="${2:-}"; shift 2 ;;
    --submit-profile) submit_profile="${2:-}"; shift 2 ;;
    --message) message="${2:-}"; shift 2 ;;
    --no-submit) submit=0; shift ;;
    --skip-preflight) skip_preflight=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done

[[ "$platform" =~ ^(ios|android|all)$ ]] || { echo "--platform must be ios, android or all" >&2; exit 2; }
[[ "$mode" =~ ^(testing|final)$ ]] || { echo "--mode must be testing or final" >&2; exit 2; }
[[ -f "$app_dir/eas.json" ]] || { echo "no eas.json in $app_dir" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required for the preflight" >&2; exit 2; }
command -v eas >/dev/null || { echo "eas CLI not found: npm install -g eas-cli" >&2; exit 2; }

cd "$app_dir"

if (( submit )); then
  submit_args=(--app-dir . --platform "$platform" --mode "$mode" --print-submit-profile)
  [[ -n "$submit_profile" ]] && submit_args+=(--submit-profile "$submit_profile")
  submit_profile="$(python3 "$SCRIPT_DIR/preflight.py" "${submit_args[@]}")"
fi

if (( ! skip_preflight )); then
  preflight_args=(--app-dir . --platform "$platform" --mode "$mode" --build-profile "$build_profile")
  (( submit )) && preflight_args+=(--submit-profile "$submit_profile")
  set +e
  python3 "$SCRIPT_DIR/preflight.py" "${preflight_args[@]}"
  status=$?
  set -e
  if (( status == 1 )); then
    echo
    echo "Preflight found blockers. Fix them, or re-run with --skip-preflight if you are sure." >&2
    exit 1
  elif (( status != 0 )); then
    echo "Preflight could not run (exit $status)." >&2
    exit "$status"
  fi
fi

cmd=(eas build --platform "$platform" --profile "$build_profile")
(( submit )) && cmd+=(--auto-submit-with-profile "$submit_profile")
[[ -n "$message" ]] && cmd+=(--message "$message")

echo
echo "About to run (in $(pwd)):"
printf '  %q' "${cmd[@]}"; echo
if (( submit )); then
  echo "Destination: $([[ $mode == final ]] && echo "store submission" || echo "testing") via submit profile '$submit_profile'"
else
  echo "Destination: build only, nothing is uploaded"
fi
(( dry_run )) && { echo "(dry run: nothing executed)"; exit 0; }

if [[ "$mode" == "final" ]]; then
  if [[ -n "$(git status --porcelain -- . 2>/dev/null)" ]]; then
    echo "Warning: uncommitted changes in $(pwd). The build uploads the working tree as-is."
  fi
  confirm "Type 'final' to build and submit for store release: " "final" || { echo "Aborted."; exit 1; }
else
  confirm "Proceed? [y/N] " "y" || { echo "Aborted."; exit 1; }
fi

echo "EAS account: $(eas whoami 2>/dev/null || echo 'not logged in; eas will prompt')"
"${cmd[@]}"

echo
echo "Next steps:"
if (( ! submit )); then
  echo "- Install from the build page link above (internal distribution), or download the artifact."
fi
if (( submit )) && [[ "$platform" != "android" ]]; then
  echo "- iOS: wait for App Store Connect processing (5-20 min). Apple emails if it fails."
  if [[ "$mode" == "final" ]]; then
    echo "- iOS: App Store tab, attach the build to a version, complete the listing, Submit to App Review."
  else
    echo "- iOS: TestFlight tab, assign the build to an internal group (external testers need Beta App Review)."
  fi
fi
if (( submit )) && [[ "$platform" != "ios" ]]; then
  if [[ "$mode" == "final" ]]; then
    echo "- Android: Play Console, Production, review the release and start the rollout."
  else
    echo "- Android: Play Console, Testing, Internal testing; share the opt-in link with testers."
  fi
  echo "- Android: if this is the app's first-ever upload, eas submit fails; upload the AAB by hand once."
fi
