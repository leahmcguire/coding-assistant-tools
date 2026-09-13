#!/usr/bin/env bash
# install-skill.sh: link a skill from this repo into another project's .claude/skills.
#
# Usage:
#   ./install-skill.sh <skill-name> <target-repo>
#   ./install-skill.sh --uninstall <skill-name> <target-repo>
#
# The skill is symlinked, so edits here take effect in the target immediately. The link is
# added to the target's .git/info/exclude (local-only, nothing committed). If the skill ships
# an install.json with a "deny" list, those permission rules are merged into the target's
# .claude/settings.json, and removed again on uninstall.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

uninstall=0
if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall=1
  shift
fi
[[ $# -eq 2 ]] || { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }

name="$1"
target="$(cd "$2" && pwd)"
source_dir="$REPO_DIR/skills/$name"
link="$target/.claude/skills/$name"
exclude_entry="/.claude/skills/$name"

[[ -f "$source_dir/SKILL.md" ]] || { echo "no skill at $source_dir" >&2; exit 2; }

exclude_file=""
if git -C "$target" rev-parse --git-dir >/dev/null 2>&1; then
  exclude_file="$(cd "$target" && git rev-parse --path-format=absolute --git-path info/exclude)"
fi

# Adds (mode=add) or removes (mode=remove) install.json's deny rules in settings.json.
merge_deny_rules() {
  local mode="$1"
  [[ -f "$source_dir/install.json" ]] || return 0
  python3 - "$mode" "$source_dir/install.json" "$target/.claude/settings.json" <<'PY'
import json
import sys
from pathlib import Path

mode, manifest_path, settings_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
rules = json.loads(manifest_path.read_text()).get("deny", [])
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
if not rules or (mode == "remove" and not settings_path.exists()):
    sys.exit(0)
permissions = settings.setdefault("permissions", {})
deny = permissions.setdefault("deny", [])
if mode == "add":
    added = [rule for rule in rules if rule not in deny]
    deny.extend(added)
    print(f"deny rules: added {len(added)}, {len(rules) - len(added)} already present")
else:
    permissions["deny"] = [rule for rule in deny if rule not in rules]
    print(f"deny rules: removed {len(deny) - len(permissions['deny'])}")
    if not permissions["deny"]:
        del permissions["deny"]
settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
PY
}

if (( uninstall )); then
  if [[ -L "$link" ]]; then
    rm "$link"
    echo "removed link $link"
  fi
  if [[ -n "$exclude_file" && -f "$exclude_file" ]]; then
    grep -vxF "$exclude_entry" "$exclude_file" > "$exclude_file.tmp" || true
    mv "$exclude_file.tmp" "$exclude_file"
  fi
  merge_deny_rules remove
  exit 0
fi

if [[ -e "$link" && ! -L "$link" ]]; then
  echo "$link exists and is not a symlink; refusing to replace it" >&2
  exit 1
fi
mkdir -p "$target/.claude/skills"
ln -sfn "$source_dir" "$link"
echo "linked $link -> $source_dir"

if [[ -n "$exclude_file" ]]; then
  mkdir -p "$(dirname "$exclude_file")"
  touch "$exclude_file"
  if ! grep -qxF "$exclude_entry" "$exclude_file"; then
    echo "$exclude_entry" >> "$exclude_file"
    echo "git exclude: added $exclude_entry"
  fi
else
  echo "note: $target is not a git repository; nothing to exclude"
fi

merge_deny_rules add
