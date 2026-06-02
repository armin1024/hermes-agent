#!/usr/bin/env bash
set -euo pipefail

# Prevent macOS tar/cp from emitting AppleDouble metadata files like `._*`.
export COPYFILE_DISABLE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

BASE_BUNDLE="${1:-}"
OUTPUT_DIR="${2:-$REPO_ROOT/dist}"
HINDSIGHT_WHEEL_DIR="${HINDSIGHT_WHEEL_DIR:-/private/tmp/hindsight-linux-wheel-cache}"

if [[ -z "$BASE_BUNDLE" ]]; then
  echo "Usage: $0 <base-offline-bundle.tar.gz> [output-dir]" >&2
  exit 1
fi

if [[ ! -f "$BASE_BUNDLE" ]]; then
  echo "Base bundle not found: $BASE_BUNDLE" >&2
  exit 1
fi

VERSION="$("$PYTHON_BIN" - <<'PY' "$REPO_ROOT"
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
TARGET_NAME="offline-bundle-v${VERSION}-aops-staging"

if [[ "$(basename "$BUNDLE_DIR")" != "$TARGET_NAME" ]]; then
  mv "$BUNDLE_DIR" "$WORK_DIR/$TARGET_NAME"
  BUNDLE_DIR="$WORK_DIR/$TARGET_NAME"
fi

# Drop macOS metadata noise from the extracted bundle before overlaying files.
find "$WORK_DIR" \( -name '._*' -o -name '__MACOSX' \) -exec rm -rf {} +

mkdir -p "$BUNDLE_DIR/overlay" "$BUNDLE_DIR/examples"

