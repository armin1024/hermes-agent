#!/usr/bin/env bash
# Emergency root-filesystem recovery for Tec01/Hermes hosts.
#
# Moves the Docker data root from /var/lib/docker to /data/docker without
# changing Hermes source or profile configuration.  Dry-run is the default.

set -Eeuo pipefail

EXECUTE=false
PURGE_SOURCE=false
MOVE_HERMES_CACHE=true
DATA_ROOT="/data/docker"
HERMES_CACHE_ROOT="/data/hermes-tec01/cache"
WAIT_SECONDS=90
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE_ROOT="/data/hermes-root-migration/$STAMP"
SOURCE_ROOT="/var/lib/docker"
SOURCE_BACKUP=""
DAEMON_CONFIG="/etc/docker/daemon.json"
DAEMON_BACKUP=""
DAEMON_CONFIG_EXISTED=false
DAEMON_CONFIG_SNAPSHOTTED=false
DAEMON_CONFIG_MUTATED=false
MIGRATION_STARTED=false
VERIFIED=false
DOCKER_WAS_ACTIVE=false
CONTAINERD_WAS_ACTIVE=false
SOURCE_MOVED=false
SOURCE_PLACEHOLDER_CREATED=false
CURRENT_STAGE="preflight"

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

on_error() {
  local rc=$?
  local line="${1:-unknown}"
  local command="${2:-unknown}"
  warn "Migration command failed: stage=$CURRENT_STAGE line=$line rc=$rc command=$command"
  return "$rc"
}

trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  sudo bash emergency_migrate_docker_to_data.sh [options]

Dry-run is the default. No data or service is changed without --execute.

Options:
  --execute                     Perform the migration.
  --purge-source-after-verify   Delete the old /var/lib/docker only after
                                Docker and all previous container IDs verify.
  --data-root DIR               New Docker data root (default: /data/docker).
  --hermes-cache-root DIR       New root for /var/cache/hermes-tec01.
  --skip-hermes-cache           Do not migrate /var/cache/hermes-tec01.
  --wait SECONDS                Docker readiness timeout (default: 90).
  --help                        Show this help.

Recommended emergency invocation:
  sudo bash emergency_migrate_docker_to_data.sh \
    --execute --purge-source-after-verify
EOF
}

