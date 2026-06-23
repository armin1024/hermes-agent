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
TEMPLATE_FILE=""
TEMPLATE_URL=""
SET_ARGS=()
SKILLS_ZIPS=()

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
    "env.CLAWHUB_REGISTRY": {"required": false, "default": "http://tec01.internal/clawhub"}
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
        "api_key_env": "MODEL_GATEWAY_API_KEY"
      },
      "memory": {"provider": "hindsight"},
      "approvals": {"mode": "off"},
      "display": {"busy_input_mode": "queue"},
      "checkpoints": {"enabled": true},
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
          "messaging"
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
          "messaging"
        ]
      },
      "aops": {
        "toolsets": {
          "disabled": [
            "browser",
            "vision",
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
      "bank_id_template": "users-{user}",
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

remote_config_supports_profile() {
  [[ -x "$INSTALL_DIR/venv/bin/python" ]] || return 1
  run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --help | grep -q -- '--profile'" >/dev/null 2>&1
}

ensure_gateway_service_installed() {
  local action="$1"
  local profile="$2"
  local profile_arg=""
  if [[ "$profile" != "default" ]]; then
    profile_arg="-p $(shell_quote "$profile")"
  fi
  if json_bool options.installGatewayService true; then
    STAGE="gateway_install_service"
    log "Ensuring Hermes gateway service is installed for $TARGET_USER profile $profile"
    run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes $profile_arg gateway install --force --no-start-now --start-on-login"
  fi
  STAGE="gateway_${action}"
  run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes $profile_arg gateway $action"
}

restart_other_running_profiles_after_upgrade() {
  [[ "${RUNTIME_CHANGED:-false}" == "true" ]] || return 0
  json_bool options.restartOtherRunningProfilesAfterUpgrade true || return 0

  STAGE="gateway_restart_other_profiles"
  log "Restarting other running Hermes profile gateways after runtime upgrade"
  run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; CURRENT_PROFILE=$(shell_quote "$PROFILE_NAME") python3 - <<'PY'
import os
import subprocess
from pathlib import Path

home = Path.home()
current_profile = os.environ.get('CURRENT_PROFILE') or 'default'
root = home / '.hermes'
profiles_root = home / '.hermes' / 'profiles'

def service_name(profile: str) -> str:
    if profile == 'default':
        return 'hermes-gateway.service'
    return f'hermes-gateway-{profile}.service'

def profile_home(profile: str) -> Path:
    if profile == 'default':
        return root
    return profiles_root / profile

def service_exists(profile: str) -> bool:
    unit_name = service_name(profile)
    unit = home / '.config' / 'systemd' / 'user' / unit_name
    if unit.exists():
        return True
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'is-enabled', unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

attempted = []
failed = []

candidates = [('default', root)]
if profiles_root.is_dir():
    for child in sorted(profiles_root.iterdir()):
        if child.is_dir():
            candidates.append((child.name, child))

for profile, directory in candidates:
    if profile == current_profile:
        continue
    pid_file = directory / 'gateway.pid'
    if not pid_file.exists() and not service_exists(profile):
        continue
    attempted.append(profile)
    try:
        cmd = ['hermes', 'gateway', 'restart']
        if profile != 'default':
            cmd = ['hermes', '-p', profile, 'gateway', 'restart']
        result = subprocess.run(cmd, check=False, timeout=120)
        if result.returncode != 0:
            failed.append(profile)
    except Exception:
        failed.append(profile)

if attempted:
    print('restarted profile gateways: ' + ', '.join(attempted))
if failed:
    print('warning: failed to restart profile gateways: ' + ', '.join(failed))
PY"
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
  STAGE="skills_zip"
  log "Installing skill zip(s) into profile $profile"
  python3 - "$SKILLS_FILE" "$profile_dir" "$WORK_DIR/skills-zip" "$(json_get options.overwriteSkills)" <<'PY'
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

print({"installed": installed})
PY
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R "$TARGET_USER":"$TARGET_USER" "$profile_dir/skills"
  fi
}

need_cmd python3
need_cmd tar

BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-tec01-bootstrap.XXXXXX")"
chmod 700 "$BOOTSTRAP_DIR"
TEMPLATE_YAML="$BOOTSTRAP_DIR/template.yaml"
SETS_FILE="$BOOTSTRAP_DIR/sets.txt"
SKILLS_FILE="$BOOTSTRAP_DIR/skills-zips.txt"
PAYLOAD_JSON="$BOOTSTRAP_DIR/payload.json"
PROFILE_JSON="$BOOTSTRAP_DIR/profile.json"

python3 - "$SETS_FILE" "${SET_ARGS[@]}" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False) + "\n", encoding="utf-8")
PY
printf '%s\n' "${SKILLS_ZIPS[@]}" > "$SKILLS_FILE"

STAGE="template"
if [[ -n "$TEMPLATE_FILE" ]]; then
  [[ -f "$TEMPLATE_FILE" ]] || fail "Template file not found: $TEMPLATE_FILE"
  cp "$TEMPLATE_FILE" "$TEMPLATE_YAML"
elif [[ -n "$TEMPLATE_URL" ]]; then
  log "Downloading template: $TEMPLATE_URL"
  download "$TEMPLATE_URL" "$TEMPLATE_YAML"
else
  write_default_template "$TEMPLATE_YAML"
fi

STAGE="render_payload"
render_payload

TASK_ID="$(json_get taskId || true)"
TARGET_USER="$(json_get targetUser)"
[[ -n "$TARGET_USER" ]] || fail "targetUser is required"
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

PAYLOAD_IN_HOME="$WORK_DIR/tec01-payload.json"
PROFILE_JSON="$WORK_DIR/profile.json"
cp "$PAYLOAD_JSON" "$PAYLOAD_IN_HOME"
chmod 600 "$PAYLOAD_IN_HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$PAYLOAD_IN_HOME"
fi
PAYLOAD_JSON="$PAYLOAD_IN_HOME"

STAGE="select_profile"
select_profile
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

  STAGE="install_or_upgrade"
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

STAGE="apply_profile_config"
mark_payload_mode "$([[ "$PROFILE_ACTION" == "update" ]] && echo true || echo false)"
run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --payload '$PAYLOAD_JSON' --profile '$PROFILE_NAME'"

install_skill_zips "$PROFILE_NAME"

if [[ "$PROFILE_ACTION" == "update" ]]; then
  if json_bool options.restartGatewayAfterUpgrade true; then
    ensure_gateway_service_installed "restart" "$PROFILE_NAME"
  fi
else
  if json_bool options.startGatewayAfterInstall true; then
    ensure_gateway_service_installed "start" "$PROFILE_NAME"
  fi
fi

restart_other_running_profiles_after_upgrade

STAGE="complete"
report_result "success" "$STAGE" "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
log "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
