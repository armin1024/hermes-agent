#!/usr/bin/env bash
# tec01-managed Hermes one-click installer/upgrader.
#
# Primary usage:
#   curl -fsSL "http://tec01/hermes/install-oneclick.sh" | sudo bash -s -- \
#     --set targetUser=hermes \
#     --set env.AOPS_BOT_TOKEN=...
#
# The script renders a built-in or supplied YAML template into a temporary
# JSON payload, installs Hermes for the target user when missing, then creates
# or updates a profile selected by AOPS_BOT_TOKEN.

set -euo pipefail
umask 077

TEC01_REPORT_URL="${TEC01_REPORT_URL:-}"
TASK_ID="${TASK_ID:-}"
WORK_DIR="${WORK_DIR:-}"
BUNDLE_CACHE_DIR="${HERMES_BUNDLE_CACHE_DIR:-}"
HERMES_DATA_ROOT="${HERMES_DATA_ROOT:-/data/hermes-users}"
SYNC_OTHER_PROFILES_CLI=""
TEMPLATE_FILE=""
TEMPLATE_URL=""
SET_ARGS=()
SKILLS_ZIPS=()
TIMING_START_MS=""
TIMING_STAGE_START_MS=""
TIMING_RAW_FILE=""
TIMING_FINALIZED=false
BUNDLE_CACHE_LOCK_DIR=""
STORAGE_LOCK_FD=""
STORAGE_STATE_DIR=""
STORAGE_SUMMARY_JSON=""
STORAGE_STARTED_MS=""
STORAGE_TARGET_MOUNT=""
STORAGE_TARGET_FS=""
STORAGE_CREATED_GUARDS=()
STORAGE_ROLLBACK_ARMED=false

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

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  if [[ "${STAGE:-}" == storage_* ]] && declare -F storage_write_summary >/dev/null 2>&1 && [[ -n "${STORAGE_SUMMARY_JSON:-}" ]]; then
    storage_write_summary "failed" "$(( $(now_ms) - ${STORAGE_STARTED_MS:-$(now_ms)} ))" 0 "" "" "${STORAGE_TARGET_FS:-}" "$*" 2>/dev/null || true
  fi
  report_result "failed" "${STAGE:-unknown}" "$*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL http://tec01/hermes/install-oneclick.sh | sudo bash -s -- \
    --set targetUser=hermes \
    --set env.AOPS_BOT_TOKEN=<token> \
    [--set key=value ...] \
    [--skills-zip <path-or-url> ...]

Options:
  --set KEY=VALUE       Set or override a template parameter/path. Repeatable.
  --skills-zip VALUE    Local path or URL to a .zip skill bundle. Repeatable.
  --template-file FILE  Read YAML template from a local file.
  --template-url URL    Download YAML template from URL.
  --bundle-cache-dir DIR
                        Reuse verified offline bundles by SHA256.
  --hermes-data-root DIR
                        Physical root for Hermes runtime/profile data
                        (default: /data/hermes-users).
  --sync-other-profiles true|false
                        Apply explicit overwrite fields to all existing profiles.
  --report-url URL      Optional task result report endpoint.
  --task-id ID          Optional task id for reporting/work-dir naming.
  --work-dir DIR        Optional private working directory.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --set)
      SET_ARGS+=("$2")
      shift 2
      ;;
    --skills-zip)
      SKILLS_ZIPS+=("$2")
      shift 2
      ;;
    --template-file)
      TEMPLATE_FILE="$2"
      shift 2
      ;;
    --template-url)
      TEMPLATE_URL="$2"
      shift 2
      ;;
    --bundle-cache-dir)
      BUNDLE_CACHE_DIR="$2"
      shift 2
      ;;
    --hermes-data-root)
      [[ $# -ge 2 ]] || fail "--hermes-data-root requires an absolute path"
      HERMES_DATA_ROOT="$2"
      shift 2
      ;;
    --sync-other-profiles)
      [[ $# -ge 2 ]] || fail "--sync-other-profiles requires true or false"
      case "$2" in
        true|false) SYNC_OTHER_PROFILES_CLI="$2" ;;
        *) fail "--sync-other-profiles must be true or false" ;;
      esac
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
    --work-dir)
      WORK_DIR="$2"
      shift 2
      ;;
    --task-url)
      fail "--task-url is no longer supported; use --template-url and --set key=value"
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

