#!/usr/bin/env bash
# Update every installed Hermes profile on a host with one installer call per user.
#
# This wrapper intentionally delegates all installation and gateway lifecycle
# work to the existing Tec01 install-oneclick.sh. It does not edit Hermes
# configuration or source files itself.

set -euo pipefail
umask 077

INSTALLER_URL="${HERMES_INSTALLER_URL:-http://tec01/hermes/install-oneclick.sh}"
TEMPLATE_URL="${HERMES_TEMPLATE_URL:-}"
GLOBAL_AOPS_URL="${AOPS_BOT_URL:-}"
BUNDLE_URL_OVERRIDE="${HERMES_BUNDLE_URL:-}"
BUNDLE_SHA_OVERRIDE="${HERMES_BUNDLE_SHA256:-}"
TIMEOUT_SECONDS="${HERMES_MULTIUSER_TIMEOUT:-900}"
JOBS="${HERMES_MULTIUSER_JOBS:-1}"
HERMES_DATA_ROOT="${HERMES_DATA_ROOT:-/data/hermes-users}"
BUNDLE_CACHE_DIR="${HERMES_BUNDLE_CACHE_DIR:-/data/hermes-tec01/cache/bundles}"
RUNTIME_LAYOUT="${HERMES_RUNTIME_LAYOUT:-shared}"
SHARED_RUNTIME_ROOT="${HERMES_SHARED_RUNTIME_ROOT:-/data/hermes-tec01/runtime}"
LOG_DIR="${HERMES_MULTIUSER_LOG_DIR:-/data/hermes-tec01/logs/multiuser}"
LOCK_FILE="${HERMES_MULTIUSER_LOCK_FILE:-/var/lock/hermes-tec01-multiuser-update.lock}"
DRY_RUN=false
REQUESTED_USERS=()
EXTRA_SET_ARGS=()

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR=""
LOCK_FD_OPEN=false
LOCK_DIR=""
SCANNED_USERS=0
ELIGIBLE_USERS=0
ELIGIBLE_PROFILES=0
RUN_START_MS=""
RUN_STARTED_AT=""
CACHED_INSTALLER=""
CACHED_TEMPLATE=""
PROFILE_PROGRESS_INDEX=0
PROFILE_PROGRESS_TOTAL=0

usage() {
  cat <<'EOF'
Usage:
  sudo bash tec01_multiuser_update.sh [options]

Options:
  --installer-url URL   Existing Tec01 install-oneclick.sh URL.
  --template-url URL    Template YAML/JSON URL passed to the installer.
  --aops-url URL        Fallback AOPS_BOT_URL when a user's .env has none.
  --bundle-url URL      Override bundle.url for this update.
  --bundle-sha256 SHA   Override bundle.sha256 for this update.
  --set KEY=VALUE       Pass an explicit installer override; repeatable.
  --user USER           Update only this user; repeatable.
  --dry-run             Scan and report eligible users without updating.
  --timeout SECONDS     Per-user synchronized update timeout (default: 900).
  --jobs N              Concurrent system users, 1..8 (default: 1).
  --hermes-data-root DIR
                        Physical Hermes user-data root (default: /data/hermes-users).
  --bundle-cache-dir DIR
                        Shared verified bundle cache.
  --runtime-layout per-user|shared
                        Runtime layout passed to the installer (default: shared).
  --shared-runtime-root DIR
                        Root-owned shared release store
                        (default: /data/hermes-tec01/runtime).
  --log-dir DIR         Root-only log directory.
  --help                Show this help.

Environment equivalents:
  HERMES_INSTALLER_URL, HERMES_TEMPLATE_URL, HERMES_BUNDLE_URL,
  HERMES_BUNDLE_SHA256, HERMES_BUNDLE_CACHE_DIR, AOPS_BOT_URL,
  HERMES_RUNTIME_LAYOUT, HERMES_SHARED_RUNTIME_ROOT,
  HERMES_DATA_ROOT, HERMES_MULTIUSER_TIMEOUT, HERMES_MULTIUSER_JOBS,
  HERMES_MULTIUSER_LOG_DIR, HERMES_MULTIUSER_LOCK_FILE
EOF
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

now_ms() {
  python3 - <<'PY'
import time
print(time.monotonic_ns() // 1_000_000)
PY
}

iso_now() {
  python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
PY
}

validate_batch_data_mount() {
  local existing_path="$HERMES_DATA_ROOT"
  local mount_info mount_target mount_type mount_options

  while [[ ! -e "$existing_path" && "$existing_path" != / ]]; do
    existing_path="$(dirname "$existing_path")"
  done
  mount_info="$(findmnt -n -o TARGET,FSTYPE,OPTIONS -T "$existing_path" 2>/dev/null || true)"
  [[ -n "$mount_info" ]] || fail "Cannot resolve filesystem for Hermes data root: $HERMES_DATA_ROOT"
  read -r mount_target mount_type mount_options <<< "$mount_info"
  [[ "$mount_target" != / ]] || fail "Hermes data root must be on a filesystem separate from /: $HERMES_DATA_ROOT"
  case "$mount_type" in
    nfs|nfs4|cifs|smb3|fuse|fuse.*|overlay)
      fail "Hermes data root must use a local filesystem, got $mount_type at $mount_target"
      ;;
  esac
  case ",$mount_options," in
    *,rw,*) ;;
    *) fail "Hermes data filesystem is not writable: $mount_target" ;;
  esac
  case ",$mount_options," in
    *,noexec,*) fail "Hermes data filesystem must allow executable venv files: $mount_target" ;;
  esac
}

