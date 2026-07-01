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
    STAGE="gateway_install_service"
    log "Ensuring Hermes gateway service is installed for $TARGET_USER profile $profile"
    run_as_target "export PATH=\"\$HOME/.local/bin:\$PATH\"; hermes $profile_arg gateway install --force --no-start-now --start-on-login"
  fi
  STAGE="gateway_${action}"
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

def append_summary(bucket: str, message: str) -> None:
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
                append_summary("restartedProfiles", f"{action} completed; pid={pid}")
                return True
            aops_state, aops_detail = aops_status(st)
            if aops_state == "connected":
                print(f"✓ Gateway profile {profile} runtime is running with AOPS connected (PID {pid})")
                append_summary("restartedProfiles", f"{action} completed; pid={pid}; aops=connected")
                return True
            if aops_state in {"missing", "error", "failed", "startup_failed", "fatal", "disconnected"}:
                diagnostics(f"AOPS platform not connected after gateway runtime started: state={aops_state}; {aops_detail or 'no detail'}")
                append_summary("failedProfiles", f"aops not connected: {aops_state}")
                return False
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

def hard_systemd_restart() -> int:
    run(["systemctl", "--user", "reset-failed", unit], timeout=15)
    result = run(["systemctl", "--user", "restart", unit], timeout=90)
    if result.returncode != 0:
        print(f"⚠ systemctl restart {unit} returned {result.returncode}; falling back to hermes gateway start", file=sys.stderr)
        result = run(["hermes", *profile_args, "gateway", "start"], timeout=90)
    return result.returncode

def systemd_start() -> int:
    result = run(["systemctl", "--user", "start", unit], timeout=90)
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
    drain_deadline = time.monotonic() + 185
    while pid_alive(before_pid) and time.monotonic() < drain_deadline:
        time.sleep(1)
    if pid_alive(before_pid):
        print(f"⚠ Graceful restart for profile {profile} did not exit within 185s; forcing systemd restart.")
    else:
        print(f"✓ Previous gateway PID {before_pid} exited; starting replacement.")
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
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus")
root = home / ".hermes"
profiles_root = root / "profiles"

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

  STAGE="gateway_restart_other_profiles"
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
  STAGE="skills_preinstall"
  log "Installing preinstall skill(s) into profile $profile"
  run_as_target "$(shell_quote "$INSTALL_DIR/venv/bin/python") - $(shell_quote "$PAYLOAD_JSON") $(shell_quote "$profile_dir") <<'PY'
import inspect
import io
import json
import os
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
if slugs:
    from rich.console import Console
    from hermes_cli.skills_hub import do_install
    import tools.skills_hub as hub
    from tools.skills_hub import ClawHubSource

    original_router = hub.create_source_router
    hub.create_source_router = lambda auth=None: [ClawHubSource()]
    try:
        params = inspect.signature(do_install).parameters
        for slug in slugs:
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
APPLY_RESULT_JSON="$WORK_DIR/remote-config-apply.json"
PREINSTALL_RESULT_JSON="$WORK_DIR/skills-preinstall-result.json"
SKILLS_RESULT_JSON="$WORK_DIR/skills-install-result.json"
RESTART_SUMMARY_JSON="$WORK_DIR/restart-summary.json"
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
cp "$PAYLOAD_JSON" "$PAYLOAD_IN_HOME"
chmod 600 "$PAYLOAD_IN_HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$TARGET_USER":"$TARGET_USER" "$PAYLOAD_IN_HOME"
  chown "$TARGET_USER":"$TARGET_USER" "$RESTART_SUMMARY_JSON"
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
run_as_target "'$INSTALL_DIR/venv/bin/python' -m hermes_cli.remote_config apply --payload '$PAYLOAD_JSON' --profile '$PROFILE_NAME' --skip-skills" | tee "$APPLY_RESULT_JSON"
APPLY_CHANGED="$(json_file_has_runtime_relevant_changes "$APPLY_RESULT_JSON")"

STAGE="validate_aops_gateway_config"
log "Validating AOPS gateway config for profile $PROFILE_NAME"
validate_aops_gateway_config_for_profile "$PROFILE_NAME" | tee "$WORK_DIR/aops-gateway-config-check.json"

install_preinstall_skills "$PROFILE_NAME" | tee "$PREINSTALL_RESULT_JSON"
PREINSTALL_CHANGED="$(skills_result_has_changes "$PREINSTALL_RESULT_JSON")"

install_skill_zips "$PROFILE_NAME" | tee "$SKILLS_RESULT_JSON"
SKILL_ZIPS_CHANGED="$(skills_result_has_changes "$SKILLS_RESULT_JSON")"
if [[ "$PREINSTALL_CHANGED" == true || "$SKILL_ZIPS_CHANGED" == true ]]; then
  SKILLS_CHANGED=true
else
  SKILLS_CHANGED=false
fi

STAGE="disable_gateway_lazy_installs"
LAZY_INSTALLS_RESULT_JSON="$WORK_DIR/gateway-lazy-installs.json"
ensure_gateway_lazy_installs_disabled_for_profile "$PROFILE_NAME" | tee "$LAZY_INSTALLS_RESULT_JSON"
LAZY_INSTALLS_CHANGED="$(json_file_changed_flag "$LAZY_INSTALLS_RESULT_JSON")"
record_lazy_installs_change_if_needed "$PROFILE_NAME" "$LAZY_INSTALLS_RESULT_JSON"

if [[ "$PROFILE_ACTION" == "update" ]]; then
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

restart_other_running_profiles_after_upgrade

STAGE="complete"
print_restart_summary
report_result "success" "$STAGE" "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
log "Hermes profile $PROFILE_NAME $PROFILE_ACTION completed for $TARGET_USER"
