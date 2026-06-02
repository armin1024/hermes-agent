#!/usr/bin/env bash
# tec01-managed Hermes one-click installer/upgrader.
#
# Expected usage from tec01:
#   curl -fsSL "http://tec01/hermes/install-oneclick.sh?taskId=xxx" | sudo bash
#
# The script fetches a task JSON, creates the target user when missing,
# downloads and verifies the offline bundle, then installs or upgrades Hermes
# under that user's HOME. Gateway lifecycle is delegated to Hermes CLI.

set -euo pipefail
umask 077

TEC01_TASK_URL="${TEC01_TASK_URL:-}"
TEC01_REPORT_URL="${TEC01_REPORT_URL:-}"
TASK_ID="${TASK_ID:-}"
WORK_DIR="${WORK_DIR:-}"

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
report_result() {
  local status="$1"
  local stage="$2"
  local message="$3"
  [[ -n "$TEC01_REPORT_URL" ]] || return 0
  python3 - "$TEC01_REPORT_URL" "$TASK_ID" "$status" "$stage" "$message" <<'PY' || true
import json
import sys
import urllib.request

url, task_id, status, stage, message = sys.argv[1:6]
payload = json.dumps({
    "taskId": task_id,
    "status": status,
    "stage": stage,
    "message": message,
}, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=10).read()
PY
}

fail() { printf '[ERROR] %s\n' "$*" >&2; report_result "failed" "${STAGE:-unknown}" "$*"; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  sudo TEC01_TASK_URL=http://tec01/hermes/tasks/<id> bash tec01_oneclick_install.sh
  sudo bash tec01_oneclick_install.sh --task-url http://tec01/hermes/tasks/<id>

Options:
  --task-url URL     tec01 task JSON endpoint
  --report-url URL   optional task result report endpoint
  --task-id ID       optional task id, used only for reporting when not in JSON
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-url)
      TEC01_TASK_URL="$2"
      shift 2
      ;;
    --report-url)
      TEC01_REPORT_URL="$2"
      shift 2
      ;;
    --task-id)
      TASK_ID="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

json_get() {
  local expr="$1"
  python3 - "$TASK_JSON" "$expr" <<'PY'
import json
import sys

path, expr = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
cur = data
for part in expr.split("."):
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break
if cur is None:
    print("")
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, ensure_ascii=False))
else:
    print(str(cur))
PY
}

