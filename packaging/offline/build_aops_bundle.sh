#!/usr/bin/env bash
set -euo pipefail

# Prevent macOS tar/cp from emitting AppleDouble metadata files like `._*`.
export COPYFILE_DISABLE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

BASE_BUNDLE="${1:-}"
OUTPUT_DIR="${2:-$REPO_ROOT/dist}"

if [[ -z "$BASE_BUNDLE" ]]; then
  echo "Usage: $0 <base-offline-bundle.tar.gz> [output-dir]" >&2
  exit 1
fi

if [[ ! -f "$BASE_BUNDLE" ]]; then
  echo "Base bundle not found: $BASE_BUNDLE" >&2
  exit 1
fi

VERSION="$(python3 - <<'PY' "$REPO_ROOT"
from pathlib import Path
import re
import sys
text = Path(sys.argv[1]).joinpath("pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
if not match:
    raise SystemExit("Could not read project version from pyproject.toml")
print(match.group(1))
PY
)"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

mkdir -p "$OUTPUT_DIR"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-aops-bundle.XXXXXX")"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

TOP_LEVEL="$(tar -tzf "$BASE_BUNDLE" | head -n 1 | cut -d/ -f1)"
if [[ -z "$TOP_LEVEL" ]]; then
  echo "Could not detect top-level directory in base bundle" >&2
  exit 1
fi

tar -xzf "$BASE_BUNDLE" -C "$WORK_DIR"

ARCH_SUFFIX="$(basename "$BASE_BUNDLE")"
ARCH_SUFFIX="${ARCH_SUFFIX#*.tar.gz}"
if [[ "$ARCH_SUFFIX" == "$(basename "$BASE_BUNDLE")" ]]; then
  ARCH_SUFFIX=""
fi

BUNDLE_DIR="$WORK_DIR/$TOP_LEVEL"
TARGET_NAME="offline-bundle-v${VERSION}-aops-${GIT_SHA}"
if [[ "$(basename "$BASE_BUNDLE")" =~ linux-x86_64 ]]; then
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}-${GIT_SHA}-linux-x86_64.tar.gz"
else
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}-${GIT_SHA}.tar.gz"
fi

if [[ "$(basename "$BUNDLE_DIR")" != "$TARGET_NAME" ]]; then
  mv "$BUNDLE_DIR" "$WORK_DIR/$TARGET_NAME"
  BUNDLE_DIR="$WORK_DIR/$TARGET_NAME"
fi

# Drop macOS metadata noise from the extracted bundle before overlaying files.
find "$WORK_DIR" \( -name '._*' -o -name '__MACOSX' \) -exec rm -rf {} +

mkdir -p "$BUNDLE_DIR/overlay" "$BUNDLE_DIR/examples"

RUNTIME_FILES=(
  "agent/prompt_builder.py"
  "cron/__init__.py"
  "cron/jobs.py"
  "cron/scheduler.py"
  "gateway/aops_commands.py"
  "gateway/config.py"
  "gateway/platforms/__init__.py"
  "gateway/platforms/aops.py"
  "gateway/platforms/base.py"
  "gateway/run.py"
  "hermes_cli/setup.py"
  "hermes_cli/gateway.py"
  "hermes_cli/platforms.py"
  "hermes_cli/status.py"
  "hermes_cli/tools_config.py"
  "tools/send_message_tool.py"
  "tools/skills_hub.py"
  "toolsets.py"
)

MANIFEST_PATH="$BUNDLE_DIR/overlay.manifest"
printf "" > "$MANIFEST_PATH"

for rel in "${RUNTIME_FILES[@]}"; do
  src="$REPO_ROOT/$rel"
  dst="$BUNDLE_DIR/overlay/$rel"
  if [[ ! -f "$src" ]]; then
    echo "Missing runtime file: $src" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  printf '%s\n' "$rel" >> "$MANIFEST_PATH"
done

python3 "$SCRIPT_DIR/web_dist_overlay.py" "$REPO_ROOT" "$BUNDLE_DIR" "$MANIFEST_PATH"
python3 "$SCRIPT_DIR/verify_overlay_imports.py" "$REPO_ROOT" "$BUNDLE_DIR" "$MANIFEST_PATH"

cat > "$BUNDLE_DIR/examples/config.aops.example.yaml" <<'EOF'
platforms:
  aops:
    enabled: true
    token: ${AOPS_BOT_TOKEN}
    home_channel:
      platform: aops
      chat_id: user-001
      name: Home
    extra:
      base_url: https://aops.example.com
      push_tool_calls: true
      dm_policy: open
      allow_from: ["user-001"]
      dangerous_commands: ["/curator run", "/curator restore"]
      trusted_agent_key_from: ["*"]
      agent_routes:
        main:
          default: true
          workspace: "~/.hermes"
        devops:
          model: "openrouter/anthropic/claude-sonnet-4"
          provider: "openrouter"
          prompt: "You are the DevOps-focused Hermes route."
          workspace: "~/.hermes/devops"
EOF

cat > "$BUNDLE_DIR/examples/aops.env.example" <<'EOF'
AOPS_BOT_TOKEN=replace-me
AOPS_BASE_URL=https://aops.example.com
AOPS_HOME_CHANNEL=user-001
AOPS_PUSH_TOOL_CALLS=true
AOPS_DM_POLICY=open
AOPS_ALLOW_FROM=user-001
AOPS_TRUSTED_AGENT_KEY_FROM=*
AOPS_DANGEROUS_COMMANDS="/curator run,/curator restore"
CLAWHUB_REGISTRY=http://clawhub.ai
EOF

cp "$SCRIPT_DIR/install_aops_offline.sh" "$BUNDLE_DIR/install.sh"
chmod +x "$BUNDLE_DIR/install.sh"

python3 "$SCRIPT_DIR/render_bundle_readme.py" \
  "$SCRIPT_DIR/README_aops_bundle.md" \
  "$BUNDLE_DIR/README.md" \
  "$OUTPUT_NAME" \
  "$TARGET_NAME" \
  "$VERSION" \
  "$GIT_SHA"

# Final sweep so the output tarball cannot contain AppleDouble debris.
find "$BUNDLE_DIR" \( -name '._*' -o -name '__MACOSX' \) -exec rm -rf {} +

tar -czf "$OUTPUT_DIR/$OUTPUT_NAME" -C "$WORK_DIR" "$TARGET_NAME"

echo "Created bundle:"
echo "  $OUTPUT_DIR/$OUTPUT_NAME"