prefetch_inputs() {
  local started_ms
  [[ "$DRY_RUN" == false ]] || return 0
  started_ms="$(now_ms)"
  CACHED_INSTALLER="$RUN_DIR/install-oneclick.sh"
  printf '[INFO] Downloading installer once: %s\n' "$INSTALLER_URL"
  curl --fail --silent --show-error --location --connect-timeout 15 --max-time 120 \
    "$INSTALLER_URL" -o "$CACHED_INSTALLER"
  chmod 700 "$CACHED_INSTALLER"
  bash -n "$CACHED_INSTALLER" || fail "Downloaded installer failed bash syntax validation"

  if [[ -n "$TEMPLATE_URL" ]]; then
    CACHED_TEMPLATE="$RUN_DIR/template.yaml"
    printf '[INFO] Downloading template once: %s\n' "$TEMPLATE_URL"
    curl --fail --silent --show-error --location --connect-timeout 15 --max-time 120 \
      "$TEMPLATE_URL" -o "$CACHED_TEMPLATE"
    chmod 600 "$CACHED_TEMPLATE"
  fi

  mkdir -p "$BUNDLE_CACHE_DIR"
  chmod 700 "$BUNDLE_CACHE_DIR"
  printf '[TIMING] stage=prefetch_inputs durationMs=%s\n' "$(($(now_ms) - started_ms))"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --installer-url)
      [[ $# -ge 2 ]] || fail "--installer-url requires a value"
      INSTALLER_URL="$2"
      shift 2
      ;;
    --template-url)
      [[ $# -ge 2 ]] || fail "--template-url requires a value"
      TEMPLATE_URL="$2"
      shift 2
      ;;
    --aops-url)
      [[ $# -ge 2 ]] || fail "--aops-url requires a value"
      GLOBAL_AOPS_URL="$2"
      shift 2
      ;;
    --bundle-url)
      [[ $# -ge 2 ]] || fail "--bundle-url requires a value"
      BUNDLE_URL_OVERRIDE="$2"
      shift 2
      ;;
    --bundle-sha256)
      [[ $# -ge 2 ]] || fail "--bundle-sha256 requires a value"
      BUNDLE_SHA_OVERRIDE="$2"
      shift 2
      ;;
    --set)
      [[ $# -ge 2 ]] || fail "--set requires KEY=VALUE"
      [[ "$2" == *=* ]] || fail "--set must use KEY=VALUE"
      override_key="${2%%=*}"
      case "$override_key" in
        targetUser|env.AOPS_BOT_TOKEN)
          fail "--set $override_key is managed by the multi-user wrapper"
          ;;
      esac
      EXTRA_SET_ARGS+=("$2")
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || fail "--user requires a value"
      REQUESTED_USERS+=("$2")
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --timeout)
      [[ $# -ge 2 ]] || fail "--timeout requires a value"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --jobs)
      [[ $# -ge 2 ]] || fail "--jobs requires a value"
      JOBS="$2"
      shift 2
      ;;
    --hermes-data-root)
      [[ $# -ge 2 ]] || fail "--hermes-data-root requires a value"
      HERMES_DATA_ROOT="$2"
      shift 2
      ;;
    --bundle-cache-dir)
      [[ $# -ge 2 ]] || fail "--bundle-cache-dir requires a value"
      BUNDLE_CACHE_DIR="$2"
      shift 2
      ;;
    --runtime-layout)
      [[ $# -ge 2 ]] || fail "--runtime-layout requires per-user or shared"
      RUNTIME_LAYOUT="$2"
      shift 2
      ;;
    --shared-runtime-root)
      [[ $# -ge 2 ]] || fail "--shared-runtime-root requires an absolute path"
      SHARED_RUNTIME_ROOT="$2"
      shift 2
      ;;
    --log-dir)
      [[ $# -ge 2 ]] || fail "--log-dir requires a value"
      LOG_DIR="$2"
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

[[ "$(id -u)" -eq 0 ]] || fail "Run this script as root (for example: sudo bash $0)"
command -v getent >/dev/null 2>&1 || fail "Required command not found: getent"
command -v curl >/dev/null 2>&1 || fail "Required command not found: curl"
command -v timeout >/dev/null 2>&1 || fail "Required command not found: timeout"
command -v python3 >/dev/null 2>&1 || fail "Required command not found: python3"
command -v findmnt >/dev/null 2>&1 || fail "Required command not found: findmnt"
command -v stat >/dev/null 2>&1 || fail "Required command not found: stat"
command -v realpath >/dev/null 2>&1 || fail "Required command not found: realpath"

case "$INSTALLER_URL" in
  http://*|https://*) ;;
  *) fail "Installer URL must use http:// or https://" ;;
esac

if [[ -n "$TEMPLATE_URL" ]]; then
  case "$TEMPLATE_URL" in
    http://*|https://*) ;;
    *) fail "Template URL must use http:// or https://" ;;
  esac
fi
if [[ -n "$BUNDLE_URL_OVERRIDE" ]]; then
  case "$BUNDLE_URL_OVERRIDE" in
    http://*|https://*) ;;
    *) fail "Bundle URL must use http:// or https://" ;;
  esac
fi
if [[ -n "$BUNDLE_SHA_OVERRIDE" && ! "$BUNDLE_SHA_OVERRIDE" =~ ^[0-9a-fA-F]{64}$ ]]; then
  fail "--bundle-sha256 must be a 64-character hexadecimal SHA256"
fi

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "--timeout must be a positive integer"
[[ "$JOBS" =~ ^[1-8]$ ]] || fail "--jobs must be an integer from 1 to 8"
[[ -n "$HERMES_DATA_ROOT" && "$HERMES_DATA_ROOT" == /* ]] || fail "--hermes-data-root must be an absolute path"
[[ "$HERMES_DATA_ROOT" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "--hermes-data-root contains unsupported characters"
HERMES_DATA_ROOT="$(realpath -m "$HERMES_DATA_ROOT")"
[[ "$HERMES_DATA_ROOT" != / ]] || fail "--hermes-data-root cannot be /"
[[ -n "$BUNDLE_CACHE_DIR" && "$BUNDLE_CACHE_DIR" == /* ]] || fail "--bundle-cache-dir must be an absolute path"
case "$RUNTIME_LAYOUT" in
  per-user|shared) ;;
  *) fail "--runtime-layout must be per-user or shared" ;;
esac
[[ "$SHARED_RUNTIME_ROOT" == /* && "$SHARED_RUNTIME_ROOT" != / ]] || fail "--shared-runtime-root must be an absolute non-root path"

validate_batch_data_mount

mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
RUN_DIR="$LOG_DIR/run-$RUN_ID"
mkdir "$RUN_DIR"
chmod 700 "$RUN_DIR"

UPDATED_FILE="$RUN_DIR/updated.tsv"
SKIPPED_FILE="$RUN_DIR/skipped.tsv"
FAILED_FILE="$RUN_DIR/failed.tsv"
USER_RESULTS_FILE="$RUN_DIR/users.tsv"
STORAGE_RESULTS_FILE="$RUN_DIR/storage.tsv"
RUNTIME_RESULTS_FILE="$RUN_DIR/runtime.tsv"
MASTER_UPDATED_FILE="$UPDATED_FILE"
MASTER_SKIPPED_FILE="$SKIPPED_FILE"
MASTER_FAILED_FILE="$FAILED_FILE"
MASTER_USER_RESULTS_FILE="$USER_RESULTS_FILE"
MASTER_STORAGE_RESULTS_FILE="$STORAGE_RESULTS_FILE"
MASTER_RUNTIME_RESULTS_FILE="$RUNTIME_RESULTS_FILE"
: > "$UPDATED_FILE"
: > "$SKIPPED_FILE"
: > "$FAILED_FILE"
: > "$USER_RESULTS_FILE"
: > "$STORAGE_RESULTS_FILE"
: > "$RUNTIME_RESULTS_FILE"
EXTRA_SET_FILE="$RUN_DIR/extra-set-args"
: > "$EXTRA_SET_FILE"
if ((${#EXTRA_SET_ARGS[@]} > 0)); then
  printf '%s\n' "${EXTRA_SET_ARGS[@]}" > "$EXTRA_SET_FILE"
fi
if [[ -n "$BUNDLE_URL_OVERRIDE" ]]; then
  printf 'bundle.url=%s\n' "$BUNDLE_URL_OVERRIDE" >> "$EXTRA_SET_FILE"
fi
if [[ -n "$BUNDLE_SHA_OVERRIDE" ]]; then
  printf 'bundle.sha256=%s\n' "$BUNDLE_SHA_OVERRIDE" >> "$EXTRA_SET_FILE"
fi
chmod 600 "$EXTRA_SET_FILE"

cleanup() {
  if [[ "$LOCK_FD_OPEN" == true ]]; then
    flock -u 9 2>/dev/null || true
    eval 'exec 9>&-' 2>/dev/null || true
  fi
  if [[ -n "$LOCK_DIR" ]]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT

acquire_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")"
  if command -v flock >/dev/null 2>&1; then
    # shellcheck disable=SC3045
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
      fail "Another multi-user update is already running: $LOCK_FILE"
    fi
    LOCK_FD_OPEN=true
    return
  fi

  LOCK_DIR="${LOCK_FILE}.d"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "Another multi-user update is already running: $LOCK_DIR"
  fi
}

record_skip() {
  local user="$1"
  local reason="$2"
  local profile="${3:-}"
  local duration_ms="${4:-0}"
  printf '%s\t%s\t%s\t%s\n' "$user" "$profile" "$reason" "$duration_ms" >> "$SKIPPED_FILE"
  printf 'user=%s profile=%s status=skipped reason=%s durationMs=%s\n' "$user" "${profile:-default}" "$reason" "$duration_ms"
}

record_failure() {
  local user="$1"
  local reason="$2"
  local code="$3"
  local profile="${4:-}"
  local duration_ms="${5:-0}"
  local logfile="${6:-}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$user" "$profile" "$reason" "$code" "$duration_ms" "$logfile" >> "$FAILED_FILE"
  printf 'user=%s profile=%s status=failed reason=%s exitCode=%s durationMs=%s\n' "$user" "${profile:-default}" "$reason" "$code" "$duration_ms" >&2
}

read_dotenv_value() {
  local env_path="$1"
  local wanted_key="$2"
  python3 - "$env_path" "$wanted_key" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
wanted = sys.argv[2]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError:
    raise SystemExit(0)

value = ""
for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        continue
    key, candidate = line.split("=", 1)
    if key.strip() != wanted:
        continue
    candidate = candidate.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"'}:
        candidate = candidate[1:-1]
    value = candidate
    break
print(value)
PY
}

valid_username() {
  [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
}

user_record() {
  local user="$1"
  getent passwd "$user" 2>/dev/null | head -n 1 || true
}

path_on_root_filesystem() {
  local path="$1"
  [[ -e "$path" ]] || return 1
  [[ "$(stat -Lc %d "$path" 2>/dev/null || true)" == "$(stat -Lc %d /)" ]]
}

classify_user_storage() {
  local user="$1"
  local passwd_record home runtime hermes_home expected_runtime expected_home
  local runtime_real home_real

  passwd_record="$(user_record "$user")"
  if [[ -z "$passwd_record" ]]; then
    printf 'unavailable\n'
    return
  fi
  IFS=: read -r _ _ _ _ _ home _ <<< "$passwd_record"
  runtime="$home/hermes-agent"
  hermes_home="$home/.hermes"
  expected_runtime="$HERMES_DATA_ROOT/$user/hermes-agent"
  expected_home="$HERMES_DATA_ROOT/$user/.hermes"

  if [[ ! -x "$runtime/venv/bin/python" ]]; then
    printf 'unavailable\n'
    return
  fi

  runtime_real="$(readlink -f "$runtime" 2>/dev/null || true)"
  home_real="$(readlink -f "$hermes_home" 2>/dev/null || true)"
  if [[ "$runtime_real" == "$expected_runtime" && "$home_real" == "$expected_home" ]]; then
    printf 'already-mapped\n'
    return
  fi

  # Any physical Hermes tree still backed by the root filesystem requires a
  # serialized cutover. The one-click installer owns the actual migration and
  # performs all safety checks and rollback.
  if path_on_root_filesystem "$runtime" || path_on_root_filesystem "$hermes_home"; then
    printf 'migration-needed\n'
    return
  fi

  # Unknown symlinks and installations already located on another independent
  # filesystem are left to the one-click installer's storage preflight.
  printf 'custom-storage\n'
}

process_profile() {
  local user="$1"
  local profile="$2"
  local env_path="$3"
  local token aops_url logfile status safe_profile started_ms duration_ms
  local installer_pid heartbeat_pid current_ms sync_summary storage_summary task_id profile_home

  PROFILE_PROGRESS_INDEX=$((PROFILE_PROGRESS_INDEX + 1))
  printf '[PROGRESS] user=%s profile=%s index=%s/%s status=running elapsed=0s\n' \
    "$user" "$profile" "$PROFILE_PROGRESS_INDEX" "$PROFILE_PROGRESS_TOTAL"
  started_ms="$(now_ms)"

  token="$(read_dotenv_value "$env_path" AOPS_BOT_TOKEN)"
  if [[ -z "$token" || "$token" == *$'\n'* || "$token" == *$'\r'* ]]; then
    duration_ms=$(($(now_ms) - started_ms))
    record_skip "$user" "default_token_missing" "$profile" "$duration_ms"
    return 0
  fi
  aops_url="$(read_dotenv_value "$env_path" AOPS_BOT_URL)"
  if [[ -z "$aops_url" ]]; then
    aops_url="$GLOBAL_AOPS_URL"
  fi
  if [[ -n "$aops_url" ]]; then
    case "$aops_url" in
      http://*|https://*) ;;
      *)
        duration_ms=$(($(now_ms) - started_ms))
        record_skip "$user" "invalid_aops_url" "$profile" "$duration_ms"
        return 0
        ;;
    esac
  fi

  if [[ "$DRY_RUN" == true ]]; then
    duration_ms=$(($(now_ms) - started_ms))
    printf '%s\t%s\tdry_run\t%s\n' "$user" "$profile" "$duration_ms" >> "$SKIPPED_FILE"
    printf 'user=%s profile=%s status=dry-run durationMs=%s\n' "$user" "$profile" "$duration_ms"
    return 0
  fi

  safe_profile="${profile//[^a-zA-Z0-9_.-]/_}"
  logfile="$RUN_DIR/${user}-${safe_profile}.log"
  printf '[INFO] Updating Hermes for user %s profile %s\n' "$user" "$profile" > "$logfile"

  # Keep the token in the child environment instead of logging it or placing
  # it in the visible command string. The existing installer still receives
  # it through its supported --set interface.
  export UPDATE_USER="$user"
  export UPDATE_TOKEN="$token"
  export UPDATE_AOPS_URL="$aops_url"
  export UPDATE_INSTALLER_FILE="$CACHED_INSTALLER"
  export UPDATE_TIMEOUT="$TIMEOUT_SECONDS"
  export UPDATE_EXTRA_SET_FILE="$EXTRA_SET_FILE"
  export UPDATE_TEMPLATE_FILE="$CACHED_TEMPLATE"
  export UPDATE_BUNDLE_CACHE_DIR="$BUNDLE_CACHE_DIR"
  export UPDATE_RUNTIME_LAYOUT="$RUNTIME_LAYOUT"
  export UPDATE_SHARED_RUNTIME_ROOT="$SHARED_RUNTIME_ROOT"
  export UPDATE_HERMES_DATA_ROOT="$HERMES_DATA_ROOT"
  task_id="multiuser-$RUN_ID"
  export UPDATE_TASK_ID="$task_id"

  set +e
  # Give the installer time to finish its storage rollback after timeout sends
  # SIGTERM. SIGKILL is only used if the rollback itself does not complete.
  timeout --foreground --kill-after=300s "$TIMEOUT_SECONDS" bash -c '
    set -o pipefail
    args=(--sync-other-profiles true --runtime-layout "$UPDATE_RUNTIME_LAYOUT" --shared-runtime-root "$UPDATE_SHARED_RUNTIME_ROOT" --hermes-data-root "$UPDATE_HERMES_DATA_ROOT" --task-id "$UPDATE_TASK_ID" --set "targetUser=$UPDATE_USER" --set "env.AOPS_BOT_TOKEN=$UPDATE_TOKEN")
    if [[ -n "$UPDATE_TEMPLATE_FILE" ]]; then
      args+=(--template-file "$UPDATE_TEMPLATE_FILE")
    fi
    args+=(--bundle-cache-dir "$UPDATE_BUNDLE_CACHE_DIR")
    if [[ -n "$UPDATE_AOPS_URL" ]]; then
      args+=(--set "env.AOPS_BOT_URL=$UPDATE_AOPS_URL")
    fi
    if [[ -s "$UPDATE_EXTRA_SET_FILE" ]]; then
      while IFS= read -r override; do
        [[ -n "$override" ]] || continue
        args+=(--set "$override")
      done < "$UPDATE_EXTRA_SET_FILE"
    fi
    bash "$UPDATE_INSTALLER_FILE" "${args[@]}"
  ' >> "$logfile" 2>&1 &
  installer_pid=$!
  (
    while sleep 15; do
      kill -0 "$installer_pid" 2>/dev/null || exit 0
      current_ms="$(now_ms)"
      printf '[PROGRESS] user=%s profile=%s status=running elapsed=%ss\n' \
        "$user" "$profile" "$(((current_ms - started_ms) / 1000))"
    done
  ) &
  heartbeat_pid=$!
  wait "$installer_pid"
  status=$?
  kill "$heartbeat_pid" 2>/dev/null || true
  wait "$heartbeat_pid" 2>/dev/null || true
  set -e
  duration_ms=$(($(now_ms) - started_ms))

  # Defensive redaction in case a downstream installer diagnostic echoes an
  # argument or environment value. The normal installer output does not log
  # secrets, but the wrapper must not persist one accidentally.
  REDACT_TOKEN="$token" python3 - "$logfile" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
token = os.environ.get("REDACT_TOKEN", "")
if token and path.exists():
    path.write_text(path.read_text(encoding="utf-8", errors="replace").replace(token, "<redacted>"), encoding="utf-8")
PY

  profile_home="$(dirname "$env_path")"
  sync_summary="$profile_home/tec01-install/$task_id/profile-sync-summary.json"
  storage_summary="$profile_home/tec01-install/$task_id/storage-summary.json"
  runtime_summary="$profile_home/tec01-install/$task_id/shared-runtime-summary.json"
  if [[ -f "$storage_summary" ]]; then
    python3 - "$storage_summary" "$STORAGE_RESULTS_FILE" "$user" <<'PY'
import json
import sys
from pathlib import Path

summary_path, output_path, user = sys.argv[1:]
try:
    data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
except Exception:
    data = {}
values = [
    user,
    str(data.get("status") or "unknown"),
    str(data.get("dataRoot") or ""),
    ",".join(str(item) for item in (data.get("migratedPaths") or [])),
    str(int(data.get("bytesMoved") or 0)),
    str(int(data.get("durationMs") or 0)),
]
with Path(output_path).open("a", encoding="utf-8") as handle:
    handle.write("\t".join(values) + "\n")
PY
  fi
  if [[ -f "$runtime_summary" ]]; then
    python3 - "$runtime_summary" "$RUNTIME_RESULTS_FILE" "$user" <<'PY'
import json, sys
from pathlib import Path
summary_path, output_path, user = sys.argv[1:]
try:
    data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
except Exception:
    data = {}
values = [
    user,
    str(data.get("runtimeLayout") or "shared"),
    str(data.get("runtimeRelease") or ""),
    str(data.get("previousRelease") or ""),
    str(bool(data.get("runtimeReused"))).lower(),
    str(bool(data.get("bindingChanged"))).lower(),
    str(bool(data.get("rollbackPerformed"))).lower(),
]
with Path(output_path).open("a", encoding="utf-8") as handle:
    handle.write("\t".join(values) + "\n")
PY
  fi
  unset UPDATE_USER UPDATE_TOKEN UPDATE_AOPS_URL UPDATE_INSTALLER_FILE UPDATE_TIMEOUT UPDATE_EXTRA_SET_FILE UPDATE_TEMPLATE_FILE UPDATE_BUNDLE_CACHE_DIR UPDATE_RUNTIME_LAYOUT UPDATE_SHARED_RUNTIME_ROOT UPDATE_HERMES_DATA_ROOT UPDATE_TASK_ID

  if [[ "$status" -eq 0 ]]; then
    if [[ -f "$sync_summary" ]]; then
      python3 - "$sync_summary" "$UPDATED_FILE" "$user" "$logfile" <<'PY'
import json, sys
from pathlib import Path
summary_path, output_path, user, log = sys.argv[1:]
data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
with Path(output_path).open("a", encoding="utf-8") as out:
    for item in data.get("profiles") or []:
        profile = str(item.get("profile") or "default")
        duration = int(item.get("durationMs") or 0)
        status = str(item.get("gatewayStatus") or item.get("configStatus") or "updated")
        out.write(f"{user}\t{profile}\t{duration}\t{log}\t{status}\n")
PY
    else
      printf '%s\t%s\t%s\t%s\tupdated\n' "$user" "$profile" "$duration_ms" "$logfile" >> "$UPDATED_FILE"
    fi
    printf 'user=%s status=updated durationMs=%s\n' "$user" "$duration_ms"
  elif [[ "$status" -eq 124 ]]; then
    record_failure "$user" "timeout" "$status" "$profile" "$duration_ms" "$logfile"
  else
    if [[ -f "$sync_summary" ]] && python3 - "$sync_summary" "$FAILED_FILE" "$user" "$status" "$logfile" <<'PY'
import json, sys
from pathlib import Path
summary_path, output_path, user, exit_code, log = sys.argv[1:]
data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
failed = set(data.get("failedProfiles") or [])
if not failed:
    raise SystemExit(1)
with Path(output_path).open("a", encoding="utf-8") as out:
    for item in data.get("profiles") or []:
        profile = str(item.get("profile") or "default")
        if profile not in failed:
            continue
        duration = int(item.get("durationMs") or 0)
        message = str(item.get("message") or "profile_sync_failed").replace("\t", " ").replace("\n", " ")
        out.write(f"{user}\t{profile}\t{message}\t{exit_code}\t{duration}\t{log}\n")
PY
    then
      printf 'user=%s status=failed reason=profile_sync_failed exitCode=%s durationMs=%s\n' "$user" "$status" "$duration_ms" >&2
    else
      record_failure "$user" "installer_failed" "$status" "$profile" "$duration_ms" "$logfile"
    fi
  fi
}

process_user() {
  local user="$1"
  local passwd_record home shell profile_root env_path profile_dir

  if ! valid_username "$user" || [[ "$user" == root ]]; then
    record_skip "$user" "invalid_or_root_user"
    return 0
  fi

  passwd_record="$(user_record "$user")"
  if [[ -z "$passwd_record" ]]; then
    record_skip "$user" "user_not_found"
    return 0
  fi
  IFS=: read -r _ _ _ _ _ home shell <<< "$passwd_record"
  SCANNED_USERS=$((SCANNED_USERS + 1))

  if [[ -z "$home" || ! -d "$home" ]]; then
    record_skip "$user" "home_not_found"
    return 0
  fi
  if [[ ! -x "$home/hermes-agent/venv/bin/python" ]]; then
    record_skip "$user" "hermes_not_installed"
    return 0
  fi

  PROFILE_PROGRESS_INDEX=0
  PROFILE_PROGRESS_TOTAL=1
  env_path="$home/.hermes/.env"
  if [[ ! -r "$env_path" ]]; then
    record_skip "$user" "default_env_not_found" "default"
    return 0
  fi
  ELIGIBLE_PROFILES=$((ELIGIBLE_PROFILES + 1))
  profile_root="$home/.hermes/profiles"
  if [[ -d "$profile_root" ]]; then
    for profile_dir in "$profile_root"/*; do
      [[ -d "$profile_dir" ]] || continue
      ELIGIBLE_PROFILES=$((ELIGIBLE_PROFILES + 1))
    done
  fi
  ELIGIBLE_USERS=$((ELIGIBLE_USERS + 1))
  process_profile "$user" "default" "$env_path"
}

run_user_worker() {
  local user="$1"
  local ordinal="$2"
  local total="$3"
  local storage_class="${4:-unknown}"
  local safe_user worker_dir started_ms duration_ms status profiles_count storage_class_after

  safe_user="${user//[^a-zA-Z0-9_.-]/_}"
  worker_dir="$RUN_DIR/workers/${ordinal}-${safe_user}"
  mkdir -p "$worker_dir"
  UPDATED_FILE="$worker_dir/updated.tsv"
  SKIPPED_FILE="$worker_dir/skipped.tsv"
  FAILED_FILE="$worker_dir/failed.tsv"
  STORAGE_RESULTS_FILE="$worker_dir/storage.tsv"
  RUNTIME_RESULTS_FILE="$worker_dir/runtime.tsv"
  : > "$UPDATED_FILE"
  : > "$SKIPPED_FILE"
  : > "$FAILED_FILE"
  : > "$STORAGE_RESULTS_FILE"
  : > "$RUNTIME_RESULTS_FILE"
  SCANNED_USERS=0
  ELIGIBLE_USERS=0
  ELIGIBLE_PROFILES=0
  started_ms="$(now_ms)"
  printf '[PROGRESS] user=%s index=%s/%s storage=%s status=running elapsed=0s\n' "$user" "$ordinal" "$total" "$storage_class"

  process_user "$user"

  duration_ms=$(($(now_ms) - started_ms))
  storage_class_after="$(classify_user_storage "$user")"
  profiles_count="$ELIGIBLE_PROFILES"
  if [[ -s "$FAILED_FILE" ]]; then
    status="failed"
  elif [[ -s "$UPDATED_FILE" ]]; then
    status="updated"
  else
    status="skipped"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$user" "$SCANNED_USERS" "$ELIGIBLE_USERS" "$ELIGIBLE_PROFILES" "$duration_ms" "$status" "$storage_class" "$storage_class_after" \
    > "$worker_dir/meta.tsv"
  printf 'user=%s status=completed profiles=%s durationMs=%s result=%s\n' \
    "$user" "$profiles_count" "$duration_ms" "$status"
}

merge_worker_results() {
  local worker meta user scanned eligible eligible_profiles duration_ms status storage_class storage_class_after
  UPDATED_FILE="$MASTER_UPDATED_FILE"
  SKIPPED_FILE="$MASTER_SKIPPED_FILE"
  FAILED_FILE="$MASTER_FAILED_FILE"
  USER_RESULTS_FILE="$MASTER_USER_RESULTS_FILE"
  STORAGE_RESULTS_FILE="$MASTER_STORAGE_RESULTS_FILE"
  RUNTIME_RESULTS_FILE="$MASTER_RUNTIME_RESULTS_FILE"
  SCANNED_USERS=0
  ELIGIBLE_USERS=0
  ELIGIBLE_PROFILES=0
  : > "$UPDATED_FILE"
  : > "$SKIPPED_FILE"
  : > "$FAILED_FILE"
  : > "$USER_RESULTS_FILE"
  : > "$STORAGE_RESULTS_FILE"
  : > "$RUNTIME_RESULTS_FILE"
  for worker in "$RUN_DIR"/workers/*; do
    [[ -d "$worker" ]] || continue
    if [[ -f "$worker/updated.tsv" ]]; then
      cat "$worker/updated.tsv" >> "$UPDATED_FILE"
    fi
    if [[ -f "$worker/skipped.tsv" ]]; then
      cat "$worker/skipped.tsv" >> "$SKIPPED_FILE"
    fi
    if [[ -f "$worker/failed.tsv" ]]; then
      cat "$worker/failed.tsv" >> "$FAILED_FILE"
    fi
    if [[ -f "$worker/storage.tsv" ]]; then
      cat "$worker/storage.tsv" >> "$STORAGE_RESULTS_FILE"
    fi
    if [[ -f "$worker/runtime.tsv" ]]; then
      cat "$worker/runtime.tsv" >> "$RUNTIME_RESULTS_FILE"
    fi
    meta="$worker/meta.tsv"
    [[ -f "$meta" ]] || continue
    IFS=$'\t' read -r user scanned eligible eligible_profiles duration_ms status storage_class storage_class_after < "$meta"
    SCANNED_USERS=$((SCANNED_USERS + scanned))
    ELIGIBLE_USERS=$((ELIGIBLE_USERS + eligible))
    ELIGIBLE_PROFILES=$((ELIGIBLE_PROFILES + eligible_profiles))
    printf '%s\t%s\t%s\t%s\t%s\n' "$user" "$duration_ms" "$status" "$storage_class" "$storage_class_after" >> "$USER_RESULTS_FILE"
  done
}

write_summary() {
  local summary_path="$LOG_DIR/summary-$RUN_ID.json"
  local total_duration_ms
  total_duration_ms=$(($(now_ms) - RUN_START_MS))
  python3 - "$summary_path" "$UPDATED_FILE" "$SKIPPED_FILE" "$FAILED_FILE" "$USER_RESULTS_FILE" "$STORAGE_RESULTS_FILE" "$RUNTIME_RESULTS_FILE" "$SCANNED_USERS" "$ELIGIBLE_USERS" "$ELIGIBLE_PROFILES" "$DRY_RUN" "$RUN_STARTED_AT" "$total_duration_ms" "$JOBS" "$HERMES_DATA_ROOT" "$RUNTIME_LAYOUT" "$SHARED_RUNTIME_ROOT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    summary_path, updated_path, skipped_path, failed_path, users_path, storage_path, runtime_path,
    scanned, eligible, eligible_profiles, dry_run, started_at, total_duration_ms, jobs, data_root,
    runtime_layout, shared_runtime_root,
) = sys.argv[1:]

def lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()

updated_profiles = []
updated_users = []
for line in lines(updated_path):
    if not line:
        continue
    parts = line.split("\t")
    user = parts[0]
    profile = parts[1] if len(parts) > 1 else "default"
    updated_profiles.append({
        "user": user,
        "profile": profile,
        "durationMs": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        "log": parts[3] if len(parts) > 3 else None,
        "status": parts[4] if len(parts) > 4 else "updated",
    })
    if user not in updated_users:
        updated_users.append(user)
skipped = []
for line in lines(skipped_path):
    if not line:
        continue
    parts = line.split("\t")
    skipped.append({
        "user": parts[0],
        "profile": parts[1] if len(parts) > 2 else "default",
        "reason": parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "unknown"),
        "durationMs": int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
    })
failed = []
for line in lines(failed_path):
    if not line:
        continue
    parts = line.split("\t")
    failed.append({
        "user": parts[0],
        "profile": parts[1] if len(parts) > 3 else "default",
        "reason": parts[2] if len(parts) > 3 else (parts[1] if len(parts) > 1 else "unknown"),
        "exitCode": int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None,
        "durationMs": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
        "log": parts[5] if len(parts) > 5 else None,
    })

storage_by_user = {}
for line in lines(storage_path):
    if not line:
        continue
    parts = line.split("\t")
    storage_by_user[parts[0]] = {
        "status": parts[1] if len(parts) > 1 else "unknown",
        "dataRoot": parts[2] if len(parts) > 2 else data_root,
        "migratedPaths": [item for item in (parts[3].split(",") if len(parts) > 3 else []) if item],
        "bytesMoved": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0,
        "durationMs": int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0,
    }

runtime_by_user = {}
for line in lines(runtime_path):
    if not line:
        continue
    parts = line.split("\t")
    runtime_by_user[parts[0]] = {
        "runtimeLayout": parts[1] if len(parts) > 1 else runtime_layout,
        "runtimeRelease": parts[2] if len(parts) > 2 and parts[2] else None,
        "previousRelease": parts[3] if len(parts) > 3 and parts[3] else None,
        "runtimeReused": len(parts) > 4 and parts[4] == "true",
        "bindingChanged": len(parts) > 5 and parts[5] == "true",
        "rollbackPerformed": len(parts) > 6 and parts[6] == "true",
    }

users = []
for line in lines(users_path):
    if not line:
        continue
    parts = line.split("\t")
    user_result = {
        "user": parts[0],
        "durationMs": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
        "status": parts[2] if len(parts) > 2 else "unknown",
        "storageBefore": parts[3] if len(parts) > 3 else "unknown",
        "storageStatus": parts[4] if len(parts) > 4 else "unknown",
    }
    if parts[0] in storage_by_user:
        user_result["storage"] = storage_by_user[parts[0]]
    if parts[0] in runtime_by_user:
        user_result["runtime"] = runtime_by_user[parts[0]]
    users.append(user_result)

finished_at = datetime.now(timezone.utc).isoformat()

result = {
    "ok": not failed,
    "dryRun": dry_run == "true",
    "startedAt": started_at,
    "finishedAt": finished_at,
    "totalDurationMs": int(total_duration_ms),
    "jobs": int(jobs),
    "hermesDataRoot": data_root,
    "runtimeLayout": runtime_layout,
    "sharedRuntimeRoot": shared_runtime_root if runtime_layout == "shared" else None,
    "scannedUsers": int(scanned),
    "eligibleUsers": int(eligible),
    "eligibleProfiles": int(eligible_profiles),
    "updatedUsers": updated_users,
    "updatedProfiles": updated_profiles,
    "users": users,
    "skippedUsers": skipped,
    "failedUsers": failed,
    "failedProfiles": failed,
}
Path(summary_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
  printf '[TIMING] totalDurationMs=%s\n' "$total_duration_ms"
  printf '[INFO] Summary: %s\n' "$summary_path"
}

acquire_lock
RUN_START_MS="$(now_ms)"
RUN_STARTED_AT="$(iso_now)"
prefetch_inputs

USERS=()
if ((${#REQUESTED_USERS[@]} > 0)); then
  USERS=("${REQUESTED_USERS[@]}")
else
  while IFS=: read -r user _ _ _ _ _ _; do
    [[ -n "$user" ]] || continue
    USERS+=("$user")
  done < <(getent passwd)
fi

DEDUPED_USERS=()
declare -A SEEN_USERS=()
for user in "${USERS[@]}"; do
  [[ -z "${SEEN_USERS[$user]:-}" ]] || continue
  SEEN_USERS[$user]=1
  DEDUPED_USERS+=("$user")
done
USERS=("${DEDUPED_USERS[@]}")

mkdir -p "$RUN_DIR/workers"
total_users=${#USERS[@]}
ordinal=0
batch_pids=()
MIGRATION_USERS=()
NORMAL_USERS=()
declare -A USER_STORAGE_CLASS=()
classification_started_ms="$(now_ms)"
for user in "${USERS[@]}"; do
  storage_class="$(classify_user_storage "$user")"
  USER_STORAGE_CLASS["$user"]="$storage_class"
  if [[ "$storage_class" == migration-needed ]]; then
    MIGRATION_USERS+=("$user")
  else
    NORMAL_USERS+=("$user")
  fi
done
printf '[TIMING] stage=classify_storage durationMs=%s migrationUsers=%s normalUsers=%s\n' \
  "$(($(now_ms) - classification_started_ms))" "${#MIGRATION_USERS[@]}" "${#NORMAL_USERS[@]}"

if ((${#MIGRATION_USERS[@]} > 0)); then
  printf '[INFO] Processing %s root-filesystem Hermes installation(s) serially before concurrent updates\n' "${#MIGRATION_USERS[@]}"
fi
for user in "${MIGRATION_USERS[@]}"; do
  ordinal=$((ordinal + 1))
  run_user_worker "$user" "$ordinal" "$total_users" "${USER_STORAGE_CLASS[$user]}"
done

for user in "${NORMAL_USERS[@]}"; do
  ordinal=$((ordinal + 1))
  if [[ "$JOBS" -eq 1 ]]; then
    run_user_worker "$user" "$ordinal" "$total_users" "${USER_STORAGE_CLASS[$user]}"
    continue
  fi
  run_user_worker "$user" "$ordinal" "$total_users" "${USER_STORAGE_CLASS[$user]}" &
  batch_pids+=("$!")
  if ((${#batch_pids[@]} >= JOBS)); then
    for worker_pid in "${batch_pids[@]}"; do
      wait "$worker_pid" || true
    done
    batch_pids=()
  fi
done
for worker_pid in "${batch_pids[@]}"; do
  wait "$worker_pid" || true
done

merge_worker_results
write_summary

if [[ -s "$FAILED_FILE" ]]; then
  exit 1
fi