json_bool() {
  local expr="$1"
  local default="$2"
  local value
  value="$(json_get "$expr")"
  case "${value:-$default}" in
    true|True|1|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

validate_username() {
  local user="$1"
  [[ "$user" != "root" ]] || fail "targetUser=root is not allowed"
  [[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "Invalid targetUser: $user"
}

download() {
  local url="$1"
  local dst="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL "$url" -o "$dst"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$dst" "$url"
  else
    fail "curl or wget is required"
  fi
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

ensure_target_private_dir() {
  local dir="$1"
  mkdir -p "$dir"
  chmod 700 "$dir"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$dir"
  fi
}

run_as_target() {
  local script="$1"
  if [[ "$(id -u)" -eq 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo -H -u "$TARGET_USER" bash -lc "$script"
    elif command -v runuser >/dev/null 2>&1; then
      runuser -u "$TARGET_USER" -- bash -lc "$script"
    else
      su - "$TARGET_USER" -s /bin/bash -c "$script"
    fi
  else
    [[ "$(id -un)" == "$TARGET_USER" ]] || fail "Run with sudo to install as $TARGET_USER"
    bash -lc "$script"
  fi
}

enable_linger_if_possible() {
  if [[ "$(id -u)" -ne 0 ]]; then
    return 0
  fi
  command -v loginctl >/dev/null 2>&1 || {
    warn "loginctl not found; gateway user service may stop after logout"
    return 0
  }
  log "Enabling systemd linger for $TARGET_USER"
  if loginctl enable-linger "$TARGET_USER"; then
    return 0
  fi
  warn "loginctl enable-linger $TARGET_USER failed; gateway service may not persist after logout"
}

ensure_gateway_service_installed() {
  local action="$1"
  if json_bool options.installGatewayService true; then
    STAGE="gateway_install_service"
    log "Ensuring Hermes gateway service is installed for $TARGET_USER"
    run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes gateway install --force"
  fi
  STAGE="gateway_${action}"
  run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes gateway $action"
}

[[ -n "$TEC01_TASK_URL" ]] || fail "--task-url or TEC01_TASK_URL is required"
need_cmd python3
need_cmd tar

BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-tec01-bootstrap.XXXXXX")"
chmod 700 "$BOOTSTRAP_DIR"
TASK_JSON="$BOOTSTRAP_DIR/task.json"

STAGE="fetch_task"
log "Fetching tec01 task: $TEC01_TASK_URL"
download "$TEC01_TASK_URL" "$TASK_JSON"

TASK_ID="$(json_get taskId || true)"
TARGET_USER="$(json_get targetUser)"
BUNDLE_URL="$(json_get bundle.url)"
BUNDLE_SHA="$(json_get bundle.sha256)"
[[ -n "$TARGET_USER" ]] || fail "task.targetUser is required"
[[ -n "$BUNDLE_URL" ]] || fail "task.bundle.url is required"
[[ -n "$BUNDLE_SHA" ]] || fail "task.bundle.sha256 is required"
validate_username "$TARGET_USER"

STAGE="ensure_user"
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  [[ "$(id -u)" -eq 0 ]] || fail "target user does not exist; run with sudo to create it"
  log "Creating user: $TARGET_USER"
  useradd -m -s /bin/bash "$TARGET_USER"
fi

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || fail "Could not resolve HOME for $TARGET_USER"
INSTALL_DIR="$TARGET_HOME/hermes-agent"
ensure_target_private_dir "$TARGET_HOME/.hermes"
ensure_target_private_dir "$TARGET_HOME/.hermes/tec01-install"
enable_linger_if_possible

if [[ -z "$WORK_DIR" ]]; then
  safe_task_id="${TASK_ID:-manual}"
  safe_task_id="${safe_task_id//[^A-Za-z0-9_.-]/_}"
  WORK_DIR="$TARGET_HOME/.hermes/tec01-install/$safe_task_id"
fi
rm -rf "$WORK_DIR"
ensure_target_private_dir "$WORK_DIR"

TASK_JSON_IN_HOME="$WORK_DIR/task.json"
cp "$TASK_JSON" "$TASK_JSON_IN_HOME"
chmod 600 "$TASK_JSON_IN_HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$TASK_JSON_IN_HOME"
fi
TASK_JSON="$TASK_JSON_IN_HOME"

STAGE="download_bundle"
BUNDLE_TGZ="$WORK_DIR/hermes-offline-bundle.tar.gz"
log "Downloading bundle"
download "$BUNDLE_URL" "$BUNDLE_TGZ"
chmod 600 "$BUNDLE_TGZ"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$BUNDLE_TGZ"
fi

STAGE="verify_bundle"
actual_sha="$(sha256_file "$BUNDLE_TGZ")"
[[ "$actual_sha" == "$BUNDLE_SHA" ]] || fail "sha256 mismatch: expected $BUNDLE_SHA got $actual_sha"

STAGE="extract_bundle"
ensure_target_private_dir "$WORK_DIR/extracted"
run_as_target "tar -xzf '$BUNDLE_TGZ' -C '$WORK_DIR/extracted'"
BUNDLE_DIR="$(find "$WORK_DIR/extracted" -maxdepth 1 -type d -name 'offline-bundle-*' | head -n 1)"
[[ -n "$BUNDLE_DIR" && -f "$BUNDLE_DIR/install.sh" ]] || fail "bundle install.sh not found"
chmod +x "$BUNDLE_DIR/install.sh"

STAGE="detect_mode"
if [[ -x "$INSTALL_DIR/hermes" || -d "$TARGET_HOME/.hermes" ]]; then
  MODE="upgrade"
else
  MODE="install"
fi
log "Install mode for $TARGET_USER: $MODE"

PAYLOAD_IN_HOME="$WORK_DIR/tec01-task.json"
python3 - "$TASK_JSON" "$PAYLOAD_IN_HOME" "$MODE" <<'PY'
import json
import sys

src, dst, mode = sys.argv[1:4]
with open(src, "r", encoding="utf-8") as f:
    data = json.load(f)
options = data.setdefault("options", {})
if not isinstance(options, dict):
    options = {}
    data["options"] = options
options["upgrade"] = mode == "upgrade"
with open(dst, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
chmod 600 "$PAYLOAD_IN_HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$PAYLOAD_IN_HOME"
fi

STAGE="install_or_upgrade"
if [[ "$MODE" == "upgrade" ]]; then
  run_as_target "cd '$BUNDLE_DIR' && bash install.sh '$INSTALL_DIR' --link --upgrade --preserve-config --config-payload '$PAYLOAD_IN_HOME'"
  if json_bool options.restartGatewayAfterUpgrade true; then
    ensure_gateway_service_installed "restart"
  fi
else
  run_as_target "cd '$BUNDLE_DIR' && bash install.sh '$INSTALL_DIR' --link --init-config --config-payload '$PAYLOAD_IN_HOME'"
  if json_bool options.startGatewayAfterInstall true; then
    ensure_gateway_service_installed "start"
  fi
fi

STAGE="complete"
report_result "success" "$STAGE" "Hermes $MODE completed for $TARGET_USER"
log "Hermes $MODE completed for $TARGET_USER"
