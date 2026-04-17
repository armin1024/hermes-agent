#!/usr/bin/env bash
set -euo pipefail

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
TARGET_NAME="offline-bundle-v${VERSION}-aops"
if [[ "$(basename "$BASE_BUNDLE")" =~ linux-x86_64 ]]; then
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}-linux-x86_64.tar.gz"
else
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}.tar.gz"
fi

mv "$BUNDLE_DIR" "$WORK_DIR/$TARGET_NAME"
BUNDLE_DIR="$WORK_DIR/$TARGET_NAME"

mkdir -p "$BUNDLE_DIR/overlay" "$BUNDLE_DIR/examples"

RUNTIME_FILES=(
  "agent/prompt_builder.py"
  "cron/scheduler.py"
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
EOF

cp "$SCRIPT_DIR/install_aops_offline.sh" "$BUNDLE_DIR/install.sh"
chmod +x "$BUNDLE_DIR/install.sh"

python3 - <<'PY' "$BUNDLE_DIR" "$SCRIPT_DIR/README_aops_bundle.md"
from pathlib import Path
import sys

bundle_dir = Path(sys.argv[1])
template = Path(sys.argv[2]).read_text(encoding="utf-8")
version = None
for line in bundle_dir.joinpath("overlay.manifest").read_text(encoding="utf-8").splitlines():
    if line == "gateway/platforms/aops.py":
        version = "AOPS overlay"
        break
if version is None:
    version = "offline overlay"
readme = template.replace("__BUNDLE_NAME__", bundle_dir.name)
bundle_dir.joinpath("README.md").write_text(readme, encoding="utf-8")
PY

tar -czf "$OUTPUT_DIR/$OUTPUT_NAME" -C "$WORK_DIR" "$TARGET_NAME"

echo "Created bundle:"
echo "  $OUTPUT_DIR/$OUTPUT_NAME"