now_ms() {
  python3 - <<'PY'
import time
print(time.monotonic_ns() // 1_000_000)
PY
}

begin_stage() {
  local next_stage="$1"
  local now duration
  now="$(now_ms)"
  if [[ -n "${STAGE:-}" && -n "$TIMING_STAGE_START_MS" && -n "$TIMING_RAW_FILE" ]]; then
    duration=$((now - TIMING_STAGE_START_MS))
    printf '%s\t%s\n' "$STAGE" "$duration" >> "$TIMING_RAW_FILE"
    printf '[TIMING] stage=%s durationMs=%s\n' "$STAGE" "$duration"
  fi
  STAGE="$next_stage"
  TIMING_STAGE_START_MS="$now"
}

finish_timings() {
  local exit_code="${1:-0}"
  local now duration output_path
  [[ "$TIMING_FINALIZED" == false && -n "$TIMING_START_MS" ]] || return 0
  TIMING_FINALIZED=true
  now="$(now_ms)"
  if [[ -n "${STAGE:-}" && -n "$TIMING_STAGE_START_MS" && -n "$TIMING_RAW_FILE" ]]; then
    duration=$((now - TIMING_STAGE_START_MS))
    printf '%s\t%s\n' "$STAGE" "$duration" >> "$TIMING_RAW_FILE"
    printf '[TIMING] stage=%s durationMs=%s\n' "$STAGE" "$duration"
  fi
  duration=$((now - TIMING_START_MS))
  printf '[TIMING] totalDurationMs=%s\n' "$duration"
  if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
    output_path="$WORK_DIR/install-timings.json"
  else
    output_path="${BOOTSTRAP_DIR:-${TMPDIR:-/tmp}}/install-timings.json"
  fi
  python3 - "$TIMING_RAW_FILE" "$output_path" "$duration" "$exit_code" "${STORAGE_SUMMARY_JSON:-}" <<'PY' || true
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

raw_path, output_path, total_ms, exit_code, storage_summary_path = sys.argv[1:]
stages = []
try:
    for line in Path(raw_path).read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        name, duration = line.split("\t", 1)
        stages.append({"stage": name, "durationMs": int(duration)})
except Exception:
    pass
total = int(total_ms)
finished = datetime.now(timezone.utc)
started = finished.timestamp() - total / 1000
payload = {
    "ok": int(exit_code) == 0,
    "exitCode": int(exit_code),
    "startedAt": datetime.fromtimestamp(started, timezone.utc).isoformat(),
    "finishedAt": finished.isoformat(),
    "totalDurationMs": total,
    "stages": stages,
}
try:
    storage_path = Path(storage_summary_path)
    if storage_path.is_file():
        payload["storage"] = json.loads(storage_path.read_text(encoding="utf-8"))
except Exception:
    pass
Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  if [[ -n "${TARGET_USER:-}" && "$(id -u)" -eq 0 && -f "$output_path" ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$output_path" 2>/dev/null || true
  fi
}

on_exit() {
  local exit_code=$?
  trap - EXIT
  trap - HUP INT TERM
  set +e
  if [[ "${STORAGE_ROLLBACK_ARMED:-false}" == true ]] && declare -F rollback_storage_cutover >/dev/null 2>&1; then
    warn "Installer interrupted during Hermes storage migration; rolling back"
    rollback_storage_cutover || true
  fi
  finish_timings "$exit_code"
  if [[ -n "$BUNDLE_CACHE_LOCK_DIR" ]]; then
    rmdir "$BUNDLE_CACHE_LOCK_DIR" 2>/dev/null || true
  fi
  exit "$exit_code"
}

on_interrupt() {
  local signal_number="$1"
  exit "$((128 + signal_number))"
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

download_bundle() {
  local url="$1"
  local expected_sha="$2"
  local dst="$3"
  local cache_file lock_file tmp_file actual_sha lock_dir=""

  if [[ -z "$BUNDLE_CACHE_DIR" ]]; then
    download "$url" "$dst"
    return 0
  fi

  mkdir -p "$BUNDLE_CACHE_DIR"
  chmod 700 "$BUNDLE_CACHE_DIR" 2>/dev/null || true
  cache_file="$BUNDLE_CACHE_DIR/${expected_sha}.tar.gz"
  lock_file="$BUNDLE_CACHE_DIR/${expected_sha}.lock"

  if command -v flock >/dev/null 2>&1; then
    exec 8>"$lock_file"
    flock 8
  else
    lock_dir="${lock_file}.d"
    while ! mkdir "$lock_dir" 2>/dev/null; do
      sleep 0.2
    done
    BUNDLE_CACHE_LOCK_DIR="$lock_dir"
  fi

  if [[ -f "$cache_file" ]]; then
    actual_sha="$(sha256_file "$cache_file")"
    if [[ "$actual_sha" == "$expected_sha" ]]; then
      log "Using cached bundle: $cache_file"
    else
      warn "Removing corrupt cached bundle: $cache_file"
      rm -f "$cache_file"
    fi
  fi

  if [[ ! -f "$cache_file" ]]; then
    tmp_file="$BUNDLE_CACHE_DIR/.${expected_sha}.$$.tmp"
    rm -f "$tmp_file"
    download "$url" "$tmp_file"
    actual_sha="$(sha256_file "$tmp_file")"
    if [[ "$actual_sha" != "$expected_sha" ]]; then
      rm -f "$tmp_file"
      [[ -n "$lock_dir" ]] && rmdir "$lock_dir" 2>/dev/null || true
      fail "sha256 mismatch: expected $expected_sha got $actual_sha"
    fi
    chmod 600 "$tmp_file"
    mv -f "$tmp_file" "$cache_file"
    log "Cached bundle: $cache_file"
  fi

  cp "$cache_file" "$dst"
  if command -v flock >/dev/null 2>&1; then
    flock -u 8 2>/dev/null || true
    exec 8>&-
  elif [[ -n "$lock_dir" ]]; then
    rmdir "$lock_dir" 2>/dev/null || true
    BUNDLE_CACHE_LOCK_DIR=""
  fi
}

json_get() {
  local expr="$1"
  python3 - "$PAYLOAD_JSON" "$expr" <<'PY'
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
    true|True|1|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

validate_username() {
  local user="$1"
  [[ "$user" != "root" ]] || fail "targetUser=root is not allowed"
  [[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "Invalid targetUser: $user"
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

shell_quote() {
  python3 - "$1" <<'PY'
import shlex
import sys
print(shlex.quote(sys.argv[1]))
PY
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

storage_systemctl_as_target() {
  local command="$1"
  run_as_target "export XDG_RUNTIME_DIR=/run/user/\$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=\$XDG_RUNTIME_DIR/bus; $command"
}

storage_write_summary() {
  local status="$1"
  local duration_ms="$2"
  local bytes_moved="$3"
  local migrated_csv="$4"
  local source_fs="$5"
  local target_fs="$6"
  local error_message="${7:-}"
  [[ -n "$STORAGE_SUMMARY_JSON" ]] || return 0
  python3 - "$STORAGE_SUMMARY_JSON" "$status" "$HERMES_DATA_ROOT" "$duration_ms" "$bytes_moved" "$migrated_csv" "$source_fs" "$target_fs" "$error_message" <<'PY'
import json
import sys
from pathlib import Path

path, status, data_root, duration, moved, migrated, source_fs, target_fs, error = sys.argv[1:]
payload = {
    "status": status,
    "dataRoot": data_root,
    "migratedPaths": [item for item in migrated.split(",") if item],
    "bytesMoved": int(moved or 0),
    "sourceFilesystem": source_fs or None,
    "targetFilesystem": target_fs or None,
    "durationMs": int(duration or 0),
}
if error:
    payload["error"] = error
Path(path).parent.mkdir(parents=True, exist_ok=True)
Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

storage_find_existing_parent() {
  local path="$1"
  while [[ ! -e "$path" && "$path" != "/" ]]; do
    path="$(dirname "$path")"
  done
  printf '%s' "$path"
}

storage_validate_target_mount() {
  local probe mount_target fs_type mount_options target_source
  probe="$(storage_find_existing_parent "$HERMES_DATA_ROOT")"
  mount_target="$(findmnt -T "$probe" -n -o TARGET 2>/dev/null || true)"
  fs_type="$(findmnt -T "$probe" -n -o FSTYPE 2>/dev/null || true)"
  mount_options="$(findmnt -T "$probe" -n -o OPTIONS 2>/dev/null || true)"
  target_source="$(findmnt -T "$probe" -n -o SOURCE 2>/dev/null || true)"
  [[ -n "$mount_target" && "$mount_target" != "/" ]] || {
    warn "$HERMES_DATA_ROOT is not backed by an independent mounted filesystem"
    return 1
  }
  case "$fs_type" in
    nfs|nfs4|cifs|smb3|fuse*|overlay)
      warn "$HERMES_DATA_ROOT uses unsupported filesystem type: $fs_type"
      return 1
      ;;
  esac
  case ",$mount_options," in
    *,ro,*) warn "$HERMES_DATA_ROOT is mounted read-only"; return 1 ;;
    *,noexec,*) warn "$HERMES_DATA_ROOT is mounted noexec"; return 1 ;;
  esac
  STORAGE_TARGET_MOUNT="$mount_target"
  STORAGE_TARGET_FS="$target_source"
}

storage_path_mount_target() {
  findmnt -T "$1" -n -o TARGET 2>/dev/null || true
}

storage_check_nested_mounts() {
  local source="$1"
  local nested
  nested="$(findmnt -rn -o TARGET | awk -v p="$source/" 'index($0,p)==1 {print; exit}')"
  [[ -z "$nested" ]] || {
    warn "Refusing to migrate $source because it contains nested mount $nested"
    return 1
  }
}

storage_find_unmanaged_processes() {
  local source_a="$1"
  local source_b="$2"
  python3 - "$TARGET_USER" "$source_a" "$source_b" <<'PY'
import os
import pwd
import sys
from pathlib import Path

uid = pwd.getpwnam(sys.argv[1]).pw_uid
roots = [str(Path(item).resolve()) for item in sys.argv[2:] if item and Path(item).exists()]
found = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    proc = Path("/proc") / name
    try:
        if proc.stat().st_uid != uid:
            continue
        values = []
        for link in ("exe", "cwd"):
            try:
                values.append(os.path.realpath(proc / link))
            except OSError:
                pass
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    values.append(os.path.realpath(fd))
                except OSError:
                    pass
        except OSError:
            pass
        try:
            values.append((proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"))
        except OSError:
            pass
        if any(value == root or value.startswith(root + os.sep) or root in value for root in roots for value in values):
            # Do not persist cmdline arguments: installers may carry secrets in
            # --set values. PID and executable are sufficient diagnostics.
            executable = Path(values[0]).name if values else "unknown"
            found.append(f"pid={name} executable={executable}")
    except (OSError, ProcessLookupError):
        pass
if found:
    print("\n".join(found))
    raise SystemExit(1)
PY
}

storage_snapshot_profile_state() {
  local output="$1"
  python3 - "$TARGET_HOME/.hermes" "$output" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
output = Path(sys.argv[2])
rows = []
if root.is_dir():
    profile_dirs = [("default", root)]
    profiles = root / "profiles"
    if profiles.is_dir():
        profile_dirs.extend(
            (child.name, child)
            for child in sorted(profiles.iterdir(), key=lambda item: item.name)
            if child.is_dir()
        )
    for name, directory in profile_dirs:
        rows.append(f"profile\t{name}")
        for relative in (".env", "config.yaml", "cron/jobs.json", "hindsight/config.json"):
            path = directory / relative
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"file\t{name}\t{relative}\t{digest}\t{path.stat().st_size}")
output.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
PY
}

storage_capture_running_gateway_units() {
  local output="$1"
  : > "$output"
  local unit_path unit
  shopt -s nullglob
  for unit_path in "$TARGET_HOME/.config/systemd/user/hermes-gateway"*.service; do
    unit="$(basename "$unit_path")"
    if storage_systemctl_as_target "timeout 10 systemctl --user is-active --quiet $(shell_quote "$unit")" >/dev/null 2>&1; then
      printf '%s\n' "$unit" >> "$output"
    fi
  done
  shopt -u nullglob
}

storage_stop_gateway_units() {
  local units_file="$1"
  local unit
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    log "Stopping gateway service for storage migration: $unit"
    storage_systemctl_as_target "timeout 90 systemctl --user stop $(shell_quote "$unit")" || return 1
  done < "$units_file"
}

storage_restore_gateway_units() {
  local units_file="$1"
  local unit rc=0
  [[ -f "$units_file" ]] || return 0
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    log "Restoring gateway service after storage migration: $unit"
    storage_systemctl_as_target "timeout 90 systemctl --user start $(shell_quote "$unit")" || rc=1
  done < "$units_file"
  return "$rc"
}

storage_install_mount_guards() {
  [[ "${STORAGE_MANAGED:-false}" == true ]] || return 0
  local marker="$HERMES_DATA_ROOT/$TARGET_USER/.hermes-storage-ready"
  local unit_path unit dropin installed=0
  [[ -f "$marker" ]] || return 1
  shopt -s nullglob
  for unit_path in "$TARGET_HOME/.config/systemd/user/hermes-gateway"*.service; do
    unit="$(basename "$unit_path")"
    dropin="$TARGET_HOME/.config/systemd/user/$unit.d/10-hermes-data-mount.conf"
    mkdir -p "$(dirname "$dropin")" || { shopt -u nullglob; return 1; }
    if [[ -f "$dropin" ]]; then
      grep -Fqx "ExecStartPre=/usr/bin/test -f $marker" "$dropin" || {
        shopt -u nullglob
        warn "Refusing to overwrite an unexpected Hermes data mount guard: $dropin"
        return 1
      }
      installed=$((installed + 1))
      continue
    fi
    if ! cat > "$dropin" <<EOF
[Unit]
ConditionPathIsMountPoint=$STORAGE_TARGET_MOUNT

[Service]
ExecStartPre=/usr/bin/test -f $marker
EOF
    then
      shopt -u nullglob
      return 1
    fi
    chmod 600 "$dropin" || { shopt -u nullglob; return 1; }
    if [[ "$(id -u)" -eq 0 ]]; then
      chown "$TARGET_USER":"$TARGET_USER" "$(dirname "$dropin")" "$dropin" || { shopt -u nullglob; return 1; }
    fi
    STORAGE_CREATED_GUARDS+=("$dropin")
    installed=$((installed + 1))
  done
  shopt -u nullglob
  [[ "$installed" -eq 0 ]] || storage_systemctl_as_target "timeout 15 systemctl --user daemon-reload"
}

storage_remove_created_mount_guards() {
  local guard
  for guard in "${STORAGE_CREATED_GUARDS[@]}"; do
    [[ -f "$guard" ]] && rm -f "$guard"
    rmdir "$(dirname "$guard")" 2>/dev/null || true
  done
  STORAGE_CREATED_GUARDS=()
  storage_systemctl_as_target "timeout 15 systemctl --user daemon-reload" >/dev/null 2>&1 || true
}

ensure_hermes_data_layout() {
  local started source_fs="" target_base marker status="mapped" bytes=0
  local logical name target mount_target required=0 migrate=0 root_device
  local migrated_csv="" units_file backup_runtime="" backup_home="" marker_existed=false
  local verify_profile_inventory=false
  local -a migrate_sources=() migrate_targets=() new_logicals=() new_targets=() created_links=()
  started="$(now_ms)"
  STORAGE_STARTED_MS="$started"
  STORAGE_MANAGED=false

  [[ "$HERMES_DATA_ROOT" == /* ]] || fail "--hermes-data-root must be an absolute path"
  [[ "$HERMES_DATA_ROOT" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "--hermes-data-root contains unsupported characters"
  need_cmd findmnt
  need_cmd realpath
  HERMES_DATA_ROOT="$(realpath -m "$HERMES_DATA_ROOT")"
  [[ "$HERMES_DATA_ROOT" != "/" ]] || fail "--hermes-data-root cannot be /"
  target_base="$HERMES_DATA_ROOT/$TARGET_USER"
  marker="$target_base/.hermes-storage-ready"
  [[ ! -e "$marker" ]] || marker_existed=true
  local target_uid
  target_uid="$(id -u "$TARGET_USER")"
  root_device="$(stat -Lc %d /)"

  for logical in "$TARGET_HOME/hermes-agent" "$TARGET_HOME/.hermes"; do
    name="$(basename "$logical")"
    target="$target_base/$name"
    if [[ -L "$logical" ]]; then
      [[ "$(readlink -f "$logical" 2>/dev/null || true)" == "$target" ]] || fail "Unexpected Hermes symlink: $logical -> $(readlink "$logical")"
      [[ -d "$target" ]] || fail "Hermes storage link is dangling: $logical"
      [[ ! -L "$target" && "$(stat -c %u "$target")" == "$target_uid" ]] || fail "Hermes storage target owner/type mismatch: $target"
      STORAGE_MANAGED=true
      continue
    fi
    if [[ ! -e "$logical" ]]; then
      required=1
      new_logicals+=("$logical")
      new_targets+=("$target")
      continue
    fi
    [[ -d "$logical" ]] || fail "Hermes path is not a directory: $logical"
    mount_target="$(storage_path_mount_target "$logical")"
    if [[ "$(stat -Lc %d "$logical")" == "$root_device" ]]; then
      storage_check_nested_mounts "$logical" || fail "Nested mount detected under $logical"
      required=1
      migrate=1
      migrate_sources+=("$logical")
      migrate_targets+=("$target")
      if [[ "$logical" == "$TARGET_HOME/.hermes" ]]; then
        verify_profile_inventory=true
      fi
      source_fs="$(findmnt -T "$logical" -n -o SOURCE 2>/dev/null || true)"
      bytes=$((bytes + $(du -sx --block-size=1 "$logical" | awk '{print $1}')))
    else
      log "Keeping custom Hermes path outside root filesystem: $logical (mount=$mount_target)"
      # Custom storage deliberately skips validation of HERMES_DATA_ROOT. Keep
      # its actual filesystem in the diagnostic summary without requiring the
      # managed-storage globals to have been populated.
      [[ -n "$source_fs" ]] || source_fs="$(findmnt -T "$logical" -n -o SOURCE 2>/dev/null || true)"
    fi
  done

  if [[ "$required" -eq 0 ]]; then
    if [[ "$STORAGE_MANAGED" == true ]]; then
      storage_validate_target_mount || fail "Hermes data root validation failed: $HERMES_DATA_ROOT"
      [[ -f "$marker" ]] || fail "Hermes data marker is missing: $marker"
      storage_write_summary "already_mapped" "$(( $(now_ms) - started ))" 0 "" "$source_fs" "$STORAGE_TARGET_FS"
    else
      storage_write_summary "external" "$(( $(now_ms) - started ))" 0 "" "$source_fs" "${STORAGE_TARGET_FS:-}"
    fi
    return 0
  fi
  storage_validate_target_mount || fail "Hermes data root validation failed: $HERMES_DATA_ROOT"
  [[ "$(id -u)" -eq 0 ]] || fail "Run as root to create or migrate Hermes /data storage mappings"
  need_cmd cmp
  need_cmd df
  need_cmd du
  need_cmd find
  need_cmd flock
  need_cmd rsync
  need_cmd stat
  mkdir -p /var/lock
  exec {STORAGE_LOCK_FD}>/var/lock/hermes-tec01-storage.lock
  flock -w 300 "$STORAGE_LOCK_FD" || fail "Timed out waiting for Hermes storage migration lock"

  local inode_need=1024
  if [[ -e "$target_base" && ! -d "$target_base" ]]; then
    fail "Hermes data target is not a directory: $target_base"
  fi
  if [[ -d "$target_base" && "$(stat -c %u "$target_base")" != "$target_uid" ]]; then
    fail "Hermes data target owner mismatch: $target_base"
  fi
  mkdir -p "$HERMES_DATA_ROOT"
  chown root:root "$HERMES_DATA_ROOT"
  chmod 0711 "$HERMES_DATA_ROOT"
  mkdir -p "$target_base"
  chown "$TARGET_USER":"$TARGET_USER" "$target_base"
  chmod 700 "$target_base"
  for target in "${migrate_targets[@]}" "${new_targets[@]}"; do
    [[ -n "$target" ]] || continue
    [[ ! -L "$target" ]] || fail "Hermes data target must not be a symlink: $target"
    if [[ -e "$target" ]]; then
      [[ -d "$target" ]] || fail "Hermes data target is not a directory: $target"
      [[ "$(stat -c %u "$target")" == "$target_uid" ]] || fail "Hermes data target owner mismatch: $target"
    fi
  done
  for target in "${new_targets[@]}"; do
    [[ ! -d "$target" || -z "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "Refusing to reuse non-empty target for a new Hermes path: $target"
  done
  for target in "${migrate_targets[@]}"; do
    [[ ! -d "$target" || -z "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]] || \
      fail "Refusing to overwrite non-empty Hermes migration target: $target"
  done
  local available required_bytes available_inodes
  available="$(df -PB1 "$STORAGE_TARGET_MOUNT" | awk 'NR==2 {print $4}')"
  required_bytes=$((bytes + bytes / 10 + 536870912))
  [[ "$available" -ge "$required_bytes" ]] || fail "Insufficient space on $STORAGE_TARGET_MOUNT: need $required_bytes bytes, available $available"
  for logical in "${migrate_sources[@]}"; do
    inode_need=$((inode_need + $(find "$logical" -xdev -printf '.' | wc -c)))
  done
  available_inodes="$(df -Pi "$STORAGE_TARGET_MOUNT" | awk 'NR==2 {print $4}')"
  [[ "${available_inodes:-0}" -ge "$inode_need" ]] || fail "Insufficient free inodes on $STORAGE_TARGET_MOUNT: need $inode_need, available ${available_inodes:-0}"

  STORAGE_STATE_DIR="$STORAGE_TARGET_MOUNT/hermes-tec01/migrations/$TARGET_USER/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$STORAGE_STATE_DIR"
  chmod 700 "$STORAGE_STATE_DIR"
  STORAGE_SUMMARY_JSON="$STORAGE_STATE_DIR/storage-summary.json"
  units_file="$STORAGE_STATE_DIR/running-gateway-units.txt"
  storage_capture_running_gateway_units "$units_file"
  find "$TARGET_HOME/.hermes" -maxdepth 3 -type f \( -name 'config.yaml' -o -name '.env' -o -name 'gateway_state.json' \) -print 2>/dev/null > "$STORAGE_STATE_DIR/key-files.before" || true

  begin_stage "storage_precopy"
  for ((i=0; i<${#migrate_sources[@]}; i++)); do
    mkdir -p "${migrate_targets[$i]}"
    chown "$TARGET_USER":"$TARGET_USER" "${migrate_targets[$i]}"
    log "Pre-copying ${migrate_sources[$i]} to ${migrate_targets[$i]}"
    set +e
    rsync -aHAXx --numeric-ids --delete "${migrate_sources[$i]}/" "${migrate_targets[$i]}/"
    precopy_rc=$?
    set -e
    if [[ "$precopy_rc" -ne 0 && "$precopy_rc" -ne 24 ]]; then
      fail "Hermes storage pre-copy failed (rsync exit $precopy_rc)"
    fi
    if [[ "$precopy_rc" -eq 24 ]]; then
      warn "Files changed during Hermes pre-copy; the stopped final sync will reconcile them"
    fi
  done

  rollback_storage_cutover() {
    local created
    STORAGE_ROLLBACK_ARMED=false
    # A previous restore attempt may have started only some units. Stop every
    # unit from the captured running set before reverting paths to avoid split
    # writes between the source tree and /data.
    storage_stop_gateway_units "$units_file" >/dev/null 2>&1 || true
    for created in "${created_links[@]}"; do
      [[ -L "$created" ]] && rm -f "$created"
    done
    if [[ -n "$backup_runtime" && -e "$backup_runtime" ]]; then
      [[ -L "$TARGET_HOME/hermes-agent" ]] && rm -f "$TARGET_HOME/hermes-agent"
      [[ -e "$TARGET_HOME/hermes-agent" ]] || mv "$backup_runtime" "$TARGET_HOME/hermes-agent"
      backup_runtime=""
    fi
    if [[ -n "$backup_home" && -e "$backup_home" ]]; then
      [[ -L "$TARGET_HOME/.hermes" ]] && rm -f "$TARGET_HOME/.hermes"
      [[ -e "$TARGET_HOME/.hermes" ]] || mv "$backup_home" "$TARGET_HOME/.hermes"
      backup_home=""
    fi
    [[ "$marker_existed" == true ]] || rm -f "$marker"
    storage_remove_created_mount_guards
    storage_restore_gateway_units "$units_file" || true
  }
  STORAGE_ROLLBACK_ARMED=true

  begin_stage "storage_cutover"
  if ! storage_stop_gateway_units "$units_file"; then
    storage_restore_gateway_units "$units_file" || true
    fail "Failed to stop gateway services for Hermes storage migration"
  fi
  if ! storage_find_unmanaged_processes "${migrate_sources[0]:-}" "${migrate_sources[1]:-}" > "$STORAGE_STATE_DIR/unmanaged-processes.txt"; then
    storage_restore_gateway_units "$units_file" || true
    fail "Unmanaged Hermes processes still use source paths; see $STORAGE_STATE_DIR/unmanaged-processes.txt"
  fi
  for ((i=0; i<${#migrate_sources[@]}; i++)); do
    if ! rsync -aHAXx --numeric-ids --delete "${migrate_sources[$i]}/" "${migrate_targets[$i]}/"; then
      storage_restore_gateway_units "$units_file" || true
      fail "Hermes storage final sync failed"
    fi
    if [[ -n "$(rsync -aHAXxni --numeric-ids --delete "${migrate_sources[$i]}/" "${migrate_targets[$i]}/")" ]]; then
      storage_restore_gateway_units "$units_file" || true
      fail "Hermes storage verification found differences after final sync"
    fi
  done
  # Inventory equivalence protects migrations of an existing .hermes tree.
  # A fresh install has no pre-cutover profile tree, so comparing it with the
  # newly-created empty .hermes directory would manufacture a false change.
  if [[ "$verify_profile_inventory" == true ]]; then
    storage_snapshot_profile_state "$STORAGE_STATE_DIR/profile-state.before"
  fi

  for ((i=0; i<${#migrate_sources[@]}; i++)); do
    logical="${migrate_sources[$i]}"
    target="${migrate_targets[$i]}"
    name="$(basename "$logical")"
    local backup="$logical.before-data-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    if [[ "$name" == "hermes-agent" ]]; then backup_runtime="$backup"; else backup_home="$backup"; fi
    if ! mv "$logical" "$backup" || ! ln -s "$target" "$logical"; then
      rollback_storage_cutover
      fail "Failed to switch Hermes storage mapping for $logical"
    fi
    created_links+=("$logical")
    migrated_csv="${migrated_csv}${migrated_csv:+,}$name"
  done
  for ((i=0; i<${#new_logicals[@]}; i++)); do
    logical="${new_logicals[$i]}"
    target="${new_targets[$i]}"
    if ! mkdir -p "$target" || ! chown "$TARGET_USER":"$TARGET_USER" "$target" || ! chmod 700 "$target" || ! ln -s "$target" "$logical"; then
      rollback_storage_cutover
      fail "Failed to create Hermes storage link: $logical"
    fi
    created_links+=("$logical")
    name="$(basename "$logical")"
    migrated_csv="${migrated_csv}${migrated_csv:+,}$name"
  done
  if ! touch "$marker" || ! chown "$TARGET_USER":"$TARGET_USER" "$marker" || ! chmod 600 "$marker"; then
    rollback_storage_cutover
    fail "Failed to create Hermes storage marker: $marker"
  fi
  STORAGE_MANAGED=true

  begin_stage "storage_verify"
  for logical in "$TARGET_HOME/hermes-agent" "$TARGET_HOME/.hermes"; do
    if [[ -L "$logical" ]]; then
      target="$target_base/$(basename "$logical")"
      [[ "$(readlink -f "$logical")" == "$target" && -d "$target" ]] || {
        rollback_storage_cutover
        fail "Hermes storage link verification failed: $logical"
      }
    fi
  done
  if [[ -n "$backup_runtime" && -x "$TARGET_HOME/hermes-agent/venv/bin/python" ]]; then
    run_as_target "$(shell_quote "$TARGET_HOME/hermes-agent/venv/bin/python") -c 'import hermes_cli'" || {
      rollback_storage_cutover
      fail "Migrated Hermes runtime import verification failed"
    }
    if [[ -x "$TARGET_HOME/hermes-agent/hermes" ]]; then
      run_as_target "$(shell_quote "$TARGET_HOME/hermes-agent/hermes") --version >/dev/null" || {
        rollback_storage_cutover
        fail "Migrated Hermes runtime version verification failed"
      }
    fi
  fi
  if ! storage_install_mount_guards; then
    rollback_storage_cutover
    fail "Failed to install Hermes data mount guards"
  fi
  if [[ "$verify_profile_inventory" == true ]]; then
    storage_snapshot_profile_state "$STORAGE_STATE_DIR/profile-state.after"
    if ! cmp -s "$STORAGE_STATE_DIR/profile-state.before" "$STORAGE_STATE_DIR/profile-state.after"; then
      rollback_storage_cutover
      fail "Hermes profile/config inventory changed during storage cutover"
    fi
  fi
  if ! storage_restore_gateway_units "$units_file"; then
    rollback_storage_cutover
    fail "Hermes data migrated, but one or more gateway services could not be restored"
  fi

  begin_stage "storage_cleanup"
  [[ -z "$backup_runtime" ]] || rm -rf --one-file-system "$backup_runtime"
  [[ -z "$backup_home" ]] || rm -rf --one-file-system "$backup_home"
  sync
  status="$([[ "$migrate" -eq 1 ]] && echo migrated || echo initialized)"
  storage_write_summary "$status" "$(( $(now_ms) - started ))" "$bytes" "$migrated_csv" "${source_fs:-/}" "$STORAGE_TARGET_FS"
  STORAGE_ROLLBACK_ARMED=false
  STORAGE_CREATED_GUARDS=()
  unset -f rollback_storage_cutover
  log "Hermes storage $status at $target_base"
}

write_default_template() {
  local dst="$1"
  # JSON is valid YAML 1.2. Keeping the built-in template JSON-shaped lets
  # fresh target hosts render it with stdlib Python even when PyYAML is absent.
  cat > "$dst" <<'JSON'
{
  "parameters": {
    "targetUser": {"required": true},
    "env.AOPS_BOT_TOKEN": {"required": true},
    "env.AOPS_BOT_URL": {"required": false, "default": "http://aops-bot.internal"},
    "env.CLAWHUB_REGISTRY": {"required": false, "default": "http://tec01.internal/clawhub"},
    "hindsight.bank_id": {"required": false}
  },
  "bundle": {
    "url": "http://tec01.internal/hermes/packages/hermes-aops-offline-bundle.tar.gz",
    "sha256": "<sha256>"
  },
  "profile": {
    "name": "",
    "namePolicy": "target-user-sequence",
    "noBundledSkills": true,
    "setActive": false
  },
  "skills": {
    "preinstall": []
  },
  "config": {
    "env": {
      "AOPS_BOT_TOKEN": "${env.AOPS_BOT_TOKEN}",
      "AOPS_BOT_URL": "${env.AOPS_BOT_URL}",
      "AOPS_HOME_CHANNEL": "${env.AOPS_HOME_CHANNEL}",
      "AOPS_HOME_CHANNEL_NAME": "${env.AOPS_HOME_CHANNEL_NAME}",
      "AOPS_BASE_URL": "${env.AOPS_BASE_URL}",
      "AOPS_API_KEY": "${env.AOPS_API_KEY}",
      "CLAWHUB_REGISTRY": "${env.CLAWHUB_REGISTRY}",
      "MODEL_GATEWAY_API_KEY": "${env.MODEL_GATEWAY_API_KEY}"
    },
    "configYaml": {
      "model": {
        "provider": "custom",
        "model": "${configYaml.model.model}",
        "default": "${configYaml.model.model}",
        "base_url": "${configYaml.model.base_url}",
        "api_mode": "openai",
        "api_key_env": "MODEL_GATEWAY_API_KEY",
        "supports_vision": true
      },
      "memory": {"provider": "hindsight"},
      "approvals": {"mode": "off"},
      "display": {"busy_input_mode": "queue", "language": "zh"},
      "checkpoints": {"enabled": true},
      "platforms": {
        "aops": {
          "enabled": true,
          "extra": {
            "base_url": "${env.AOPS_BOT_URL}"
          }
        }
      },
      "platform_toolsets": {
        "cli": [
          "terminal",
          "file",
          "code_execution",
          "skills",
          "todo",
          "memory",
          "session_search",
          "clarify",
          "delegation",
          "cronjob",
          "messaging",
          "vision"
        ],
        "aops": [
          "terminal",
          "file",
          "code_execution",
          "skills",
          "todo",
          "memory",
          "session_search",
          "clarify",
          "delegation",
          "cronjob",
          "messaging",
          "vision"
        ]
      },
      "aops": {
        "toolsets": {
          "disabled": [
            "browser",
            "video",
            "image_gen",
            "video_gen",
            "x_search",
            "moa",
            "tts",
            "homeassistant",
            "spotify",
            "discord",
            "discord_admin",
            "yuanbao",
            "computer_use",
            "web"
          ],
          "unsupportedReasons": {
            "web": "内网 Linux 服务器通常无法访问公网搜索/抓取服务，默认关闭。",
            "browser": "需要可用浏览器或浏览器自动化运行环境，纯终端服务器默认关闭。",
            "computer_use": "仅适用于 macOS 桌面控制，Linux 纯终端不可用。"
          }
        }
      }
    },
    "hindsight": {
      "mode": "local_external",
      "api_url": "${hindsight.api_url}",
      "api_key": "${hindsight.api_key}",
      "bank_id": "${hindsight.bank_id}",
      "budget": "mid",
      "timeout": 120,
      "idle_timeout": 300
    },
    "userMemory": "# 用户偏好\n\n请优先使用中文回答，保持专业、清晰、简洁。\n",
    "soul": "# Agent Identity\n\nYou are a focused AOPS operations agent.\n"
  },
  "options": {
    "installGatewayService": true,
    "startGatewayAfterInstall": true,
    "restartGatewayAfterUpgrade": true,
    "restartOtherRunningProfilesAfterUpgrade": true,
    "overwriteExistingConfig": false,
    "overwriteFields": [],
    "overwriteSkills": true
  }
}
JSON
}

render_payload() {
  python3 - "$TEMPLATE_YAML" "$SETS_FILE" "$SKILLS_FILE" "$PAYLOAD_JSON" "$TASK_ID" <<'PY'
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

template_path, sets_path, skills_path, output_path, task_id = sys.argv[1:6]
template_text = Path(template_path).read_text(encoding="utf-8")
if yaml is not None:
    raw = yaml.safe_load(template_text) or {}
else:
    try:
        raw = json.loads(template_text)
    except Exception as exc:
        raise SystemExit(
            "PyYAML is required for non-JSON templates; install python3-yaml "
            f"or use the built-in/default JSON-compatible template: {exc}"
        )
if not isinstance(raw, dict):
    raise SystemExit("template root must be a mapping")

MISSING = object()
PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")

MARKDOWN_KEYS = {
    "soul",
    "SOUL.md",
    "userMemory",
    "user_memory",
    "userInstructions",
    "userMd",
    "USER.md",
}

def markdown_key(key):
    key = str(key or "")
    if key.startswith("config."):
        key = key[len("config."):]
    return key in MARKDOWN_KEYS

def decode_markdown_escapes(value):
    text = str(value)
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)

def parse_value(key, value):
    value = str(value)
    if markdown_key(key):
        return decode_markdown_escapes(value)
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "none"}:
        return None
    try:
        if value.startswith(("{", "[")):
            return json.loads(value)
    except Exception:
        pass
    try:
        if re.fullmatch(r"-?\d+", value):
            return int(value)
    except Exception:
        pass
    return value

def deep_set(obj, dotted, value):
    cur = obj
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value

def deep_get(obj, dotted, default=MISSING):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

try:
    set_items = json.loads(Path(sets_path).read_text(encoding="utf-8") or "[]")
except Exception as exc:
    raise SystemExit(f"failed to read --set arguments: {exc}")
if not isinstance(set_items, list):
    raise SystemExit("--set arguments file must be a JSON array")

overrides = {}
for line in set_items:
    if not isinstance(line, str):
        raise SystemExit("--set value must be a string")
    if not line:
        continue
    if "=" not in line:
        raise SystemExit(f"--set value must be KEY=VALUE: {line}")
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        raise SystemExit("--set key must not be empty")
    overrides[key] = parse_value(key, value)

values = {}
params = raw.get("parameters") or {}
if params and not isinstance(params, dict):
    raise SystemExit("parameters must be a mapping")
for key, meta in params.items():
    meta = meta or {}
    if not isinstance(meta, dict):
        raise SystemExit(f"parameters.{key} must be a mapping")
    if "default" in meta:
        values[str(key)] = meta["default"]
values.update(overrides)

for key, meta in params.items():
    meta = meta or {}
    if bool(meta.get("required")):
        value = values.get(str(key), MISSING)
        if value is MISSING or value in (None, ""):
            raise SystemExit(f"Missing required parameter: {key}")

payload = deepcopy(raw)
payload.pop("parameters", None)

def render(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            rendered = render(value)
            if rendered is not MISSING:
                out[key] = rendered
        return out
    if isinstance(node, list):
        out = []
        for value in node:
            rendered = render(value)
            if rendered is not MISSING:
                out.append(rendered)
        return out
    if isinstance(node, str):
        matches = list(PLACEHOLDER.finditer(node))
        if not matches:
            return node
        if len(matches) == 1 and matches[0].span() == (0, len(node)):
            return values.get(matches[0].group(1), MISSING)
        result = node
        for match in matches:
            value = values.get(match.group(1), "")
            result = result.replace(match.group(0), "" if value is None else str(value))
        return result
    return node

payload = render(payload)

# Backward-compatible skill preinstall placement:
# older runtimes only read config.skills.preinstall, while templates used by
# Tec01 may place the user-facing block at top-level skills.preinstall.
top_level_skills = payload.get("skills")
if isinstance(top_level_skills, dict):
    config_block = payload.setdefault("config", {})
    if isinstance(config_block, dict) and "skills" not in config_block:
        config_block["skills"] = deepcopy(top_level_skills)

# Apply overrides into their runtime destinations, even when the built-in
# template did not explicitly reference that key.
for key, value in overrides.items():
    if key == "targetUser":
        payload["targetUser"] = value
    elif key.startswith("env."):
        deep_set(payload, "config.env." + key[4:], value)
    elif key.startswith("configYaml."):
        deep_set(payload, "config.configYaml." + key[len("configYaml."):], value)
    elif key.startswith("hindsight."):
        deep_set(payload, "config.hindsight." + key[len("hindsight."):], value)
    elif key in {"userMemory", "soul"}:
        deep_set(payload, "config." + key, value)
    elif key.startswith(("profile.", "bundle.", "options.", "config.")):
        deep_set(payload, key, value)

skills = [line for line in Path(skills_path).read_text(encoding="utf-8").splitlines() if line]
if skills:
    payload["skillsZip"] = skills

if task_id:
    payload["taskId"] = task_id

target_user = deep_get(payload, "targetUser", "")
token = deep_get(payload, "config.env.AOPS_BOT_TOKEN", "")
if not target_user:
    raise SystemExit("targetUser is required")
if not token:
    raise SystemExit("env.AOPS_BOT_TOKEN is required")

Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

select_profile() {
  python3 - "$TARGET_HOME" "$TARGET_USER" "$PAYLOAD_JSON" "$PROFILE_JSON" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

home = Path(sys.argv[1])
target_user = sys.argv[2]
payload_path = Path(sys.argv[3])
out_path = Path(sys.argv[4])
payload = json.loads(payload_path.read_text(encoding="utf-8"))
token = (((payload.get("config") or {}).get("env") or {}).get("AOPS_BOT_TOKEN") or "").strip()
if not token:
    raise SystemExit("env.AOPS_BOT_TOKEN is required")

root = home / ".hermes"
profiles_root = root / "profiles"

def read_env_token(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "AOPS_BOT_TOKEN":
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""

entries = []
if (root / ".env").exists():
    entries.append(("default", root, read_env_token(root / ".env")))
if profiles_root.is_dir():
    for child in sorted(profiles_root.iterdir()):
        if child.is_dir():
            entries.append((child.name, child, read_env_token(child / ".env")))

matches = [(name, path) for name, path, value in entries if value == token]
if len(matches) > 1:
    names = ", ".join(name for name, _ in matches)
    raise SystemExit(f"Multiple profiles contain the same AOPS_BOT_TOKEN: {names}")

profile_cfg = payload.get("profile") or {}
explicit = str(profile_cfg.get("name") or "").strip()
agent_count = sum(1 for _, _, value in entries if value)

def sanitize_base(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    if not value or not re.match(r"^[a-z0-9]", value):
        value = "agent-" + re.sub(r"^-+", "", value)
    return value or "agent"

if matches:
    name, path = matches[0]
    action = "update"
else:
    action = "create"
    if explicit:
        name = explicit.lower()
        path = profiles_root / name
        if path.exists():
            raise SystemExit(f"profile.name={explicit!r} exists but has a different or missing AOPS_BOT_TOKEN")
    else:
        if agent_count == 0:
            name = "default"
            path = root
        else:
            base = sanitize_base(target_user)
            idx = agent_count
            while True:
                name = f"{base}-{idx}"
                path = profiles_root / name
                if not path.exists():
                    break
                idx += 1

profile_cfg["name"] = name
payload["profile"] = profile_cfg
payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
out_path.write_text(json.dumps({"profile": name, "action": action, "path": str(path)}, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

resolve_aops_owner_bank_plan() {
  local resolver="$WORK_DIR/resolve-aops-owner-banks.py"
  cat > "$resolver" <<'PY'
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

payload_path = Path(os.environ["PAYLOAD_JSON"])
profile_path = Path(os.environ["PROFILE_JSON"])
output_path = Path(os.environ["OWNER_BANK_PLAN_JSON"])
payload = json.loads(payload_path.read_text(encoding="utf-8"))
selected = json.loads(profile_path.read_text(encoding="utf-8"))
selected_profile = str(selected.get("profile") or "default")
selected_action = str(selected.get("action") or "")
default_create_lookup = selected_action == "create" and selected_profile == "default"
timeout = 5.0 if default_create_lookup else float(os.environ.get("AOPS_OWNER_LOOKUP_TIMEOUT", "5"))
attempts = 1 if default_create_lookup else max(1, int(os.environ.get("AOPS_OWNER_LOOKUP_ATTEMPTS", "3")))
payload_config = payload.get("config") or {}
payload_env = payload_config.get("env") or {} if isinstance(payload_config, dict) else {}
payload_hindsight = payload_config.get("hindsight") or {} if isinstance(payload_config, dict) else {}
manual_bank_id = ""
if isinstance(payload_hindsight, dict):
    manual_bank_id = str(payload_hindsight.get("bank_id") or payload_hindsight.get("bankId") or "").strip()

def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    except OSError:
        pass
    return values

home = Path.home()
root = home / ".hermes"
profiles_root = root / "profiles"

def profile_dir(name: str) -> Path:
    return root if name == "default" else profiles_root / name

safe_bank_id = re.compile(r"^[A-Za-z0-9_-]+$")

def current_bank_id(directory: Path) -> str:
    config_path = directory / "hindsight" / "config.json"
    old_bank_id = ""
    try:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            old_bank_id = str(existing.get("bank_id") or "").strip()
            if not old_bank_id:
                banks = existing.get("banks")
                hermes_bank = banks.get("hermes") if isinstance(banks, dict) else None
                if isinstance(hermes_bank, dict):
                    old_bank_id = str(hermes_bank.get("bankId") or "").strip()
    except Exception:
        pass
    return old_bank_id

def target_records() -> list[dict[str, str]]:
    records = [{"profile": selected_profile, "home": str(profile_dir(selected_profile))}]
    if selected_action == "update" and selected_profile == "default" and profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir()):
            if not child.is_dir():
                continue
            if not str(read_dotenv(child / ".env").get("AOPS_BOT_TOKEN") or "").strip():
                continue
            records.append({"profile": child.name, "home": str(child)})
    return records

def make_plan_record(record: dict[str, str], bank_id: str, *, owner: str | None, source: str) -> dict[str, object]:
    directory = Path(record["home"])
    return {
        "profile": record["profile"],
        "hermesHome": str(directory),
        "ownerUserId": owner,
        "bankId": bank_id,
        "previousBankId": current_bank_id(directory),
        "source": source,
    }

def write_plan(*, mode: str, profiles: list[dict[str, object]]) -> None:
    result = {
        "ok": True,
        "mode": mode,
        "selectedProfile": selected_profile,
        "profiles": profiles,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if manual_bank_id:
    if not safe_bank_id.fullmatch(manual_bank_id):
        raise SystemExit("hindsight.bank_id must contain only letters, digits, '-' or '_'")
    write_plan(
        mode="manual-default-all-profiles" if selected_action == "update" and selected_profile == "default" else "manual-current-profile",
        profiles=[make_plan_record(record, manual_bank_id, owner=None, source="manual") for record in target_records()],
    )
    raise SystemExit(0)

if selected_profile != "default":
    default_bank_id = current_bank_id(profile_dir("default"))
    if not safe_bank_id.fullmatch(default_bank_id):
        raise SystemExit(
            "Cannot configure named profile: default Hindsight bank_id is missing or invalid"
        )
    write_plan(
        mode="named-profile-default-bank",
        profiles=[make_plan_record(
            {"profile": selected_profile, "home": str(profile_dir(selected_profile))},
            default_bank_id,
            owner=None,
            source="default-config-bank",
        )],
    )
    raise SystemExit(0)

payload_token = str(payload_env.get("AOPS_BOT_TOKEN") or "").strip()
payload_url = str(payload_env.get("AOPS_BOT_URL") or "").strip()
if not payload_token:
    raise SystemExit("AOPS owner lookup requires config.env.AOPS_BOT_TOKEN")
if not payload_url:
    raise SystemExit("AOPS owner lookup requires config.env.AOPS_BOT_URL")

def lookup_owner(base_url: str, token: str) -> str:
    endpoint = base_url.rstrip("/") + "/other/aops/bot-token/owner-user"
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("AOPS_BOT_URL must be an absolute http(s) URL")
    body = json.dumps({"bot_token": token}).encode("utf-8")
    last_error = "owner lookup failed"
    for attempt in range(attempts):
        retry_after = None
        try:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status_code = int(response.getcode())
                response_body = response.read().decode("utf-8", errors="replace")
            parsed_body = json.loads(response_body)
            if status_code == 200 and isinstance(parsed_body, dict) and parsed_body.get("status") == 200:
                data = parsed_body.get("data")
                owner = data.get("owner_user_id") if isinstance(data, dict) else None
                if isinstance(owner, str) and owner.strip():
                    return owner.strip()
                raise RuntimeError("owner lookup returned an empty owner_user_id")
            message = parsed_body.get("msg") if isinstance(parsed_body, dict) else "invalid response"
            raise RuntimeError(f"owner lookup returned HTTP {status_code}: {str(message)[:160]}")
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            try:
                response_body = exc.read().decode("utf-8", errors="replace")
                parsed_body = json.loads(response_body)
                message = parsed_body.get("msg") if isinstance(parsed_body, dict) else response_body
            except Exception:
                message = str(exc.reason)
            last_error = f"owner lookup returned HTTP {status_code}: {str(message)[:160]}"
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            retryable = status_code in {408, 429} or status_code >= 500
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"owner lookup network error: {str(exc)[:160]}"
            retryable = True
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"owner lookup returned invalid JSON: {str(exc)[:160]}"
            retryable = False
        except RuntimeError as exc:
            last_error = str(exc)
            retryable = False
        if not retryable or attempt + 1 >= attempts:
            break
        try:
            delay = min(30.0, max(0.0, float(retry_after))) if retry_after else float(2 ** attempt)
        except (TypeError, ValueError):
            delay = float(2 ** attempt)
        time.sleep(delay)
    raise RuntimeError(last_error)

if selected_action == "update" and selected_profile == "default":
    try:
        owner = lookup_owner(payload_url, payload_token)
        if not safe_bank_id.fullmatch(owner):
            raise RuntimeError("owner lookup returned an unsupported owner_user_id")
        bank_id = f"aops-tec01-{owner}"
        source = "default-owner-api"
    except Exception as exc:
        bank_id = current_bank_id(profile_dir("default"))
        if not safe_bank_id.fullmatch(bank_id):
            raise SystemExit(
                "AOPS owner lookup failed and default Hindsight bank_id is unavailable or invalid"
            ) from exc
        owner = None
        source = "default-config-fallback"
    write_plan(
        mode="default-all-profiles",
        profiles=[make_plan_record(record, bank_id, owner=owner, source=source) for record in target_records()],
    )
else:
    # New default profile creation is deliberately fail-closed: unlike
    # updates, it has no existing bank to use as a safe fallback.
    owner = lookup_owner(payload_url, payload_token)
    if not safe_bank_id.fullmatch(owner):
        raise SystemExit("AOPS owner lookup returned an unsupported owner_user_id")
    write_plan(
        mode="current-profile",
        profiles=[make_plan_record(record, f"aops-tec01-{owner}", owner=owner, source="owner-api") for record in target_records()],
    )
PY
  chmod 700 "$resolver"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$resolver"
  fi
  run_as_target "PAYLOAD_JSON=$(shell_quote "$PAYLOAD_JSON") PROFILE_JSON=$(shell_quote "$PROFILE_JSON") OWNER_BANK_PLAN_JSON=$(shell_quote "$OWNER_BANK_PLAN_JSON") AOPS_OWNER_LOOKUP_TIMEOUT=$(shell_quote "${AOPS_OWNER_LOOKUP_TIMEOUT:-10}") AOPS_OWNER_LOOKUP_ATTEMPTS=$(shell_quote "${AOPS_OWNER_LOOKUP_ATTEMPTS:-3}") python3 $(shell_quote "$resolver")"
}

inject_current_owner_bank_into_payload() {
  python3 - "$PAYLOAD_JSON" "$OWNER_BANK_PLAN_JSON" "$PROFILE_NAME" <<'PY'
import json
import sys
from pathlib import Path

payload_path = Path(sys.argv[1])
plan_path = Path(sys.argv[2])
profile = sys.argv[3]
payload = json.loads(payload_path.read_text(encoding="utf-8"))
plan = json.loads(plan_path.read_text(encoding="utf-8"))
records = plan.get("profiles") if isinstance(plan, dict) else []
record = next((item for item in records if isinstance(item, dict) and item.get("profile") == profile), None)
if not isinstance(record, dict) or not str(record.get("bankId") or "").strip():
    raise SystemExit(f"AOPS owner bank plan has no bank for current profile {profile}")
config = payload.setdefault("config", {})
if not isinstance(config, dict):
    raise SystemExit("payload.config must be an object")
hindsight = config.setdefault("hindsight", {})
if not isinstance(hindsight, dict):
    raise SystemExit("payload.config.hindsight must be an object")
hindsight["bank_id"] = str(record["bankId"])
hindsight["bank_id_template"] = ""
payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

sync_hindsight_owner_banks() {
  local synchronizer="$WORK_DIR/sync-hindsight-owner-banks.py"
  cat > "$synchronizer" <<'PY'
import json
import os
import tempfile
import time
from pathlib import Path

plan_path = Path(os.environ["OWNER_BANK_PLAN_JSON"])
result_path = Path(os.environ["OWNER_BANK_SYNC_RESULT_JSON"])
plan = json.loads(plan_path.read_text(encoding="utf-8"))
records = plan.get("profiles") if isinstance(plan, dict) else None
if not isinstance(records, list) or not records:
    raise SystemExit("AOPS owner bank plan is empty")

stamp = time.strftime("%Y%m%d-%H%M%S")
prepared = []
for record in records:
    if not isinstance(record, dict):
        raise SystemExit("AOPS owner bank plan contains an invalid profile")
    profile = str(record.get("profile") or "").strip()
    home = Path(str(record.get("hermesHome") or "")).expanduser()
    bank_id = str(record.get("bankId") or "").strip()
    if not profile or not str(home) or not bank_id:
        raise SystemExit("AOPS owner bank plan contains an incomplete profile")
    path = home / "hindsight" / "config.json"
    original = path.read_bytes() if path.exists() else None
    try:
        config = json.loads(original.decode("utf-8")) if original is not None else {}
    except Exception as exc:
        raise SystemExit(f"invalid Hindsight config for profile {profile}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"invalid Hindsight config root for profile {profile}")
    updated = dict(config)
    updated["bank_id"] = bank_id
    # Static owner banks must win over all historic templates.
    updated["bank_id_template"] = ""
    banks = updated.get("banks")
    banks = dict(banks) if isinstance(banks, dict) else {}
    hermes = banks.get("hermes")
    hermes = dict(hermes) if isinstance(hermes, dict) else {}
    hermes["bankId"] = bank_id
    hermes.setdefault("budget", updated.get("budget") or updated.get("recall_budget") or "mid")
    hermes.setdefault("enabled", True)
    banks["hermes"] = hermes
    updated["banks"] = banks
    rendered = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    prepared.append({
        "profile": profile,
        "path": path,
        "bankId": bank_id,
        "ownerUserId": str(record.get("ownerUserId") or ""),
        "original": original,
        "rendered": rendered,
        "changed": original != rendered,
    })

written = []
try:
    for item in prepared:
        if not item["changed"]:
            continue
        path = item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if item["original"] is not None:
            backup_path = path.with_name(f"{path.name}.bank.bak.{stamp}")
            backup_path.write_bytes(item["original"])
            backup = str(backup_path)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.bank.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(item["rendered"])
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        item["backup"] = backup
        written.append(item)
except Exception:
    for item in reversed(written):
        path = item["path"]
        if item["original"] is None:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            path.write_bytes(item["original"])
    raise

changed = []
unchanged = []
for item in prepared:
    target = changed if item["changed"] else unchanged
    target.append({
        "profile": item["profile"],
        "ownerUserId": item["ownerUserId"],
        "bankId": item["bankId"],
        "path": str(item["path"]),
        "backup": item.get("backup"),
    })
result = {"ok": True, "changedProfiles": changed, "unchangedProfiles": unchanged}
result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
  chmod 700 "$synchronizer"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$synchronizer"
  fi
  run_as_target "OWNER_BANK_PLAN_JSON=$(shell_quote "$OWNER_BANK_PLAN_JSON") OWNER_BANK_SYNC_RESULT_JSON=$(shell_quote "$OWNER_BANK_SYNC_RESULT_JSON") python3 $(shell_quote "$synchronizer")"
}

owner_bank_sync_changed_for_profile() {
  local profile="$1"
  python3 - "$OWNER_BANK_SYNC_RESULT_JSON" "$profile" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
changed = payload.get("changedProfiles") if isinstance(payload, dict) else []
print("true" if any(isinstance(item, dict) and item.get("profile") == sys.argv[2] for item in (changed or [])) else "false")
PY
}

write_changed_owner_bank_profiles() {
  local output="$1"
  python3 - "$OWNER_BANK_SYNC_RESULT_JSON" "$PROFILE_NAME" "$output" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
current = sys.argv[2]
output = Path(sys.argv[3])
try:
    payload = json.loads(source.read_text(encoding="utf-8"))
except Exception:
    payload = {}
profiles = []
for item in payload.get("changedProfiles") or []:
    if isinstance(item, dict):
        profile = str(item.get("profile") or "")
        if profile and profile != current:
            profiles.append(profile)
output.write_text(json.dumps(profiles, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

mark_payload_mode() {
  local upgrade="$1"
  python3 - "$PAYLOAD_JSON" "$upgrade" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
upgrade = sys.argv[2].lower() == "true"
payload = json.loads(path.read_text(encoding="utf-8"))
options = payload.setdefault("options", {})
if not isinstance(options, dict):
    options = {}
    payload["options"] = options
options["upgrade"] = upgrade
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

apply_sync_other_profiles_cli_override() {
  [[ -n "$SYNC_OTHER_PROFILES_CLI" ]] || return 0
  python3 - "$PAYLOAD_JSON" "$SYNC_OTHER_PROFILES_CLI" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = sys.argv[2] == "true"
payload = json.loads(path.read_text(encoding="utf-8"))
options = payload.setdefault("options", {})
if not isinstance(options, dict):
    raise SystemExit("payload.options must be an object")
options["syncOtherProfiles"] = value
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

build_profile_sync_payload() {
  local output_payload="$1"
  local output_profiles="$2"
  python3 - "$PAYLOAD_JSON" "$output_payload" "$output_profiles" "$PROFILE_SYNC_SUMMARY_JSON" "$PROFILE_NAME" "$TARGET_HOME" <<'PY'
import copy
import json
import sys
from pathlib import Path

source_path, output_path, profiles_path, summary_path, selected_profile, target_home = sys.argv[1:]
payload = json.loads(Path(source_path).read_text(encoding="utf-8"))
config = payload.get("config") or {}
options = payload.get("options") or {}
if not isinstance(config, dict) or not isinstance(options, dict):
    raise SystemExit("payload config/options must be objects")

overwrite_all = bool(options.get("overwriteExistingConfig", options.get("overwrite", False)))
overwrite_fields = options.get("overwriteFields") or []
if not isinstance(overwrite_fields, list) or not all(isinstance(item, str) and item.strip() for item in overwrite_fields):
    raise SystemExit("options.overwriteFields must be a list of non-empty strings")

allowed_roots = {
    "env", "configYaml", "config_yaml", "aops", "modelGateway", "hindsight",
    "userInstructions", "userMemory", "soul",
}

def get_path(root, parts):
    cur = root
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return copy.deepcopy(cur), True

def set_path(root, parts, value):
    cur = root
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value

sync_config = copy.deepcopy(config) if overwrite_all else {}
if not overwrite_all:
    for field in overwrite_fields:
        parts = field.split(".")
        if not parts or parts[0] not in allowed_roots:
            continue
        value, present = get_path(config, parts)
        if present:
            set_path(sync_config, parts, value)

protected = []
def remove_path(root, parts, label):
    cur = root
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict) and parts[-1] in cur:
        cur.pop(parts[-1], None)
        protected.append(label)

remove_path(sync_config, ["env", "AOPS_BOT_TOKEN"], "env.AOPS_BOT_TOKEN")
remove_path(sync_config, ["aops", "AOPS_BOT_TOKEN"], "aops.AOPS_BOT_TOKEN")
remove_path(sync_config, ["configYaml", "platforms", "aops", "token"], "configYaml.platforms.aops.token")
remove_path(sync_config, ["configYaml", "gateway", "platforms", "aops", "token"], "configYaml.gateway.platforms.aops.token")
remove_path(sync_config, ["config_yaml", "platforms", "aops", "token"], "configYaml.platforms.aops.token")
remove_path(sync_config, ["config_yaml", "gateway", "platforms", "aops", "token"], "configYaml.gateway.platforms.aops.token")

sync_overwrite_fields = []
for field in overwrite_fields:
    if field in {
        "env.AOPS_BOT_TOKEN", "aops.AOPS_BOT_TOKEN",
        "configYaml.platforms.aops.token", "configYaml.gateway.platforms.aops.token",
        "config_yaml.platforms.aops.token", "config_yaml.gateway.platforms.aops.token",
    }:
        if field not in protected:
            protected.append(field.replace("config_yaml", "configYaml"))
        continue
    if field.split(".", 1)[0] in allowed_roots:
        sync_overwrite_fields.append(field)

sync_payload = {
    "config": sync_config,
    "options": {
        "upgrade": True,
        "overwriteExistingConfig": overwrite_all,
        "overwriteFields": sync_overwrite_fields,
    },
}
Path(output_path).write_text(json.dumps(sync_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

home = Path(target_home)
root = home / ".hermes"
profiles_root = root / "profiles"
profiles = ["default"]
seen_profiles = {"default"}
warnings = []
if profiles_root.is_dir():
    base = profiles_root.resolve()
    for child in sorted(profiles_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            child.resolve().relative_to(base)
        except ValueError:
            continue
        if child.name == "default":
            warning = {
                "code": "reserved_default_profile_directory_ignored",
                "profile": "default",
                "path": str(child),
                "message": "ignored reserved named-profile directory; default uses ~/.hermes",
            }
            warnings.append(warning)
            print(
                f"[WARN] Ignoring reserved profile directory {child}; "
                "default profile uses ~/.hermes",
                file=sys.stderr,
            )
            continue
        if child.name in seen_profiles:
            continue
        seen_profiles.add(child.name)
        profiles.append(child.name)
Path(profiles_path).write_text(json.dumps(profiles, ensure_ascii=False) + "\n", encoding="utf-8")

summary = {
    "enabled": True,
    "selectedProfile": selected_profile,
    "protectedFields": sorted(set(protected)),
    "profiles": [],
    "failedProfiles": [],
    "warnings": warnings,
}
Path(summary_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

profile_sync_unique_profiles() {
  python3 - "$PROFILE_SYNC_PROFILES_JSON" "$PROFILE_SYNC_SUMMARY_JSON" <<'PY'
import json
import sys
from pathlib import Path

profiles_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
if not isinstance(profiles, list):
    raise SystemExit("profile sync list must be an array")

try:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
except Exception:
    summary = {}
warnings = summary.setdefault("warnings", [])
seen = set()
for raw in profiles:
    profile = str(raw or "").strip()
    if not profile:
        continue
    if profile in seen:
        warning = {
            "code": "duplicate_profile_skipped",
            "profile": profile,
            "message": "duplicate profile entry skipped during synchronized update",
        }
        if warning not in warnings:
            warnings.append(warning)
        print(
            f"[WARN] duplicate_profile_skipped profile={profile}",
            file=sys.stderr,
        )
        continue
    seen.add(profile)
    print(profile)
summary_path.write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

record_profile_sync_result() {
  local profile="$1"
  local config_status="$2"
  local gateway_action="$3"
  local gateway_status="$4"
  local duration_ms="$5"
  local message="${6:-}"
  python3 - "$PROFILE_SYNC_SUMMARY_JSON" "$profile" "$config_status" "$gateway_action" "$gateway_status" "$duration_ms" "$message" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
profile, config_status, gateway_action, gateway_status, duration_ms, message = sys.argv[2:]
data = json.loads(path.read_text(encoding="utf-8"))
entries = data.setdefault("profiles", [])
entry = next((item for item in entries if item.get("profile") == profile), None)
if entry is None:
    entry = {"profile": profile}
    entries.append(entry)
entry.update({
    "configStatus": config_status,
    "gatewayAction": gateway_action or None,
    "gatewayStatus": gateway_status or None,
    "durationMs": int(entry.get("durationMs") or 0) + int(duration_ms or 0),
})
if message:
    entry["message"] = message
if config_status == "failed" or gateway_status == "failed":
    failed = data.setdefault("failedProfiles", [])
    if profile not in failed:
        failed.append(profile)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

profile_sync_has_failures() {
  python3 - "$PROFILE_SYNC_SUMMARY_JSON" <<'PY'
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("true" if data.get("failedProfiles") else "false")
except Exception:
    print("true")
PY
}

remote_config_supports_profile() {
  [[ -x "$INSTALL_DIR/venv/bin/python" ]] || return 1
  run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --help | grep -q -- '--profile'" >/dev/null 2>&1
}

profile_arg_for() {
  local profile="$1"
  local profile_arg=""
  if [[ "$profile" != "default" ]]; then
    profile_arg="-p $(shell_quote "$profile")"
  fi
  printf '%s' "$profile_arg"
}

profile_home_for() {
  local profile="$1"
  if [[ "$profile" == "default" ]]; then
    printf '%s' "$TARGET_HOME/.hermes"
  else
    printf '%s' "$TARGET_HOME/.hermes/profiles/$profile"
  fi
}

ensure_aops_pdf_capabilities_for_profile() {
  local profile="$1"
  local profile_dir
  profile_dir="$(profile_home_for "$profile")"
  log "Enabling bundled PDF capabilities for profile $profile" >&2
  run_as_target "$(shell_quote "$INSTALL_DIR/venv/bin/python") -m tools.aops_pdf_setup --config $(shell_quote "$profile_dir/config.yaml")"
}

ensure_gateway_lazy_installs_disabled_for_profile() {
  local profile="$1"
  if json_bool options.allowGatewayLazyInstalls false || json_bool options.allowLazyInstalls false; then
    log "Leaving gateway lazy installs enabled for profile $profile because payload option requested it"
    printf 'false\n'
    return 0
  fi

  local profile_dir
  profile_dir="$(profile_home_for "$profile")"
  local py="$INSTALL_DIR/venv/bin/python"
  [[ -x "$py" ]] || py="python3"

  run_as_target "$(shell_quote "$py") - $(shell_quote "$profile_dir") <<'PY'
import json
import sys
from pathlib import Path

profile_dir = Path(sys.argv[1])
config_path = profile_dir / 'config.yaml'
changed = False

try:
    import yaml
except Exception as exc:
    print(json.dumps({
        'changed': False,
        'error': f'PyYAML unavailable; cannot disable gateway lazy installs: {exc}',
    }, ensure_ascii=False))
    raise SystemExit(0)

try:
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        cfg = loaded if isinstance(loaded, dict) else {}
    else:
        cfg = {}
except Exception:
    cfg = {}

security = cfg.get('security')
if not isinstance(security, dict):
    security = {}
    cfg['security'] = security

if security.get('allow_lazy_installs') is not False:
    security['allow_lazy_installs'] = False
    changed = True
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )

print(json.dumps({
    'changed': changed,
    'path': str(config_path),
    'security.allow_lazy_installs': security.get('allow_lazy_installs'),
}, ensure_ascii=False))
PY"
}

append_restart_summary() {
  local bucket="$1"
  local profile="$2"
  local message="$3"
  [[ -n "${RESTART_SUMMARY_JSON:-}" ]] || return 0
  python3 - "$RESTART_SUMMARY_JSON" "$bucket" "$profile" "$message" <<'PY' || true
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
bucket, profile, message = sys.argv[2:5]
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
for key in ("restartedProfiles", "skippedProfiles", "failedProfiles", "diagnostics"):
    data.setdefault(key, [])
entry = {"profile": profile, "message": message}
if bucket == "diagnostics":
    data["diagnostics"].append(entry)
else:
    data.setdefault(bucket, []).append(entry)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

ensure_gateway_service_installed() {
  local action="$1"
  local profile="$2"
  local profile_arg
  profile_arg="$(profile_arg_for "$profile")"
  if json_bool options.installGatewayService true; then
    begin_stage "gateway_install_service"
    log "Ensuring Hermes gateway service is installed for $TARGET_USER profile $profile"
    run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes $profile_arg gateway install --force --no-start-now --start-on-login"
    storage_install_mount_guards || fail "Failed to install Hermes data mount guard for profile $profile"
  fi
  begin_stage "gateway_${action}"
  controlled_gateway_lifecycle "$profile" "$action" 240 true
}

write_gateway_lifecycle_controller() {
  local dst="$1"
  cat > "$dst" <<'PY'
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

home = Path.home()
profile = os.environ.get("PROFILE") or "default"
action = os.environ.get("ACTION") or "restart"
try:
    max_wait = max(1, int(float(os.environ.get("MAX_WAIT") or "240")))
except Exception:
    max_wait = 240
summary_json = os.environ.get("SUMMARY_JSON") or ""
require_aops = str(os.environ.get("REQUIRE_AOPS") or "").strip().lower() in {"1", "true", "yes", "on"}
os.environ["PATH"] = f"{home}/.local/bin:" + os.environ.get("PATH", "")
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus")
root = home / ".hermes"
profiles_root = root / "profiles"

def service_name(profile: str) -> str:
    return "hermes-gateway.service" if profile == "default" else f"hermes-gateway-{profile}.service"

def profile_home(profile: str) -> Path:
    if profile == "default":
        return root
    return profiles_root / profile

unit = service_name(profile)
profile_dir = profile_home(profile)
profile_args = [] if profile == "default" else ["-p", profile]
lifecycle_started = time.monotonic()

def append_summary(bucket: str, message: str, **extra) -> None:
    if not summary_json:
        return
    try:
        path = Path(summary_json)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for key in ("restartedProfiles", "skippedProfiles", "failedProfiles", "diagnostics"):
            data.setdefault(key, [])
        entry = {
            "profile": profile,
            "message": message,
            "durationMs": int((time.monotonic() - lifecycle_started) * 1000),
        }
        entry.update({key: value for key, value in extra.items() if value is not None})
        data[bucket].append(entry)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

def run(cmd, *, timeout=30, check=False, capture=False):
    kwargs = {
        "timeout": timeout,
        "check": check,
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.run(cmd, **kwargs)

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def state():
    return read_json(profile_dir / "gateway_state.json")

def pid_record():
    return read_json(profile_dir / "gateway.pid")

def pid_alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def current_pid():
    record = pid_record()
    pid = record.get("pid") or state().get("pid")
    return int(pid or 0) if str(pid or "").isdigit() else 0

def systemd_props():
    try:
        result = run(
            ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "Result", "-p", "ExecMainStatus"],
            timeout=8,
            capture=True,
        )
        props = {}
        for line in (result.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        return props
    except Exception:
        return {}

def systemd_main_pid(props=None) -> int:
    props = props if props is not None else systemd_props()
    raw = str(props.get("MainPID") or "")
    return int(raw) if raw.isdigit() else 0

def start_limited(props) -> bool:
    return str(props.get("Result", "")).lower() == "start-limit-hit" or str(props.get("SubState", "")).lower() == "start-limit-hit"

def process_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""

def child_pids(pid: int):
    try:
        children = Path(f"/proc/{int(pid)}/task/{int(pid)}/children").read_text(encoding="utf-8").split()
        return [int(item) for item in children if item.isdigit()]
    except Exception:
        return []

def process_tree_lines(pid: int, depth: int = 0, seen=None):
    seen = seen or set()
    try:
        pid = int(pid)
    except Exception:
        return []
    if pid <= 0 or pid in seen or not pid_alive(pid):
        return []
    seen.add(pid)
    cmdline = process_cmdline(pid) or "(cmdline unavailable)"
    lines = [f"{'  ' * depth}{pid}: {cmdline}"]
    for child in child_pids(pid):
        lines.extend(process_tree_lines(child, depth=depth + 1, seen=seen))
    return lines

def process_tree_text(pid: int) -> str:
    lines = process_tree_lines(pid)
    return "\n".join(lines) if lines else "(no live process tree)"

def detect_startup_blocker(main_pid: int) -> str:
    text = process_tree_text(main_pid)
    lowered = text.lower()
    if " pip install " in lowered or " uv pip install " in lowered or " ensurepip " in lowered:
        return "startup is blocked by a lazy dependency install (pip/uv/ensurepip)"
    if "discord.py" in lowered or "brotlicffi" in lowered:
        return "startup is resolving Discord lazy dependencies"
    return ""

def aops_status(st: dict) -> tuple[str, str]:
    platforms = st.get("platforms")
    if not isinstance(platforms, dict):
        return "missing", "gateway_state.json has no platforms map"
    aops = platforms.get("aops")
    if not isinstance(aops, dict):
        return "missing", "AOPS platform is absent from gateway_state.json"
    state = str(aops.get("state") or "").strip().lower()
    error = str(aops.get("error_message") or aops.get("error_code") or "").strip()
    return state or "unknown", error

def print_file(label: str, path: Path) -> None:
    print(f"--- {label}: {path} ---")
    try:
        print(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"(unavailable: {exc})")

def diagnostics(reason: str) -> None:
    append_summary("diagnostics", reason)
    print(f"⚠ Gateway diagnostics for profile {profile}: {reason}", file=sys.stderr)
    props = systemd_props()
    main_pid = systemd_main_pid(props)
    if main_pid:
        blocker = detect_startup_blocker(main_pid)
        print(f"--- process tree for systemd MainPID {main_pid} ---")
        print(process_tree_text(main_pid))
        if blocker:
            print(f"⚠ Detected startup blocker: {blocker}", file=sys.stderr)
    commands = [
        ["hermes", *profile_args, "gateway", "status"],
        ["systemctl", "--user", "status", unit, "--no-pager", "-l"],
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "ExecMainStatus", "-p", "Result", "-p", "RestartUSec", "-p", "TimeoutStopUSec"],
        ["systemctl", "--user", "cat", unit],
        ["journalctl", "--user", "-u", unit, "-n", "100", "--no-pager", "-l"],
    ]
    for cmd in commands:
        print(f"--- {' '.join(cmd)} ---")
        try:
            result = run(cmd, timeout=15, capture=True)
            print(result.stdout or "")
        except Exception as exc:
            print(f"(failed: {exc})")
    print_file("gateway_state", profile_dir / "gateway_state.json")
    print_file("gateway_pid", profile_dir / "gateway.pid")

def wait_ready() -> bool:
    deadline = time.monotonic() + max_wait
    next_progress = time.monotonic()
    warned_60 = False
    runtime_ready_at = None
    while time.monotonic() < deadline:
        st = state()
        pid = int(st.get("pid") or current_pid() or 0)
        gateway_state = str(st.get("gateway_state") or "")
        props = systemd_props()
        active_state = str(props.get("ActiveState") or "").lower()
        main_pid = systemd_main_pid(props)
        main_pid_alive = pid_alive(main_pid)
        if gateway_state == "running" and pid_alive(pid):
            if not require_aops:
                print(f"✓ Gateway profile {profile} runtime is running (PID {pid})")
                append_summary(
                    "restartedProfiles",
                    f"{action} completed; pid={pid}",
                    runtimeState="running",
                    aopsState=None,
                )
                return True
            aops_state, aops_detail = aops_status(st)
            if aops_state == "connected":
                print(f"✓ Gateway profile {profile} runtime is running with AOPS connected (PID {pid})")
                append_summary(
                    "restartedProfiles",
                    f"{action} completed; pid={pid}; aops=connected",
                    runtimeState="running",
                    aopsState="connected",
                )
                return True
            if runtime_ready_at is None:
                runtime_ready_at = time.monotonic()
                print(
                    f"⏳ Gateway profile {profile} runtime is running; "
                    f"waiting up to 15s for AOPS (state={aops_state})"
                )
            fatal_aops = aops_state in {"error", "failed", "startup_failed", "fatal"}
            if fatal_aops or time.monotonic() - runtime_ready_at >= 15:
                if fatal_aops:
                    warning = (
                        f"AOPS startup reported state={aops_state}; gateway runtime remains active "
                        f"({aops_detail or 'no detail'})"
                    )
                else:
                    warning = (
                        f"AOPS not connected within 15s; gateway will continue reconnecting "
                        f"(state={aops_state}; {aops_detail or 'no detail'})"
                    )
                print(f"⚠ {warning}", file=sys.stderr)
                append_summary(
                    "restartedProfiles",
                    f"{action} completed; pid={pid}; aops={aops_state}",
                    runtimeState="running",
                    aopsState=aops_state,
                    warning=warning,
                )
                append_summary("diagnostics", warning, runtimeState="running", aopsState=aops_state)
                return True
        else:
            runtime_ready_at = None
        if gateway_state == "startup_failed":
            diagnostics(f"startup_failed: {st.get('exit_reason') or 'unknown'}")
            append_summary("failedProfiles", "startup_failed")
            return False
        if start_limited(props):
            diagnostics("systemd start-limit-hit")
            append_summary("failedProfiles", "systemd start-limit-hit")
            return False
        if gateway_state in {"stopped", "unknown", ""} and active_state == "active" and main_pid_alive and main_pid != pid:
            blocker = detect_startup_blocker(main_pid)
            if blocker:
                diagnostics(
                    f"systemd is active with MainPID {main_pid}, but Hermes runtime state is stale "
                    f"(state={gateway_state or 'unknown'} pid={pid or 'unknown'}); {blocker}"
                )
                append_summary("failedProfiles", "startup blocked by lazy dependency install")
                return False
        now = time.monotonic()
        if not warned_60 and now > deadline - max_wait + 60:
            warned_60 = True
            print(
                f"⚠ Gateway profile {profile} not ready after 60s; "
                f"state={gateway_state or 'unknown'} pid={pid or 'unknown'} "
                f"systemd={active_state or 'unknown'} mainPid={main_pid or 'unknown'}; "
                f"continuing to wait up to {max_wait}s."
            )
        if now >= next_progress:
            print(
                f"⏳ Waiting for profile {profile}: state={gateway_state or 'unknown'} "
                f"pid={pid or 'unknown'} systemd={active_state or 'unknown'} "
                f"mainPid={main_pid or 'unknown'}"
            )
            next_progress = now + 30
        time.sleep(2)
    diagnostics(f"runtime did not become ready within {max_wait}s")
    append_summary("failedProfiles", f"timeout after {max_wait}s")
    return False

def wait_for_replacement(old_pid: int, timeout_seconds: int = 30) -> bool:
    deadline = time.monotonic() + timeout_seconds
    next_progress = time.monotonic()
    while time.monotonic() < deadline:
        st = state()
        candidate = int(st.get("pid") or current_pid() or 0)
        props = systemd_props()
        main_pid = systemd_main_pid(props)
        if candidate and candidate != old_pid and pid_alive(candidate):
            print(f"✓ Replacement gateway process detected for profile {profile} (PID {candidate})")
            return True
        if main_pid and main_pid != old_pid and pid_alive(main_pid):
            print(f"✓ Replacement systemd MainPID detected for profile {profile} (PID {main_pid})")
            return True
        if str(st.get("gateway_state") or "") == "startup_failed" or start_limited(props):
            return False
        now = time.monotonic()
        if now >= next_progress:
            print(
                f"⏳ Waiting for systemd replacement for profile {profile}: "
                f"oldPid={old_pid} mainPid={main_pid or 'unknown'}"
            )
            next_progress = now + 15
        time.sleep(1)
    return False

def running_replacement_after_timeout():
    st = state()
    candidate = int(st.get("pid") or current_pid() or 0)
    if (
        str(st.get("gateway_state") or "") == "running"
        and candidate
        and candidate != before_pid
        and pid_alive(candidate)
    ):
        return candidate
    return 0

def hard_systemd_restart() -> int:
    try:
        run(["systemctl", "--user", "reset-failed", unit], timeout=15)
        result = run(["systemctl", "--user", "restart", unit], timeout=90)
    except subprocess.TimeoutExpired:
        replacement_pid = running_replacement_after_timeout()
        if replacement_pid:
            warning = (
                f"systemctl restart {unit} timed out, but replacement runtime "
                f"is already running with PID {replacement_pid}"
            )
            print(f"⚠ {warning}", file=sys.stderr)
            append_summary(
                "diagnostics",
                warning,
                runtimeState="running",
            )
            return 0
        print(
            f"⚠ systemctl restart {unit} timed out and no running replacement "
            "runtime was detected",
            file=sys.stderr,
        )
        return 124
    if result.returncode != 0:
        print(f"⚠ systemctl restart {unit} returned {result.returncode}; falling back to hermes gateway start", file=sys.stderr)
        result = run(["hermes", *profile_args, "gateway", "start"], timeout=90)
    return result.returncode

def systemd_start() -> int:
    try:
        result = run(["systemctl", "--user", "start", unit], timeout=90)
    except subprocess.TimeoutExpired:
        replacement_pid = running_replacement_after_timeout()
        if replacement_pid:
            warning = (
                f"systemctl start {unit} timed out, but gateway runtime "
                f"is already running with PID {replacement_pid}"
            )
            print(f"⚠ {warning}", file=sys.stderr)
            append_summary(
                "diagnostics",
                warning,
                runtimeState="running",
            )
            return 0
        print(
            f"⚠ systemctl start {unit} timed out and no running gateway "
            "runtime was detected",
            file=sys.stderr,
        )
        return 124
    if result.returncode != 0:
        print(f"⚠ systemctl start {unit} returned {result.returncode}; falling back to hermes gateway start", file=sys.stderr)
        result = run(["hermes", *profile_args, "gateway", "start"], timeout=90)
    return result.returncode

before = state()
before_state = str(before.get("gateway_state") or "")
before_pid = current_pid()
print(f"[INFO] Gateway profile {profile}: action={action}, pre_state={before_state or 'unknown'}, pre_pid={before_pid or 'unknown'}")

if action == "start":
    rc = systemd_start()
    if rc != 0:
        diagnostics(f"start command failed with exit code {rc}")
        append_summary("failedProfiles", f"start command failed: {rc}")
        raise SystemExit(rc)
    raise SystemExit(0 if wait_ready() else 1)

if action != "restart":
    diagnostics(f"unsupported lifecycle action: {action}")
    append_summary("failedProfiles", f"unsupported lifecycle action: {action}")
    raise SystemExit(2)

if before_state in {"starting", "draining"}:
    print(f"⚠ Profile {profile} is already {before_state}; skip SIGUSR1 restart and wait for current transition.")
    append_summary("skippedProfiles", f"already {before_state}; waited instead of sending SIGUSR1")
    raise SystemExit(0 if wait_ready() else 1)

if before_state == "running" and pid_alive(before_pid):
    print(f"⏳ Sending graceful SIGUSR1 restart to profile {profile} PID {before_pid}")
    try:
        os.kill(before_pid, signal.SIGUSR1)
    except Exception as exc:
        print(f"⚠ SIGUSR1 failed for PID {before_pid}: {exc}; using systemd restart", file=sys.stderr)
        rc = hard_systemd_restart()
        if rc != 0:
            diagnostics(f"systemd restart failed with exit code {rc}")
            append_summary("failedProfiles", f"systemd restart failed: {rc}")
            raise SystemExit(rc)
        raise SystemExit(0 if wait_ready() else 1)
    try:
        active_agents = int(before.get("active_agents") or 0)
    except Exception:
        active_agents = 0
    drain_timeout = 185 if active_agents > 0 else 30
    drain_started = time.monotonic()
    drain_deadline = drain_started + drain_timeout
    next_drain_progress = drain_started + 15
    while pid_alive(before_pid) and time.monotonic() < drain_deadline:
        now = time.monotonic()
        if now >= next_drain_progress:
            print(
                f"⏳ Graceful restart draining profile {profile}: "
                f"elapsed={int(now - drain_started)}s activeAgents={active_agents} "
                f"timeout={drain_timeout}s"
            )
            next_drain_progress = now + 15
        time.sleep(1)
    if pid_alive(before_pid):
        print(
            f"⚠ Graceful restart for profile {profile} did not exit within "
            f"{drain_timeout}s; forcing systemd restart."
        )
    else:
        print(f"✓ Previous gateway PID {before_pid} exited; waiting for systemd replacement.")
        if wait_for_replacement(before_pid, 30):
            raise SystemExit(0 if wait_ready() else 1)
        print(f"⚠ No replacement process appeared for profile {profile} within 30s; forcing systemd restart.")
    rc = hard_systemd_restart()
    if rc != 0:
        diagnostics(f"systemd restart failed with exit code {rc}")
        append_summary("failedProfiles", f"systemd restart failed: {rc}")
        raise SystemExit(rc)
    raise SystemExit(0 if wait_ready() else 1)

print(f"⚠ Profile {profile} is not running (state={before_state or 'unknown'}); using systemd restart without SIGUSR1.")
rc = hard_systemd_restart()
if rc != 0:
    diagnostics(f"systemd restart failed with exit code {rc}")
    append_summary("failedProfiles", f"systemd restart failed: {rc}")
    raise SystemExit(rc)
raise SystemExit(0 if wait_ready() else 1)
PY
  chmod 700 "$dst"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$dst"
  fi
}

controlled_gateway_lifecycle() {
  local profile="$1"
  local action="$2"
  local max_wait="$3"
  local required="$4"
  local summary_path="${RESTART_SUMMARY_JSON:-}"
  local controller="$WORK_DIR/gateway-lifecycle.py"
  write_gateway_lifecycle_controller "$controller"
  if run_as_target "PROFILE=$(shell_quote "$profile") ACTION=$(shell_quote "$action") MAX_WAIT=$(shell_quote "$max_wait") SUMMARY_JSON=$(shell_quote "$summary_path") REQUIRE_AOPS=$(shell_quote "$required") python3 $(shell_quote "$controller")"; then
    return 0
  fi
  if [[ "$required" == "true" ]]; then
    return 1
  fi
  return 0
}

write_gateway_profile_selector() {
  local dst="$1"
  cat > "$dst" <<'PY'
import json
import os
import subprocess
from pathlib import Path

home = Path.home()
current_profile = os.environ.get("CURRENT_PROFILE") or "default"
summary_json = os.environ.get("SUMMARY_JSON") or ""
only_profiles_json = os.environ.get("ONLY_PROFILES_JSON") or ""
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus")
root = home / ".hermes"
profiles_root = root / "profiles"

try:
    only_profiles = set(json.loads(Path(only_profiles_json).read_text(encoding="utf-8"))) if only_profiles_json else set()
except Exception:
    only_profiles = set()

def profile_home(profile: str) -> Path:
    return root if profile == "default" else profiles_root / profile

def service_name(profile: str) -> str:
    return "hermes-gateway.service" if profile == "default" else f"hermes-gateway-{profile}.service"

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def append(bucket: str, profile: str, message: str) -> None:
    if not summary_json:
        return
    try:
        path = Path(summary_json)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for key in ("restartedProfiles", "skippedProfiles", "failedProfiles", "diagnostics"):
            data.setdefault(key, [])
        data[bucket].append({"profile": profile, "message": message})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

def service_exists(profile: str) -> bool:
    return (home / ".config" / "systemd" / "user" / service_name(profile)).exists()

def pid_alive(pid: int) -> bool:
    try:
        pid = int(pid or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def systemd_props(profile: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", service_name(profile), "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "Result"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
        )
        props = {}
        for line in (result.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        return props
    except Exception:
        return {}

candidates = [("default", root)]
if profiles_root.is_dir():
    for child in sorted(profiles_root.iterdir()):
        if child.is_dir():
            candidates.append((child.name, child))

for profile, directory in candidates:
    if profile == current_profile:
        continue
    if only_profiles and profile not in only_profiles:
        continue
    state = read_json(directory / "gateway_state.json")
    gateway_state = str(state.get("gateway_state") or "")
    props = systemd_props(profile)
    main_pid_raw = str(props.get("MainPID") or "")
    main_pid = int(main_pid_raw) if main_pid_raw.isdigit() else 0
    active_state = str(props.get("ActiveState") or "").lower()
    if gateway_state == "running":
        print(profile)
    elif active_state == "active" and pid_alive(main_pid):
        append("diagnostics", profile, f"systemd active mainPid={main_pid}; runtime state={gateway_state or 'unknown'}; recovering with hard restart")
        print(f"::recover::{profile}::systemd-active-runtime-{gateway_state or 'unknown'}")
    elif service_exists(profile) or (directory / "gateway.pid").exists():
        append("skippedProfiles", profile, f"state={gateway_state or 'unknown'}; not sending SIGUSR1")
        print(f"::skip::{profile}::{gateway_state or 'unknown'}")
PY
  chmod 700 "$dst"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$dst"
  fi
}

restart_other_running_profiles_after_upgrade() {
  [[ "${RUNTIME_CHANGED:-false}" == "true" ]] || return 0
  json_bool options.restartOtherRunningProfilesAfterUpgrade true || return 0

  begin_stage "gateway_restart_other_profiles"
  log "Restarting other running Hermes profile gateways after runtime upgrade"
  local selector="$WORK_DIR/select-running-profiles.py"
  write_gateway_profile_selector "$selector"
  run_as_target "CURRENT_PROFILE=$(shell_quote "$PROFILE_NAME") SUMMARY_JSON=$(shell_quote "${RESTART_SUMMARY_JSON:-}") python3 $(shell_quote "$selector")" | while IFS= read -r other_profile; do
    [[ -n "$other_profile" ]] || continue
    case "$other_profile" in
      ::skip::*)
        printf '[WARN] skipped profile gateway %s\n' "${other_profile#::skip::}"
        continue
        ;;
      ::recover::*)
        recovered="${other_profile#::recover::}"
        other_profile="${recovered%%::*}"
        log "Recovering Hermes gateway profile $other_profile from ${recovered#*::}"
        ;;
    esac
    log "Restarting Hermes gateway profile $other_profile"
    other_lazy_result="$WORK_DIR/gateway-lazy-installs-other.json"
    ensure_gateway_lazy_installs_disabled_for_profile "$other_profile" > "$other_lazy_result" || true
    record_lazy_installs_change_if_needed "$other_profile" "$other_lazy_result"
    controlled_gateway_lifecycle "$other_profile" "restart" 120 false || true
  done
}

restart_other_profiles_after_owner_bank_sync() {
  [[ "${RUNTIME_CHANGED:-false}" == "true" ]] && return 0
  local changed_profiles="$WORK_DIR/owner-bank-changed-profiles.json"
  write_changed_owner_bank_profiles "$changed_profiles"
  if [[ "$(python3 - "$changed_profiles" <<'PY'
import json, sys
from pathlib import Path
try:
    print("true" if json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")) else "false")
except Exception:
    print("false")
PY
)" != "true" ]]; then
    return 0
  fi

  begin_stage "gateway_restart_owner_bank_profiles"
  log "Restarting running Hermes profile gateways whose AOPS owner bank changed"
  local selector="$WORK_DIR/select-owner-bank-profiles.py"
  write_gateway_profile_selector "$selector"
  run_as_target "CURRENT_PROFILE=$(shell_quote "$PROFILE_NAME") SUMMARY_JSON=$(shell_quote "${RESTART_SUMMARY_JSON:-}") ONLY_PROFILES_JSON=$(shell_quote "$changed_profiles") python3 $(shell_quote "$selector")" | while IFS= read -r other_profile; do
    [[ -n "$other_profile" ]] || continue
    case "$other_profile" in
      ::skip::*)
        printf '[WARN] skipped profile gateway %s\n' "${other_profile#::skip::}"
        continue
        ;;
      ::recover::*)
        recovered="${other_profile#::recover::}"
        other_profile="${recovered%%::*}"
        log "Recovering Hermes gateway profile $other_profile from ${recovered#*::}"
        ;;
    esac
    log "Restarting Hermes gateway profile $other_profile after Hindsight bank change"
    other_lazy_result="$WORK_DIR/gateway-lazy-installs-other.json"
    ensure_gateway_lazy_installs_disabled_for_profile "$other_profile" > "$other_lazy_result" || true
    record_lazy_installs_change_if_needed "$other_profile" "$other_lazy_result"
    controlled_gateway_lifecycle "$other_profile" "restart" 120 false || true
  done
}

profile_sync_config_status() {
  local profile="$1"
  python3 - "$PROFILE_SYNC_SUMMARY_JSON" "$profile" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
entry = next((item for item in data.get("profiles", []) if item.get("profile") == sys.argv[2]), {})
print(entry.get("configStatus") or "pending")
PY
}

apply_other_profile_configs() {
  [[ "$SYNC_OTHER_PROFILES" == true ]] || return 0
  local profile started duration result_path lazy_path status
  while IFS= read -r profile; do
    [[ -n "$profile" && "$profile" != "$PROFILE_NAME" ]] || continue
    started="$(now_ms)"
    result_path="$WORK_DIR/remote-config-apply-${profile//[^A-Za-z0-9_.-]/_}.json"
    log "Synchronizing explicit configuration to profile $profile"
    set +e
    run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --payload $(shell_quote "$PROFILE_SYNC_PAYLOAD_JSON") --profile $(shell_quote "$profile") --skip-skills" > "$result_path" 2>&1
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
      duration=$(($(now_ms) - started))
      warn "Configuration sync failed for profile $profile; continuing"
      record_profile_sync_result "$profile" "failed" "" "" "$duration" "remote_config apply failed (exit $status)"
      continue
    fi
    lazy_path="$WORK_DIR/gateway-lazy-installs-${profile//[^A-Za-z0-9_.-]/_}.json"
    ensure_gateway_lazy_installs_disabled_for_profile "$profile" > "$lazy_path" || true
    record_lazy_installs_change_if_needed "$profile" "$lazy_path"
    if ! ensure_aops_pdf_capabilities_for_profile "$profile" > "$WORK_DIR/pdf-capabilities-${profile//[^A-Za-z0-9_.-]/_}.json"; then
      duration=$(($(now_ms) - started))
      warn "PDF capability migration failed for profile $profile; continuing"
      record_profile_sync_result "$profile" "failed" "" "" "$duration" "PDF capability migration failed"
      continue
    fi
    if ! install_preinstall_skills "$profile" > "$WORK_DIR/pdf-skill-${profile//[^A-Za-z0-9_.-]/_}.json"; then
      duration=$(($(now_ms) - started))
      warn "Bundled PDF skill installation failed for profile $profile; continuing"
      record_profile_sync_result "$profile" "failed" "" "" "$duration" "bundled PDF skill installation failed"
      continue
    fi
    set +e
    validate_aops_gateway_config_for_profile "$profile" > "$WORK_DIR/aops-gateway-config-check-${profile//[^A-Za-z0-9_.-]/_}.json" 2>&1
    status=$?
    set -e
    duration=$(($(now_ms) - started))
    if [[ "$status" -ne 0 ]]; then
      warn "AOPS config validation failed for profile $profile; continuing"
      record_profile_sync_result "$profile" "failed" "" "" "$duration" "AOPS config validation failed"
    else
      record_profile_sync_result "$profile" "updated" "" "" "$duration" "configuration synchronized"
    fi
  done < <(profile_sync_unique_profiles)
}

gateway_action_for_profile() {
  local profile="$1"
  python3 - "$(profile_home_for "$profile")" <<'PY'
import json, os, sys
from pathlib import Path
home = Path(sys.argv[1])
try:
    state = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
except Exception:
    state = {}
pid = state.get("pid")
alive = False
try:
    if int(pid or 0) > 0:
        os.kill(int(pid), 0)
        alive = True
except (OSError, TypeError, ValueError):
    pass
print("restart" if state.get("gateway_state") == "running" and alive else "start")
PY
}

start_all_profile_gateways() {
  [[ "$SYNC_OTHER_PROFILES" == true ]] || return 0
  local profile action started duration status profile_arg
  while IFS= read -r profile; do
    [[ -n "$profile" ]] || continue
    if [[ "$(profile_sync_config_status "$profile")" == "failed" ]]; then
      warn "Not starting profile $profile because its configuration failed"
      continue
    fi
    started="$(now_ms)"
    action="$(gateway_action_for_profile "$profile")"
    profile_arg="$(profile_arg_for "$profile")"
    log "Gateway profile $profile: synchronized action=$action"
    set +e
    run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes $profile_arg gateway install --force --no-start-now --start-on-login"
    status=$?
    if [[ "$status" -eq 0 ]]; then
      controlled_gateway_lifecycle "$profile" "$action" 240 true
      status=$?
    fi
    set -e
    duration=$(($(now_ms) - started))
    if [[ "$status" -eq 0 ]]; then
      record_profile_sync_result "$profile" "$(profile_sync_config_status "$profile")" "$action" "running" "$duration" "gateway $action completed"
    else
      warn "Gateway $action failed for profile $profile; continuing"
      record_profile_sync_result "$profile" "$(profile_sync_config_status "$profile")" "$action" "failed" "$duration" "gateway lifecycle failed (exit $status)"
    fi
  done < <(profile_sync_unique_profiles)
}

install_skill_zips() {
  local profile="$1"
  local profile_dir
  if [[ "$profile" == "default" ]]; then
    profile_dir="$TARGET_HOME/.hermes"
  else
    profile_dir="$TARGET_HOME/.hermes/profiles/$profile"
  fi
  [[ -s "$SKILLS_FILE" ]] || return 0
  log "Installing skill zip(s) into profile $profile"
  python3 - "$SKILLS_FILE" "$profile_dir" "$WORK_DIR/skills-zip" "$(json_get options.overwriteSkills)" <<'PY'
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

skills_file, profile_dir, work_dir, overwrite_raw = sys.argv[1:5]
profile_dir = Path(profile_dir)
work_dir = Path(work_dir)
overwrite = str(overwrite_raw or "true").lower() in {"true", "1", "yes", "on"}
skills_root = profile_dir / "skills"
skills_root.mkdir(parents=True, exist_ok=True)
work_dir.mkdir(parents=True, exist_ok=True)

def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")

def safe_members(zf: zipfile.ZipFile):
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part == ".." for part in path.parts):
            raise SystemExit(f"Unsafe zip member path: {info.filename}")

def copy_skill_dir(src: Path, rel: Path) -> str:
    dest = skills_root / rel
    if dest.exists():
        if not overwrite:
            return "skipped"
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return "installed"

installed = []
for index, spec in enumerate(Path(skills_file).read_text(encoding="utf-8").splitlines(), 1):
    if not spec:
        continue
    zip_path = work_dir / f"skills-{index}.zip"
    if is_url(spec):
        urllib.request.urlretrieve(spec, zip_path)
    else:
        src = Path(spec).expanduser()
        if not src.is_file():
            raise SystemExit(f"skills zip not found: {spec}")
        shutil.copy2(src, zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise SystemExit(f"skills bundle is not a zip file: {spec}")
    extract_dir = work_dir / f"extract-{index}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        safe_members(zf)
        zf.extractall(extract_dir)
    source_root = extract_dir / "skills" if (extract_dir / "skills").is_dir() else extract_dir
    for skill_md in source_root.rglob("SKILL.md"):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(source_root)
        if not rel.parts:
            rel = Path(skill_dir.name)
        status = copy_skill_dir(skill_dir, rel)
        installed.append({"skill": str(rel), "status": status})

print(json.dumps({"installed": installed}, ensure_ascii=False))
PY
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "$TARGET_USER":"$TARGET_USER" "$profile_dir/skills"
  fi
}

install_preinstall_skills() {
  local profile="$1"
  local profile_dir
  profile_dir="$(profile_home_for "$profile")"
  log "Installing preinstall skill(s) into profile $profile"
  run_as_target "$(shell_quote "$INSTALL_DIR/venv/bin/python") - $(shell_quote "$PAYLOAD_JSON") $(shell_quote "$profile_dir") <<'PY'
import inspect
import io
import json
import os
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

payload_path = Path(sys.argv[1])
profile_dir = Path(sys.argv[2])
os.environ['HERMES_HOME'] = str(profile_dir)

try:
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=profile_dir)
except Exception:
    pass

payload = json.loads(payload_path.read_text(encoding='utf-8'))

def _dict(value):
    return value if isinstance(value, dict) else {}

config_skills = _dict(_dict(payload.get('config')).get('skills'))
top_skills = _dict(payload.get('skills'))
skills = config_skills or top_skills
raw = skills.get('preinstall') if isinstance(skills, dict) else []
if raw is None:
    raw = []
if not isinstance(raw, list):
    raise SystemExit('skills.preinstall must be a list')
slugs = [str(item).strip() for item in raw if str(item or '').strip()]

installed = []
builtin_slugs = [slug.split(':', 1)[1] for slug in slugs if slug.startswith('builtin:')]
remote_slugs = [slug for slug in slugs if not slug.startswith('builtin:')]

if builtin_slugs:
    from hermes_constants import get_bundled_skills_dir
    from tools.skills_sync import _dir_hash, _read_manifest, _write_manifest

    bundled_root = get_bundled_skills_dir()
    bundled_manifest = _read_manifest()
    bundled_manifest_changed = False
    for skill_name in builtin_slugs:
        matches = [
            skill_md.parent
            for skill_md in bundled_root.rglob('SKILL.md')
            if skill_md.parent.name == skill_name
        ] if bundled_root.is_dir() else []
        if len(matches) != 1:
            installed.append({
                'skill': 'builtin:' + skill_name,
                'status': 'failed',
                'source': 'builtin',
                'output': f'Expected exactly one bundled skill named {skill_name}; found {len(matches)}',
            })
            continue
        source = matches[0]
        relative = source.relative_to(bundled_root)
        destination = profile_dir / 'skills' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        # AOPS installs only an audited subset of the official bundled skills,
        # rather than running the full bundled sync.  Preserve the same origin
        # manifest so inventory/curator code can still identify this directory
        # as builtin instead of falling through to legacy local/user_created.
        bundled_manifest[skill_name] = _dir_hash(destination)
        bundled_manifest_changed = True
        installed.append({
            'skill': 'builtin:' + skill_name,
            'status': 'success',
            'source': 'builtin',
            'path': str(destination),
            'output': 'Installed from the audited AOPS offline bundle.',
        })
    if bundled_manifest_changed:
        _write_manifest(bundled_manifest)

if remote_slugs:
    from rich.console import Console
    from hermes_cli.skills_hub import do_install
    import tools.skills_hub as hub
    from tools.skills_hub import ClawHubSource

    original_router = hub.create_source_router
    hub.create_source_router = lambda auth=None: [ClawHubSource()]
    try:
        params = inspect.signature(do_install).parameters
        for slug in remote_slugs:
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None, width=120)
            status = 'success'
            try:
                kwargs = {
                    'force': True,
                    'skip_confirm': True,
                    'console': console,
                }
                if 'invalidate_cache' in params:
                    kwargs['invalidate_cache'] = True
                if 'ignore_scan_policy' in params:
                    kwargs['ignore_scan_policy'] = True
                if 'source' in params:
                    kwargs['source'] = 'clawhub'
                with redirect_stdout(stream), redirect_stderr(stream):
                    do_install(slug, **kwargs)
            except Exception as exc:
                status = 'failed'
                stream.write(chr(10) + 'Error: ' + str(exc))
            output = stream.getvalue().strip()
            lowered = output.lower()
            for marker in ('error:', 'installation blocked:', 'could not fetch', 'no skill named', 'cannot install', 'cancelled.'):
                if marker in lowered:
                    status = 'failed'
                    break
            installed.append({
                'skill': slug,
                'status': status,
                'source': 'clawhub',
                'output': output[-4000:],
            })
    finally:
        hub.create_source_router = original_router

print(json.dumps({'installed': installed}, ensure_ascii=False))
if any(item.get('source') == 'builtin' and item.get('status') == 'failed' for item in installed):
    raise SystemExit(1)
PY"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "$TARGET_USER":"$TARGET_USER" "$profile_dir/skills" 2>/dev/null || true
  fi
}

json_file_has_runtime_relevant_changes() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    text = path.read_text(encoding="utf-8")
    start = text.find("{")
    end = text.rfind("}")
    payload = json.loads(text[start:end + 1]) if start >= 0 and end >= start else {}
except Exception:
    payload = {}

def nonempty_list(name):
    value = payload.get(name)
    return isinstance(value, list) and len(value) > 0

changed = (
    nonempty_list("envChanged")
    or nonempty_list("configChanged")
    or nonempty_list("skills")
    or payload.get("userInstructions") is not None
    or payload.get("soul") is not None
    or payload.get("hindsight") is not None
)
print("true" if changed else "false")
PY
}

skills_result_has_changes() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    text = path.read_text(encoding="utf-8")
    start = text.find("{")
    end = text.rfind("}")
    payload = json.loads(text[start:end + 1]) if start >= 0 and end >= start else {}
except Exception:
    payload = {}
items = payload.get("installed") if isinstance(payload, dict) else []
changed = False
if isinstance(items, list):
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip().lower() != "skipped":
            changed = True
            break
print("true" if changed else "false")
PY
}

json_file_changed_flag() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    text = path.read_text(encoding="utf-8")
    start = text.find("{")
    end = text.rfind("}")
    payload = json.loads(text[start:end + 1]) if start >= 0 and end >= start else {}
except Exception:
    payload = {}
print("true" if payload.get("changed") is True else "false")
PY
}

record_lazy_installs_change_if_needed() {
  local profile="$1"
  local result_json="$2"
  if [[ "$(json_file_changed_flag "$result_json")" == "true" ]]; then
    append_restart_summary "diagnostics" "$profile" "set security.allow_lazy_installs=false to avoid startup-time pip installs in offline gateway"
  fi
}

validate_aops_gateway_config_for_profile() {
  local profile="$1"
  local profile_dir
  profile_dir="$(profile_home_for "$profile")"
  local py="$INSTALL_DIR/venv/bin/python"
  [[ -x "$py" ]] || py="python3"

  run_as_target "$(shell_quote "$py") - $(shell_quote "$profile") $(shell_quote "$profile_dir") <<'PY'
import json
import os
import sys
from pathlib import Path

profile = sys.argv[1]
profile_dir = Path(sys.argv[2])
os.environ['HERMES_HOME'] = str(profile_dir)

result = {
    'ok': False,
    'profile': profile,
    'hermesHome': str(profile_dir),
    'envPath': str(profile_dir / '.env'),
    'configPath': str(profile_dir / 'config.yaml'),
    'envExists': (profile_dir / '.env').exists(),
    'configExists': (profile_dir / 'config.yaml').exists(),
    'connectedPlatforms': [],
    'aops': {},
    'error': None,
}

try:
    env_path = profile_dir / '.env'

    def _read_dotenv(path):
        values = {}
        if not path.exists():
            return values
        for raw_line in path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if (len(value) >= 2) and value[0] == value[-1] and value[0] in {chr(34), chr(39)}:
                value = value[1:-1]
            values[key] = value
        return values

    dotenv_values = _read_dotenv(env_path)
    for key, value in dotenv_values.items():
        os.environ.setdefault(key, value)

    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv(hermes_home=profile_dir)
    except Exception:
        pass

    config_path = profile_dir / 'config.yaml'
    config_data = {}
    if config_path.exists():
        import yaml
        loaded = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        if isinstance(loaded, dict):
            config_data = loaded

    def _dict(value):
        return value if isinstance(value, dict) else {}

    def _deep_get(data, *keys):
        cur = data
        for key in keys:
            if not isinstance(cur, dict):
                return {}
            cur = cur.get(key)
        return _dict(cur)

    def _clean(value):
        return str(value or '').strip()

    def _env(name):
        return _clean(os.environ.get(name) or dotenv_values.get(name))

    def _expand(value):
        text = _clean(value)
        placeholder_prefix = chr(36) + chr(123)
        if text.startswith(placeholder_prefix) and text.endswith('}'):
            name = text[2:-1].strip()
            if name.startswith('env.'):
                name = name[4:]
            return _env(name)
        return text

    def _bool_or_none(value):
        if isinstance(value, bool):
            return value
        text = _clean(value).lower()
        if text in {'1', 'true', 'yes', 'y', 'on'}:
            return True
        if text in {'0', 'false', 'no', 'n', 'off'}:
            return False
        return None

    blocks = [
        _deep_get(config_data, 'gateway', 'platforms', 'aops'),
        _deep_get(config_data, 'platforms', 'aops'),
        _deep_get(config_data, 'aops'),
    ]
    merged = {}
    merged_extra = {}
    explicit_enabled = None
    for block in blocks:
        if not block:
            continue
        extra = _dict(block.get('extra'))
        merged.update({k: v for k, v in block.items() if k != 'extra'})
        merged_extra.update(extra)
        if 'enabled' in block:
            parsed_enabled = _bool_or_none(block.get('enabled'))
            if parsed_enabled is not None:
                explicit_enabled = parsed_enabled

    token = (
        _env('AOPS_BOT_TOKEN')
        or _expand(merged.get('token'))
        or _expand(merged.get('bot_token'))
        or _expand(merged.get('AOPS_BOT_TOKEN'))
    )
    base_url = (
        _env('AOPS_BOT_URL')
        or _expand(merged_extra.get('base_url'))
        or _expand(merged_extra.get('baseUrl'))
        or _expand(merged.get('base_url'))
        or _expand(merged.get('baseUrl'))
        or _env('AOPS_BASE_URL')
    ).rstrip('/')
    present = bool(token or base_url or any(blocks))
    enabled = explicit_enabled if explicit_enabled is not None else present
    has_token = bool(token)
    has_base_url = bool(base_url)
    result['aops'] = {
        'present': present,
        'enabled': bool(enabled),
        'hasToken': has_token,
        'baseUrl': base_url,
    }
    if present and enabled and has_token and has_base_url:
        result['connectedPlatforms'] = ['aops']
        result['ok'] = True
except Exception as exc:
    result['error'] = str(exc)

print(json.dumps(result, ensure_ascii=False, indent=2))
if not result['ok']:
    raise SystemExit(42)
PY"
}

print_restart_summary() {
  [[ -n "${RESTART_SUMMARY_JSON:-}" && -f "$RESTART_SUMMARY_JSON" ]] || return 0
  log "Gateway restart summary"
  python3 - "$RESTART_SUMMARY_JSON" <<'PY' || true
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    data = {}
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
}

need_cmd python3
need_cmd tar
if [[ -n "$BUNDLE_CACHE_DIR" && "$BUNDLE_CACHE_DIR" != /* ]]; then
  fail "--bundle-cache-dir must be an absolute path"
fi
if [[ "$HERMES_DATA_ROOT" != /* ]]; then
  fail "--hermes-data-root must be an absolute path"
fi

BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-tec01-bootstrap.XXXXXX")"
chmod 700 "$BOOTSTRAP_DIR"
TIMING_RAW_FILE="$BOOTSTRAP_DIR/install-timings.tsv"
: > "$TIMING_RAW_FILE"
TIMING_START_MS="$(now_ms)"
TIMING_STAGE_START_MS="$TIMING_START_MS"
STAGE="bootstrap"
trap on_exit EXIT
trap 'on_interrupt 1' HUP
trap 'on_interrupt 2' INT
trap 'on_interrupt 15' TERM
TEMPLATE_YAML="$BOOTSTRAP_DIR/template.yaml"
SETS_FILE="$BOOTSTRAP_DIR/sets.txt"
SKILLS_FILE="$BOOTSTRAP_DIR/skills-zips.txt"
PAYLOAD_JSON="$BOOTSTRAP_DIR/payload.json"
PROFILE_JSON="$BOOTSTRAP_DIR/profile.json"
STORAGE_SUMMARY_JSON="$BOOTSTRAP_DIR/storage-summary.json"

python3 - "$SETS_FILE" "${SET_ARGS[@]}" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False) + "\n", encoding="utf-8")
PY
printf '%s\n' "${SKILLS_ZIPS[@]}" > "$SKILLS_FILE"

begin_stage "template"
if [[ -n "$TEMPLATE_FILE" ]]; then
  [[ -f "$TEMPLATE_FILE" ]] || fail "Template file not found: $TEMPLATE_FILE"
  cp "$TEMPLATE_FILE" "$TEMPLATE_YAML"
elif [[ -n "$TEMPLATE_URL" ]]; then
  log "Downloading template: $TEMPLATE_URL"
  download "$TEMPLATE_URL" "$TEMPLATE_YAML"
else
  write_default_template "$TEMPLATE_YAML"
fi

begin_stage "render_payload"
render_payload
apply_sync_other_profiles_cli_override

SYNC_OTHER_PROFILES_RAW="$(json_get options.syncOtherProfiles)"
case "${SYNC_OTHER_PROFILES_RAW:-false}" in
  true|True) SYNC_OTHER_PROFILES=true ;;
  false|False|"") SYNC_OTHER_PROFILES=false ;;
  *) fail "options.syncOtherProfiles must be true or false" ;;
esac

TASK_ID="$(json_get taskId || true)"
TARGET_USER="$(json_get targetUser)"
[[ -n "$TARGET_USER" ]] || fail "targetUser is required"
validate_username "$TARGET_USER"

begin_stage "ensure_user"
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  [[ "$(id -u)" -eq 0 ]] || fail "target user does not exist; run with sudo to create it"
  log "Creating user: $TARGET_USER"
  useradd -m -s /bin/bash "$TARGET_USER"
fi

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || fail "Could not resolve HOME for $TARGET_USER"
INSTALL_DIR="$TARGET_HOME/hermes-agent"
begin_stage "storage_preflight"
ensure_hermes_data_layout
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

if [[ -f "$STORAGE_SUMMARY_JSON" ]]; then
  storage_summary_source="$STORAGE_SUMMARY_JSON"
  STORAGE_SUMMARY_JSON="$WORK_DIR/storage-summary.json"
  if [[ "$storage_summary_source" != "$STORAGE_SUMMARY_JSON" ]]; then
    cp "$storage_summary_source" "$STORAGE_SUMMARY_JSON"
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$STORAGE_SUMMARY_JSON"
  fi
else
  STORAGE_SUMMARY_JSON="$WORK_DIR/storage-summary.json"
fi

PAYLOAD_IN_HOME="$WORK_DIR/tec01-payload.json"
PROFILE_JSON="$WORK_DIR/profile.json"
APPLY_RESULT_JSON="$WORK_DIR/remote-config-apply.json"
PREINSTALL_RESULT_JSON="$WORK_DIR/skills-preinstall-result.json"
SKILLS_RESULT_JSON="$WORK_DIR/skills-install-result.json"
RESTART_SUMMARY_JSON="$WORK_DIR/restart-summary.json"
PROFILE_SYNC_SUMMARY_JSON="$WORK_DIR/profile-sync-summary.json"
python3 - "$RESTART_SUMMARY_JSON" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "restartedProfiles": [],
    "skippedProfiles": [],
    "failedProfiles": [],
    "diagnostics": [],
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
python3 - "$PROFILE_SYNC_SUMMARY_JSON" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "enabled": False,
    "selectedProfile": None,
    "protectedFields": [],
    "profiles": [],
    "failedProfiles": [],
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
cp "$PAYLOAD_JSON" "$PAYLOAD_IN_HOME"
chmod 600 "$PAYLOAD_IN_HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$PAYLOAD_IN_HOME"
  chown "$TARGET_USER":"$TARGET_USER" "$RESTART_SUMMARY_JSON"
  chown "$TARGET_USER":"$TARGET_USER" "$PROFILE_SYNC_SUMMARY_JSON"
fi
PAYLOAD_JSON="$PAYLOAD_IN_HOME"

begin_stage "select_profile"
select_profile
if [[ "$(id -u)" -eq 0 ]]; then
  # select_profile runs as root and may inherit a 0600 umask.  The resolver
  # intentionally runs as TARGET_USER, so hand off this non-secret profile
  # metadata file before the owner-bank preflight reads it.
  chown "$TARGET_USER":"$TARGET_USER" "$PROFILE_JSON"
  chmod 600 "$PROFILE_JSON"
fi
PROFILE_NAME="$(python3 - "$PROFILE_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["profile"])
PY
)"
PROFILE_ACTION="$(python3 - "$PROFILE_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["action"])
PY
)"
log "Selected profile: $PROFILE_NAME ($PROFILE_ACTION)"
if [[ "$SYNC_OTHER_PROFILES" == true && "$PROFILE_ACTION" != "update" ]]; then
  fail "syncOtherProfiles is only supported when updating an existing profile"
fi

# Resolve the AOPS owner before downloading or modifying the runtime.  A
# default-profile update reconciles every existing AOPS profile; all lookups
# must succeed before any profile configuration is written.
OWNER_BANK_PLAN_JSON="$WORK_DIR/aops-owner-bank-plan.json"
OWNER_BANK_PLAN_LOG_JSON="$WORK_DIR/aops-owner-bank-plan.log"
begin_stage "resolve_aops_owner_banks"
log "Resolving AOPS owner-backed Hindsight bank(s)"
resolve_aops_owner_bank_plan | tee "$OWNER_BANK_PLAN_LOG_JSON"
inject_current_owner_bank_into_payload

PROFILE_SYNC_PAYLOAD_JSON="$WORK_DIR/profile-sync-payload.json"
PROFILE_SYNC_PROFILES_JSON="$WORK_DIR/profile-sync-profiles.json"
if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
  build_profile_sync_payload "$PROFILE_SYNC_PAYLOAD_JSON" "$PROFILE_SYNC_PROFILES_JSON"
  if [[ "$(json_get options.overwriteExistingConfig)" == "True" || "$(json_get options.overwriteExistingConfig)" == "true" ]]; then
    log "Cross-profile sync will apply all managed config fields except protected AOPS tokens"
  else
    log "Cross-profile sync will apply only options.overwriteFields"
  fi
  protected_fields="$(python3 - "$PROFILE_SYNC_SUMMARY_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(",".join(data.get("protectedFields") or []))
PY
)"
  [[ -z "$protected_fields" ]] || warn "Protected fields excluded from cross-profile sync: $protected_fields"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$PROFILE_SYNC_PAYLOAD_JSON" "$PROFILE_SYNC_PROFILES_JSON" "$PROFILE_SYNC_SUMMARY_JSON"
  fi
fi

HERMES_INSTALLED=false
RUNTIME_CHANGED=false
BUNDLE_STAMP="$INSTALL_DIR/.aops_bundle_sha256"
BUNDLE_URL="$(json_get bundle.url)"
BUNDLE_SHA="$(json_get bundle.sha256)"
if [[ -x "$INSTALL_DIR/hermes" && -x "$INSTALL_DIR/venv/bin/python" ]] && remote_config_supports_profile; then
  HERMES_INSTALLED=true
fi

RUNTIME_UPDATE_NEEDED=false
if [[ "$HERMES_INSTALLED" == false ]]; then
  RUNTIME_UPDATE_NEEDED=true
else
  if [[ -n "$BUNDLE_SHA" && "$BUNDLE_SHA" != "<sha256>" ]]; then
    INSTALLED_BUNDLE_SHA=""
    if [[ -f "$BUNDLE_STAMP" ]]; then
      INSTALLED_BUNDLE_SHA="$(tr -d '[:space:]' < "$BUNDLE_STAMP" || true)"
    fi
    if [[ "$INSTALLED_BUNDLE_SHA" != "$BUNDLE_SHA" ]]; then
      RUNTIME_UPDATE_NEEDED=true
      if [[ -n "$INSTALLED_BUNDLE_SHA" ]]; then
        log "Hermes runtime bundle sha changed; will upgrade runtime"
      else
        log "Hermes runtime bundle stamp missing; will upgrade runtime"
      fi
    fi
  else
    log "bundle.sha256 is not set; skipping installed runtime upgrade check"
  fi
fi

if [[ "$RUNTIME_UPDATE_NEEDED" == true ]]; then
  [[ -n "$BUNDLE_URL" ]] || fail "bundle.url is required when Hermes runtime must be installed or upgraded"
  [[ -n "$BUNDLE_SHA" && "$BUNDLE_SHA" != "<sha256>" ]] || fail "bundle.sha256 is required when Hermes runtime must be installed or upgraded"

  begin_stage "download_bundle"
  BUNDLE_TGZ="$WORK_DIR/hermes-offline-bundle.tar.gz"
  log "Downloading bundle"
  download_bundle "$BUNDLE_URL" "$BUNDLE_SHA" "$BUNDLE_TGZ"
  chmod 600 "$BUNDLE_TGZ"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$TARGET_USER":"$TARGET_USER" "$BUNDLE_TGZ"
  fi

  begin_stage "verify_bundle"
  actual_sha="$(sha256_file "$BUNDLE_TGZ")"
  [[ "$actual_sha" == "$BUNDLE_SHA" ]] || fail "sha256 mismatch: expected $BUNDLE_SHA got $actual_sha"

  begin_stage "extract_bundle"
  ensure_target_private_dir "$WORK_DIR/extracted"
  run_as_target "tar -xzf '$BUNDLE_TGZ' -C '$WORK_DIR/extracted'"
  BUNDLE_DIR="$(find "$WORK_DIR/extracted" -maxdepth 1 -type d -name 'offline-bundle-*' | head -n 1)"
  [[ -n "$BUNDLE_DIR" && -f "$BUNDLE_DIR/install.sh" ]] || fail "bundle install.sh not found"
  chmod +x "$BUNDLE_DIR/install.sh"

  begin_stage "install_or_upgrade"
  if [[ -x "$INSTALL_DIR/hermes" || -d "$INSTALL_DIR/venv" || -d "$INSTALL_DIR/source-overlay" ]]; then
    log "Upgrading Hermes runtime for $TARGET_USER"
    run_as_target "cd '$BUNDLE_DIR' && bash install.sh '$INSTALL_DIR' --link --upgrade --preserve-config"
  else
    log "Installing Hermes runtime for $TARGET_USER"
    run_as_target "cd '$BUNDLE_DIR' && bash install.sh '$INSTALL_DIR' --link"
  fi
  RUNTIME_CHANGED=true
  run_as_target "printf '%s\n' $(shell_quote "$BUNDLE_SHA") > $(shell_quote "$BUNDLE_STAMP")"
fi

begin_stage "apply_profile_config"
mark_payload_mode "$([[ "$PROFILE_ACTION" == "update" ]] && echo true || echo false)"
CURRENT_PROFILE_CONFIG_OK=true
if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
  current_apply_started="$(now_ms)"
  set +e
  run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --payload '$PAYLOAD_JSON' --profile '$PROFILE_NAME' --skip-skills" | tee "$APPLY_RESULT_JSON"
  current_apply_status=$?
  set -e
  if [[ "$current_apply_status" -ne 0 ]]; then
    CURRENT_PROFILE_CONFIG_OK=false
    APPLY_CHANGED=false
    record_profile_sync_result "$PROFILE_NAME" "failed" "" "" "$(($(now_ms) - current_apply_started))" "remote_config apply failed (exit $current_apply_status)"
  else
    APPLY_CHANGED="$(json_file_has_runtime_relevant_changes "$APPLY_RESULT_JSON")"
    record_profile_sync_result "$PROFILE_NAME" "updated" "" "" "$(($(now_ms) - current_apply_started))" "configuration updated"
  fi
else
  run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --payload '$PAYLOAD_JSON' --profile '$PROFILE_NAME' --skip-skills" | tee "$APPLY_RESULT_JSON"
  APPLY_CHANGED="$(json_file_has_runtime_relevant_changes "$APPLY_RESULT_JSON")"
fi

# The owner lookup is authoritative even when ordinary remote config updates
# preserve an existing Hindsight file.  This synchronizes only the bank fields
# for every profile included in the already-successful preflight plan.
OWNER_BANK_SYNC_RESULT_JSON="$WORK_DIR/hindsight-owner-bank-sync.json"
OWNER_BANK_SYNC_LOG_JSON="$WORK_DIR/hindsight-owner-bank-sync.log"
begin_stage "sync_hindsight_owner_banks"
log "Synchronizing owner-backed Hindsight bank(s)"
sync_hindsight_owner_banks | tee "$OWNER_BANK_SYNC_LOG_JSON"
if [[ "$(owner_bank_sync_changed_for_profile "$PROFILE_NAME")" == "true" ]]; then
  APPLY_CHANGED=true
fi

if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
  begin_stage "sync_profile_configs"
  apply_other_profile_configs
fi

PDF_CAPABILITIES_RESULT_JSON="$WORK_DIR/pdf-capabilities.json"
begin_stage "enable_pdf_capabilities"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  if ensure_aops_pdf_capabilities_for_profile "$PROFILE_NAME" | tee "$PDF_CAPABILITIES_RESULT_JSON"; then
    if python3 - "$PDF_CAPABILITIES_RESULT_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
raise SystemExit(0 if data.get('changed') else 1)
PY
    then
      APPLY_CHANGED=true
    fi
  else
    CURRENT_PROFILE_CONFIG_OK=false
    if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
      record_profile_sync_result "$PROFILE_NAME" "failed" "" "" "0" "PDF capability migration failed"
    else
      fail "PDF capability migration failed for profile $PROFILE_NAME"
    fi
  fi
else
  printf '{"ok":false,"changed":false,"code":"config_invalid"}\n' > "$PDF_CAPABILITIES_RESULT_JSON"
fi

begin_stage "validate_aops_gateway_config"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  log "Validating AOPS gateway config for profile $PROFILE_NAME"
  if ! validate_aops_gateway_config_for_profile "$PROFILE_NAME" | tee "$WORK_DIR/aops-gateway-config-check.json"; then
    if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
      CURRENT_PROFILE_CONFIG_OK=false
      record_profile_sync_result "$PROFILE_NAME" "failed" "" "" "0" "AOPS config validation failed"
    else
      fail "AOPS config validation failed for profile $PROFILE_NAME"
    fi
  fi
fi

VISION_PROBE_RESULT_JSON="$WORK_DIR/model-vision-probe.json"
begin_stage "probe_model_vision"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  profile_home="$(profile_home_for "$PROFILE_NAME")"
  log "Probing image input for profile $PROFILE_NAME"
  set +e
  run_as_target "HERMES_HOME=$(shell_quote "$profile_home") $(shell_quote "$INSTALL_DIR/venv/bin/python") -m tools.aops_vision_probe" | tee "$VISION_PROBE_RESULT_JSON"
  vision_probe_status=$?
  set -e
  if [[ "$vision_probe_status" -ne 0 ]]; then
    warn "Qwen image-input probe failed; normal PDF text extraction remains available, but scanned/graphics-heavy PDF pages require a working vision endpoint. See $VISION_PROBE_RESULT_JSON"
    append_restart_summary "diagnostics" "$PROFILE_NAME" "model vision probe failed; PDF visual pages will report a clear vision error"
  fi
else
  printf '{"ok":false,"code":"config_invalid","error":"AOPS profile config validation failed"}\n' > "$VISION_PROBE_RESULT_JSON"
fi

begin_stage "skills_preinstall"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  install_preinstall_skills "$PROFILE_NAME" | tee "$PREINSTALL_RESULT_JSON"
  PREINSTALL_CHANGED="$(skills_result_has_changes "$PREINSTALL_RESULT_JSON")"
else
  printf '{"installed":[]}\n' > "$PREINSTALL_RESULT_JSON"
  PREINSTALL_CHANGED=false
fi

begin_stage "skills_zip"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  install_skill_zips "$PROFILE_NAME" | tee "$SKILLS_RESULT_JSON"
  SKILL_ZIPS_CHANGED="$(skills_result_has_changes "$SKILLS_RESULT_JSON")"
else
  printf '{"installed":[]}\n' > "$SKILLS_RESULT_JSON"
  SKILL_ZIPS_CHANGED=false
fi
if [[ "$PREINSTALL_CHANGED" == true || "$SKILL_ZIPS_CHANGED" == true ]]; then
  SKILLS_CHANGED=true
else
  SKILLS_CHANGED=false
fi

begin_stage "disable_gateway_lazy_installs"
LAZY_INSTALLS_RESULT_JSON="$WORK_DIR/gateway-lazy-installs.json"
if [[ "$CURRENT_PROFILE_CONFIG_OK" == true ]]; then
  ensure_gateway_lazy_installs_disabled_for_profile "$PROFILE_NAME" | tee "$LAZY_INSTALLS_RESULT_JSON"
  LAZY_INSTALLS_CHANGED="$(json_file_changed_flag "$LAZY_INSTALLS_RESULT_JSON")"
  record_lazy_installs_change_if_needed "$PROFILE_NAME" "$LAZY_INSTALLS_RESULT_JSON"
else
  printf '{"changed":false}\n' > "$LAZY_INSTALLS_RESULT_JSON"
  LAZY_INSTALLS_CHANGED=false
fi

if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
  begin_stage "start_all_profile_gateways"
  start_all_profile_gateways
elif [[ "$PROFILE_ACTION" == "update" ]]; then
  FORCE_GATEWAY_RESTART=false
  if json_bool options.forceGatewayRestart false; then
    FORCE_GATEWAY_RESTART=true
  fi
  if json_bool options.restartGatewayAfterUpgrade true && {
    [[ "$RUNTIME_CHANGED" == true ]] || [[ "$APPLY_CHANGED" == true ]] || [[ "$SKILLS_CHANGED" == true ]] || [[ "$LAZY_INSTALLS_CHANGED" == true ]] || [[ "$FORCE_GATEWAY_RESTART" == true ]]
  }; then
    log "Gateway restart required: runtimeChanged=$RUNTIME_CHANGED applyChanged=$APPLY_CHANGED skillsChanged=$SKILLS_CHANGED lazyInstallsChanged=$LAZY_INSTALLS_CHANGED force=$FORCE_GATEWAY_RESTART"
    ensure_gateway_service_installed "restart" "$PROFILE_NAME"
  else
    log "Skipping gateway restart for profile $PROFILE_NAME: no runtime/config/env/skill changes detected"
    append_restart_summary "skippedProfiles" "$PROFILE_NAME" "no runtime/config/env/skill changes detected"
  fi
else
  if json_bool options.startGatewayAfterInstall true; then
    ensure_gateway_service_installed "start" "$PROFILE_NAME"
  fi
fi

if [[ "$SYNC_OTHER_PROFILES" != true ]]; then
  restart_other_profiles_after_owner_bank_sync
  restart_other_running_profiles_after_upgrade
fi

begin_stage "complete"
print_restart_summary
if [[ "$SYNC_OTHER_PROFILES" == true ]]; then
  cat "$PROFILE_SYNC_SUMMARY_JSON"
  if [[ "$(profile_sync_has_failures)" == "true" ]]; then
    fail "One or more profiles failed during synchronized update"
  fi
fi
report_result "success" "$STAGE" "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
log "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