if [[ -d "$HINDSIGHT_WHEEL_DIR" ]] && compgen -G "$HINDSIGHT_WHEEL_DIR/*.whl" >/dev/null; then
  mkdir -p "$BUNDLE_DIR/wheels"
  cp "$HINDSIGHT_WHEEL_DIR"/*.whl "$BUNDLE_DIR/wheels/"
  if [[ -f "$BUNDLE_DIR/requirements.txt" ]] && ! grep -q '^hindsight-client==0.6.1$' "$BUNDLE_DIR/requirements.txt"; then
    printf '\nhindsight-client==0.6.1\n' >> "$BUNDLE_DIR/requirements.txt"
  fi
fi

PACKAGE_DIRS=(
  "acp_adapter"
  "agent"
  "cron"
  "gateway"
  "hermes_cli"
  "plugins"
  "tools"
  "tui_gateway"
)

TOP_LEVEL_MODULES=(
  "batch_runner.py"
  "cli.py"
  "hermes_constants.py"
  "hermes_logging.py"
  "hermes_state.py"
  "hermes_time.py"
  "mcp_serve.py"
  "mini_swe_runner.py"
  "model_tools.py"
  "rl_cli.py"
  "run_agent.py"
  "toolset_distributions.py"
  "toolsets.py"
  "trajectory_compressor.py"
  "utils.py"
)

RUNTIME_FILE_LIST="$WORK_DIR/runtime-files.txt"
printf "" > "$RUNTIME_FILE_LIST"
for rel in "${TOP_LEVEL_MODULES[@]}"; do
  if [[ -f "$REPO_ROOT/$rel" ]]; then
    printf '%s\n' "$rel" >> "$RUNTIME_FILE_LIST"
  fi
done
for dir in "${PACKAGE_DIRS[@]}"; do
  if [[ ! -d "$REPO_ROOT/$dir" ]]; then
    echo "Missing runtime directory: $REPO_ROOT/$dir" >&2
    exit 1
  fi
  find "$REPO_ROOT/$dir" -type f -name '*.py' \
    ! -path '*/__pycache__/*' \
    -print | sed "s#^$REPO_ROOT/##" >> "$RUNTIME_FILE_LIST"
done
sort -u "$RUNTIME_FILE_LIST" -o "$RUNTIME_FILE_LIST"

MANIFEST_PATH="$BUNDLE_DIR/overlay.manifest"
printf "" > "$MANIFEST_PATH"

while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  src="$REPO_ROOT/$rel"
  dst="$BUNDLE_DIR/overlay/$rel"
  if [[ ! -f "$src" ]]; then
    echo "Missing runtime file: $src" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  printf '%s\n' "$rel" >> "$MANIFEST_PATH"
done < "$RUNTIME_FILE_LIST"

"$PYTHON_BIN" "$SCRIPT_DIR/web_dist_overlay.py" "$REPO_ROOT" "$BUNDLE_DIR" "$MANIFEST_PATH"
"$PYTHON_BIN" "$SCRIPT_DIR/verify_overlay_imports.py" "$REPO_ROOT" "$BUNDLE_DIR" "$MANIFEST_PATH"

cat > "$BUNDLE_DIR/examples/config.aops.example.yaml" <<'EOF'
display:
  busy_input_mode: queue

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
      dangerous_commands: ["/skills", "/curator run", "/curator restore"]
      trusted_agent_key_from: ["*"]
      agent_routes:
        main:
          default: true
          workspace: "~/.hermes"
EOF

cat > "$BUNDLE_DIR/examples/aops.env.example" <<'EOF'
AOPS_BOT_TOKEN=replace-me
AOPS_BOT_URL=https://aops.example.com
AOPS_HOME_CHANNEL=user-001
AOPS_PUSH_TOOL_CALLS=true
AOPS_DM_POLICY=open
AOPS_ALLOW_FROM=user-001
AOPS_TRUSTED_AGENT_KEY_FROM=*
AOPS_DANGEROUS_COMMANDS="/skills,/curator run,/curator restore"
EOF

cp "$SCRIPT_DIR/install_aops_offline.sh" "$BUNDLE_DIR/install.sh"
chmod +x "$BUNDLE_DIR/install.sh"
cp "$SCRIPT_DIR/tec01_oneclick_install.sh" "$BUNDLE_DIR/tec01_oneclick_install.sh"
chmod +x "$BUNDLE_DIR/tec01_oneclick_install.sh"

CONTENT_SHA="$(
  cd "$BUNDLE_DIR"
  find . -type f \
    ! -path './README.md' \
    -print0 \
    | sort -z \
    | xargs -0 shasum -a 256 \
    | shasum -a 256 \
    | cut -c1-7
)"
TARGET_NAME="offline-bundle-v${VERSION}-aops-${CONTENT_SHA}"
if [[ "$(basename "$BASE_BUNDLE")" =~ linux-x86_64 ]]; then
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}-${CONTENT_SHA}-linux-x86_64.tar.gz"
else
  OUTPUT_NAME="hermes-aops-offline-bundle-v${VERSION}-${CONTENT_SHA}.tar.gz"
fi
mv "$BUNDLE_DIR" "$WORK_DIR/$TARGET_NAME"
BUNDLE_DIR="$WORK_DIR/$TARGET_NAME"

"$PYTHON_BIN" "$SCRIPT_DIR/render_bundle_readme.py" \
  "$SCRIPT_DIR/README_aops_bundle.md" \
  "$BUNDLE_DIR/README.md" \
  "$OUTPUT_NAME" \
  "$TARGET_NAME" \
  "$VERSION" \
  "$CONTENT_SHA"

# Final sweep so the output tarball cannot contain AppleDouble debris or Python cache files.
find "$BUNDLE_DIR" \( -name '._*' -o -name '__MACOSX' -o -name '__pycache__' \) -exec rm -rf {} +
find "$BUNDLE_DIR" -name '*.pyc' -delete

tar -czf "$OUTPUT_DIR/$OUTPUT_NAME" -C "$WORK_DIR" "$TARGET_NAME"

echo "Created bundle:"
echo "  $OUTPUT_DIR/$OUTPUT_NAME"