while (($#)); do
  case "$1" in
    --execute)
      EXECUTE=true
      shift
      ;;
    --purge-source-after-verify)
      PURGE_SOURCE=true
      shift
      ;;
    --data-root)
      [[ $# -ge 2 ]] || fail "--data-root requires a value"
      DATA_ROOT="$2"
      shift 2
      ;;
    --hermes-cache-root)
      [[ $# -ge 2 ]] || fail "--hermes-cache-root requires a value"
      HERMES_CACHE_ROOT="$2"
      shift 2
      ;;
    --skip-hermes-cache)
      MOVE_HERMES_CACHE=false
      shift
      ;;
    --wait)
      [[ $# -ge 2 ]] || fail "--wait requires a value"
      WAIT_SECONDS="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "Run this script as root"
[[ "$DATA_ROOT" == /data/* ]] || fail "--data-root must be below /data"
[[ "$HERMES_CACHE_ROOT" == /data/* ]] || fail "--hermes-cache-root must be below /data"
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "--wait must be a positive integer"

log "Emergency migration preflight started (execute=$EXECUTE purgeSource=$PURGE_SOURCE)"

for command in findmnt df du rsync systemctl python3 docker timeout; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command not found: $command"
done

log "Checking / and /data mount layout"
ROOT_DEVICE="$(findmnt -n -o SOURCE --target /)"
DATA_DEVICE="$(findmnt -n -o SOURCE --target /data 2>/dev/null || true)"
[[ -n "$DATA_DEVICE" ]] || fail "/data is not a mounted filesystem"
[[ "$ROOT_DEVICE" != "$DATA_DEVICE" ]] || fail "/data is on the same filesystem as /; migration would not free root space"
[[ -d "$SOURCE_ROOT" ]] || fail "$SOURCE_ROOT does not exist"

log "Measuring $SOURCE_ROOT usage; this can take several minutes on Docker overlay2"
SOURCE_BYTES="$(du -sx --block-size=1 "$SOURCE_ROOT" | awk '{print $1}')"
log "Docker source usage measurement completed"
DATA_AVAILABLE_BYTES="$(df -P -B1 /data | awk 'NR==2 {print $4}')"
REQUIRED_BYTES=$((SOURCE_BYTES + SOURCE_BYTES / 10 + 268435456))
((DATA_AVAILABLE_BYTES >= REQUIRED_BYTES)) || fail "/data does not have enough free space; required=$REQUIRED_BYTES available=$DATA_AVAILABLE_BYTES"

log "Reading current Docker data-root (timeout=15s)"
CURRENT_DOCKER_ROOT="$(timeout 15 docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [[ "$CURRENT_DOCKER_ROOT" == "$DATA_ROOT" ]]; then
  log "Docker already uses $DATA_ROOT"
  exit 0
fi
if [[ -n "$CURRENT_DOCKER_ROOT" && "$CURRENT_DOCKER_ROOT" != "$SOURCE_ROOT" ]]; then
  fail "Docker currently uses unexpected data root: $CURRENT_DOCKER_ROOT"
fi

log "Root filesystem: $ROOT_DEVICE"
log "Data filesystem: $DATA_DEVICE"
log "Docker source: $SOURCE_ROOT ($(numfmt --to=iec "$SOURCE_BYTES" 2>/dev/null || printf '%s bytes' "$SOURCE_BYTES"))"
log "Docker destination: $DATA_ROOT"
log "Available on /data: $(numfmt --to=iec "$DATA_AVAILABLE_BYTES" 2>/dev/null || printf '%s bytes' "$DATA_AVAILABLE_BYTES")"
if [[ "$MOVE_HERMES_CACHE" == true ]]; then
  log "Hermes shared cache will move to $HERMES_CACHE_ROOT"
fi
if [[ "$PURGE_SOURCE" == true ]]; then
  warn "The old Docker tree will be deleted only after verification succeeds"
else
  warn "The old Docker tree will be retained; root space will not be reclaimed until it is removed"
fi

if [[ "$EXECUTE" != true ]]; then
  printf '\n[DRY-RUN] No changes made. Re-run with:\n'
  printf '  sudo bash %q --execute --purge-source-after-verify\n' "$0"
  exit 0
fi

mkdir -p "$STATE_ROOT"
chmod 700 "$STATE_ROOT"

# Prepare everything needed to change daemon.json before stopping Docker.  In
# particular, prove that /etc/docker can still complete an atomic write even
# when the root filesystem is nearly full.  The desired config itself lives on
# /data so no parsing or allocation surprise is deferred until the outage.
CURRENT_STAGE="prepare_daemon_config"
mkdir -p "$(dirname "$DAEMON_CONFIG")"
if [[ -f "$DAEMON_CONFIG" ]]; then
  DAEMON_CONFIG_EXISTED=true
  DAEMON_BACKUP="$STATE_ROOT/daemon.json.before"
  cp -aL "$DAEMON_CONFIG" "$DAEMON_BACKUP"
fi
python3 - "$DAEMON_CONFIG" "$DATA_ROOT" "$STATE_ROOT/daemon.json.desired" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
data_root = sys.argv[2]
desired_path = Path(sys.argv[3])
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Invalid {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid {path}: top-level JSON must be an object")
else:
    data = {}
data["data-root"] = data_root

# Exercise the same create/write/fsync/unlink operations that the final atomic
# replacement needs.  Failure here is safe because Docker is still running.
probe_fd, probe_name = tempfile.mkstemp(prefix=".daemon-write-probe.", dir=str(path.parent))
try:
    with os.fdopen(probe_fd, "w", encoding="utf-8") as handle:
        handle.write("{}\n")
        handle.flush()
        os.fsync(handle.fileno())
finally:
    if os.path.exists(probe_name):
        os.unlink(probe_name)

desired_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
DAEMON_CONFIG_SNAPSHOTTED=true
log "Docker daemon config snapshot and atomic-write preflight completed"

DOCKER_WAS_ACTIVE=false
CONTAINERD_WAS_ACTIVE=false
CURRENT_STAGE="read_service_state"
timeout 15 systemctl is-active --quiet docker.service && DOCKER_WAS_ACTIVE=true || true
timeout 15 systemctl is-active --quiet containerd.service && CONTAINERD_WAS_ACTIVE=true || true
log "Service state captured: dockerActive=$DOCKER_WAS_ACTIVE containerdActive=$CONTAINERD_WAS_ACTIVE"

capture_container_inventory() {
  local all_output="$STATE_ROOT/container-ids.before"
  local running_output="$STATE_ROOT/running-container-ids.before"
  local api_all="$STATE_ROOT/container-ids.api"
  local api_running="$STATE_ROOT/running-container-ids.api"

  log "Capturing Docker container inventory (API timeout=15s)"
  if timeout 15 docker ps -aq > "$api_all" 2>/dev/null; then
    sort -u "$api_all" > "$all_output"
  else
    warn "docker ps -aq did not respond; reading container IDs from local Docker metadata"
    find "$SOURCE_ROOT/containers" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
      | sort -u > "$all_output"
  fi

  if timeout 15 docker ps -q > "$api_running" 2>/dev/null; then
    sort -u "$api_running" > "$running_output"
  else
    warn "docker ps -q did not respond; reading running state from config.v2.json"
    python3 - "$SOURCE_ROOT/containers" "$running_output" <<'PY'
import json
import sys
from pathlib import Path

containers_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
running = []
if containers_dir.is_dir():
    for config_path in containers_dir.glob("*/config.v2.json"):
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = data.get("State") if isinstance(data, dict) else None
        if isinstance(state, dict) and state.get("Running") is True:
            running.append(config_path.parent.name)
output.write_text("".join(f"{item}\n" for item in sorted(set(running))), encoding="utf-8")
PY
    # Runtime task directories are a second source of truth when Docker's
    # persisted config does not expose State.Running on a particular release.
    for runtime_dir in \
      /run/docker/runtime-runc/moby \
      /run/docker/containerd/daemon/io.containerd.runtime.v2.task/moby; do
      if [[ -d "$runtime_dir" ]]; then
        find "$runtime_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null >> "$running_output"
      fi
    done
    sort -u -o "$running_output" "$running_output"
  fi
  rm -f "$api_all" "$api_running"
  log "Container inventory captured: total=$(wc -l < "$all_output" | tr -d ' ') running=$(wc -l < "$running_output" | tr -d ' ')"
}

capture_container_inventory

migrate_hermes_cache() {
  local source="/var/cache/hermes-tec01"
  local target_parent
  [[ "$MOVE_HERMES_CACHE" == true ]] || return 0
  if [[ -L "$source" ]]; then
    log "Hermes cache is already a symlink: $source -> $(readlink "$source")"
    return 0
  fi
  [[ -d "$source" ]] || return 0
  target_parent="$(dirname "$HERMES_CACHE_ROOT")"
  mkdir -p "$target_parent" "$HERMES_CACHE_ROOT"
  chmod 700 "$target_parent" "$HERMES_CACHE_ROOT" 2>/dev/null || true
  log "Migrating Hermes shared cache to $HERMES_CACHE_ROOT"
  rsync -aHAX --numeric-ids "$source/" "$HERMES_CACHE_ROOT/"
  rm -rf "$source"
  ln -s "$HERMES_CACHE_ROOT" "$source"
}

restore_daemon_config() {
  if [[ "$DAEMON_CONFIG_SNAPSHOTTED" != true ]]; then
    warn "Docker daemon config was not snapshotted; refusing to alter it during rollback"
    return 1
  fi
  if [[ "$DAEMON_CONFIG_EXISTED" == true && -n "$DAEMON_BACKUP" && -f "$DAEMON_BACKUP" ]]; then
    cp -a "$DAEMON_BACKUP" "$DAEMON_CONFIG"
  elif [[ "$DAEMON_CONFIG_EXISTED" == false ]]; then
    rm -f "$DAEMON_CONFIG"
  fi
}

rollback() {
  local actual_root=""
  local docker_ready=false
  local deadline
  local main_pid="0"
  warn "Migration failed; attempting rollback"
  trap - ERR
  set +e
  CURRENT_STAGE="rollback_stop_services"
  timeout 90 systemctl stop docker.service docker.socket
  if (( $? != 0 )); then
    warn "Rollback: Docker service/socket did not stop cleanly within 90s"
  fi
  timeout 90 systemctl stop containerd.service
  if (( $? != 0 )); then
    warn "Rollback: containerd did not stop cleanly within 90s"
  fi
  main_pid="$(systemctl show docker.service -p MainPID --value 2>/dev/null || printf '0')"
  if systemctl is-active --quiet docker.service \
    || systemctl is-active --quiet docker.socket \
    || { [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$main_pid" 2>/dev/null; }; then
    warn "Rollback: Docker service/socket is still active; refusing to modify its config or data directory"
    systemctl status docker.service --no-pager -l >&2 || true
    return 1
  fi

  CURRENT_STAGE="rollback_restore_config"
  if [[ "$DAEMON_CONFIG_MUTATED" == true ]]; then
    restore_daemon_config || warn "Rollback: failed to restore $DAEMON_CONFIG"
  fi

  CURRENT_STAGE="rollback_restore_source"
  if [[ "$SOURCE_MOVED" == true && -n "$SOURCE_BACKUP" && -d "$SOURCE_BACKUP" ]]; then
    if [[ "$SOURCE_PLACEHOLDER_CREATED" == true ]]; then
      rm -rf "$SOURCE_ROOT"
    elif [[ -e "$SOURCE_ROOT" ]]; then
      warn "Rollback: refusing to overwrite unexpected $SOURCE_ROOT"
      return 1
    fi
    if ! mv "$SOURCE_BACKUP" "$SOURCE_ROOT"; then
      warn "Rollback: failed to restore $SOURCE_BACKUP to $SOURCE_ROOT"
      return 1
    fi
  fi

  CURRENT_STAGE="rollback_restart_services"
  if [[ "$CONTAINERD_WAS_ACTIVE" == true ]]; then
    timeout 90 systemctl start containerd.service || warn "Rollback: containerd failed to restart"
  fi
  if [[ "$DOCKER_WAS_ACTIVE" == true ]]; then
    if ! timeout 90 systemctl start docker.service; then
      warn "Rollback: Docker service start command failed or timed out"
    fi
    deadline=$((SECONDS + 90))
    while ((SECONDS < deadline)); do
      if actual_root="$(timeout 10 docker info --format '{{.DockerRootDir}}' 2>/dev/null)"; then
        if [[ "$actual_root" == "$SOURCE_ROOT" ]]; then
          docker_ready=true
          break
        fi
        warn "Rollback: Docker is responsive but uses unexpected data root: $actual_root"
      fi
      sleep 2
    done
    if [[ "$docker_ready" != true ]]; then
      warn "Rollback: Docker did not become ready; diagnostic output follows"
      systemctl status docker.service --no-pager -l >&2 || true
      journalctl -u docker.service -n 80 --no-pager -l >&2 || true
    else
      warn "Rollback restored Docker on the original data root"
    fi
  fi
  warn "Rollback attempted. Destination copy remains at $DATA_ROOT for diagnosis."
  set -e
}

on_exit() {
  local rc=$?
  trap - EXIT
  if ((rc != 0)) && [[ "$MIGRATION_STARTED" == true && "$VERIFIED" != true ]]; then
    rollback
  fi
  exit "$rc"
}
trap on_exit EXIT

migrate_hermes_cache

log "Stopping Docker and containerd"
# Set this before the first destructive service action.  If either stop fails,
# the EXIT trap must still restore the services that were running at preflight.
MIGRATION_STARTED=true
CURRENT_STAGE="stop_docker"
timeout 90 systemctl stop docker.service docker.socket
log "Docker service and socket stopped"
CURRENT_STAGE="stop_containerd"
timeout 90 systemctl stop containerd.service
log "containerd service stopped"

mkdir -p "$DATA_ROOT"
chmod 710 "$DATA_ROOT"
log "Copying Docker data to $DATA_ROOT"
CURRENT_STAGE="copy_docker_data"
rsync -aHAXx --numeric-ids --delete --info=progress2 "$SOURCE_ROOT/" "$DATA_ROOT/"
sync

CURRENT_STAGE="write_daemon_config"
# Mark intent before replacement so an interrupt immediately after os.replace
# still restores the snapshotted configuration.
DAEMON_CONFIG_MUTATED=true
python3 - "$DAEMON_CONFIG" "$STATE_ROOT/daemon.json.desired" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
desired_path = Path(sys.argv[2])
fd, temp_name = tempfile.mkstemp(prefix=".daemon.json.", dir=str(path.parent))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(desired_path.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)
finally:
    if os.path.exists(temp_name):
        os.unlink(temp_name)
PY

CURRENT_STAGE="switch_docker_root"
SOURCE_BACKUP="/var/lib/docker.before-data-$STAMP"
[[ ! -e "$SOURCE_BACKUP" ]] || fail "Backup path already exists: $SOURCE_BACKUP"
SOURCE_MOVED=true
mv "$SOURCE_ROOT" "$SOURCE_BACKUP"
SOURCE_PLACEHOLDER_CREATED=true
mkdir -p "$SOURCE_ROOT"

log "Starting containerd and Docker with the new data root"
CURRENT_STAGE="start_containerd"
timeout 90 systemctl start containerd.service
CURRENT_STAGE="start_docker"
timeout 90 systemctl start docker.service

deadline=$((SECONDS + WAIT_SECONDS))
until docker info >/dev/null 2>&1; do
  ((SECONDS < deadline)) || fail "Docker did not become ready within ${WAIT_SECONDS}s"
  sleep 2
done

ACTUAL_ROOT="$(docker info --format '{{.DockerRootDir}}')"
[[ "$ACTUAL_ROOT" == "$DATA_ROOT" ]] || fail "Docker started with unexpected data root: $ACTUAL_ROOT"

timeout 30 docker ps -aq | sort -u > "$STATE_ROOT/container-ids.after"
comm -23 "$STATE_ROOT/container-ids.before" "$STATE_ROOT/container-ids.after" > "$STATE_ROOT/missing-container-ids"
if [[ -s "$STATE_ROOT/missing-container-ids" ]]; then
  fail "Container IDs are missing after migration; see $STATE_ROOT/missing-container-ids"
fi

if [[ -s "$STATE_ROOT/running-container-ids.before" ]]; then
  log "Restoring containers that were running before migration"
  timeout 90 xargs -r docker start < "$STATE_ROOT/running-container-ids.before" >/dev/null
  sleep 3
  timeout 30 docker ps -q | sort -u > "$STATE_ROOT/running-container-ids.after"
  comm -23 "$STATE_ROOT/running-container-ids.before" "$STATE_ROOT/running-container-ids.after" > "$STATE_ROOT/missing-running-container-ids"
  if [[ -s "$STATE_ROOT/missing-running-container-ids" ]]; then
    fail "Previously running containers did not stay running; see $STATE_ROOT/missing-running-container-ids"
  fi
fi

VERIFIED=true
CURRENT_STAGE="verified"
log "Verified DockerRootDir=$ACTUAL_ROOT and all previous container IDs"

if [[ "$PURGE_SOURCE" == true ]]; then
  log "Removing verified old Docker tree: $SOURCE_BACKUP"
  rm -rf "$SOURCE_BACKUP"
  SOURCE_BACKUP=""
else
  warn "Old Docker data retained at $SOURCE_BACKUP"
  warn "After manual verification, reclaim space with: rm -rf '$SOURCE_BACKUP'"
fi

sync
log "Migration complete"
df -h / /data
timeout 30 docker system df || warn "docker system df timed out after migration"
