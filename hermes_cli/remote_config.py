"""Apply tec01-managed offline install configuration payloads.

This module is intentionally small and conservative: it only patches the
fields that tec01 is allowed to manage, and leaves every other local setting
untouched so upgrade tasks preserve existing user configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hermes_cli.config import (
    ensure_hermes_home,
    get_config_path,
    get_env_path,
    get_hermes_home,
    load_env,
    load_config,
    save_config,
    save_env_value,
)


APPROVAL_MODE_VALUES = ("manual", "smart", "off")
BUSY_INPUT_MODE_VALUES = ("interrupt", "queue", "steer")
AOPS_ENV_KEYS = (
    "AOPS_BOT_TOKEN",
    "AOPS_BOT_URL",
    "AOPS_HOME_CHANNEL",
    "AOPS_HOME_CHANNEL_NAME",
    "AOPS_HOME_CHANNEL_THREAD_ID",
    "CLAWHUB_REGISTRY",
    "AOPS_BASE_URL",
    "AOPS_API_KEY",
    "AOPS_CONNECT_TIMEOUT",
    "HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT",
)
MODEL_GATEWAY_API_KEY_ENV = "MODEL_GATEWAY_API_KEY"
MODEL_GATEWAY_ENV_KEYS = (MODEL_GATEWAY_API_KEY_ENV,)
HINDSIGHT_ENV_KEYS = (
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_API_URL",
    "HINDSIGHT_LLM_API_KEY",
    "HINDSIGHT_TIMEOUT",
    "HINDSIGHT_IDLE_TIMEOUT",
)
HINDSIGHT_ALLOWED_KEYS = (
    "mode",
    "apiKey",
    "api_key",
    "apiUrl",
    "api_url",
    "llmApiKey",
    "llm_api_key",
    "llmBaseUrl",
    "llm_base_url",
    "llmModel",
    "llm_model",
    "bankId",
    "bank_id",
    "bankIdTemplate",
    "bank_id_template",
    "budget",
    "recallBudget",
    "recall_budget",
    "memoryMode",
    "memory_mode",
    "timeout",
    "idleTimeout",
    "idle_timeout",
    "retainTags",
    "retain_tags",
    "retainSource",
    "retain_source",
    "retainUserPrefix",
    "retain_user_prefix",
    "retainAssistantPrefix",
    "retain_assistant_prefix",
)


class RemoteConfigError(RuntimeError):
    """Raised when a tec01 payload is invalid or cannot be applied."""


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _read_payload(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        raise RemoteConfigError(f"failed to read payload {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RemoteConfigError("payload root must be a JSON object")
    config = payload.get("config", {})
    if config is not None and not isinstance(config, dict):
        raise RemoteConfigError("payload.config must be a JSON object")
    return payload


def _backup_file(path: Path) -> str | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.bak.{_now_stamp()}")
    backup.write_bytes(path.read_bytes())
    return str(backup)


def _restore_file(path: Path, backup: str | None) -> None:
    if backup:
        path.write_bytes(Path(backup).read_bytes())


def _rollback_file(path: Path, backup: str | None) -> None:
    if backup:
        path.write_bytes(Path(backup).read_bytes())
    elif path.exists():
        path.unlink()


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RemoteConfigError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise RemoteConfigError(f"{field} must not be empty")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RemoteConfigError(f"{field} must be a string")
    value = value.strip()
    return value or None


def _validate_enum(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    text = _require_string(value, field).lower()
    if text not in allowed:
        raise RemoteConfigError(
            f"{field}={text!r} is not supported; allowed values: {', '.join(allowed)}"
        )
    return text


def _deep_set(config: dict[str, Any], dotted_key: str, value: Any) -> None:
    cur = config
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _deep_get(config: dict[str, Any], dotted_key: str) -> Any:
    cur: Any = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _payload_options(payload: dict[str, Any]) -> dict[str, Any]:
    options = payload.get("options") or {}
    if options and not isinstance(options, dict):
        raise RemoteConfigError("payload.options must be a JSON object")
    return options


def _is_upgrade_mode(options: dict[str, Any]) -> bool:
    return bool(options.get("upgrade") or options.get("isUpgrade"))


def _overwrite_all(options: dict[str, Any]) -> bool:
    value = options.get("overwriteExistingConfig", options.get("overwrite", False))
    return bool(value)


def _overwrite_fields(options: dict[str, Any]) -> set[str]:
    raw = options.get("overwriteFields") or []
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise RemoteConfigError("options.overwriteFields must be a list")
    fields: set[str] = set()
    for idx, item in enumerate(raw):
        fields.add(_require_string(item, f"options.overwriteFields[{idx}]"))
    return fields


def _should_overwrite(field: str, options: dict[str, Any]) -> bool:
    if not _is_upgrade_mode(options):
        return True
    if _overwrite_all(options):
        return True
    fields = _overwrite_fields(options)
    return field in fields or any(field.startswith(prefix + ".") for prefix in fields)


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RemoteConfigError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteConfigError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise RemoteConfigError(f"{field} must be >= 0")
    return parsed


def _optional_env_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RemoteConfigError(f"{field} must be a string or integer")
    if isinstance(value, int):
        return str(value)
    return _optional_string(value, field)


def _save_env_if_allowed(key: str, value: str, *, field: str, options: dict[str, Any], existing_env: dict[str, str], changed: list[str]) -> None:
    if existing_env.get(key) and not _should_overwrite(field, options):
        return
    save_env_value(key, value)
    os.environ[key] = value
    changed.append(key)


def _model_gateway_api_key(model_gateway: dict[str, Any]) -> str | None:
    return (
        _optional_string(model_gateway.get("apiKey"), "config.modelGateway.apiKey")
        or _optional_string(model_gateway.get("api_key"), "config.modelGateway.api_key")
        or _optional_string(model_gateway.get(MODEL_GATEWAY_API_KEY_ENV), f"config.modelGateway.{MODEL_GATEWAY_API_KEY_ENV}")
        or _optional_string(model_gateway.get("OPENAI_API_KEY"), "config.modelGateway.OPENAI_API_KEY")
    )


def _apply_env(config_payload: dict[str, Any], options: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    existing_env = load_env()

    aops = config_payload.get("aops") or {}
    if aops and not isinstance(aops, dict):
        raise RemoteConfigError("config.aops must be a JSON object")
    for key in AOPS_ENV_KEYS:
        value = _optional_string(aops.get(key), f"config.aops.{key}")
        if value is not None:
            _save_env_if_allowed(
                key,
                value,
                field=f"aops.{key}",
                options=options,
                existing_env=existing_env,
                changed=changed,
            )

    model_gateway = config_payload.get("modelGateway") or {}
    if model_gateway and not isinstance(model_gateway, dict):
        raise RemoteConfigError("config.modelGateway must be a JSON object")
    api_key = _model_gateway_api_key(model_gateway)
    if api_key is not None:
        _save_env_if_allowed(
            MODEL_GATEWAY_API_KEY_ENV,
            api_key,
            field="modelGateway.apiKey",
            options=options,
            existing_env=existing_env,
            changed=changed,
        )

    for key in MODEL_GATEWAY_ENV_KEYS:
        if key == MODEL_GATEWAY_API_KEY_ENV:
            continue
        value = _optional_string(model_gateway.get(key), f"config.modelGateway.{key}")
        if value is not None:
            _save_env_if_allowed(
                key,
                value,
                field=f"modelGateway.{key}",
                options=options,
                existing_env=existing_env,
                changed=changed,
            )

    hindsight = config_payload.get("hindsight") or {}
    if hindsight and not isinstance(hindsight, dict):
        raise RemoteConfigError("config.hindsight must be a JSON object")
    if _is_upgrade_mode(options) and (get_hermes_home() / "hindsight" / "config.json").exists() and not _should_overwrite("hindsight", options):
        return changed
    hindsight_env_aliases = {
        "HINDSIGHT_API_KEY": ("apiKey", "api_key", "HINDSIGHT_API_KEY"),
        "HINDSIGHT_API_URL": ("apiUrl", "api_url", "HINDSIGHT_API_URL"),
        "HINDSIGHT_LLM_API_KEY": ("llmApiKey", "llm_api_key", "HINDSIGHT_LLM_API_KEY"),
        "HINDSIGHT_TIMEOUT": ("timeout", "HINDSIGHT_TIMEOUT"),
        "HINDSIGHT_IDLE_TIMEOUT": ("idleTimeout", "idle_timeout", "HINDSIGHT_IDLE_TIMEOUT"),
    }
    for env_key in HINDSIGHT_ENV_KEYS:
        aliases = hindsight_env_aliases[env_key]
        value = None
        for alias in aliases:
            value = _optional_env_string(hindsight.get(alias), f"config.hindsight.{alias}")
            if value is not None:
                break
        if value is not None:
            _save_env_if_allowed(
                env_key,
                value,
                field=f"hindsight.{aliases[0]}",
                options=options,
                existing_env=existing_env,
                changed=changed,
            )

    return changed


def _deep_set_if_allowed(cfg: dict[str, Any], dotted_key: str, value: Any, *, field: str, options: dict[str, Any], changed: list[str]) -> None:
    existing = _deep_get(cfg, dotted_key)
    if existing not in (None, "") and not _should_overwrite(field, options):
        return
    _deep_set(cfg, dotted_key, value)
    changed.append(dotted_key)


def _apply_config_yaml(config_payload: dict[str, Any], options: dict[str, Any]) -> list[str]:
    cfg = load_config()
    original = copy.deepcopy(cfg)
    changed: list[str] = []

    model_gateway = config_payload.get("modelGateway") or {}
    if model_gateway and not isinstance(model_gateway, dict):
        raise RemoteConfigError("config.modelGateway must be a JSON object")
    base_url = _optional_string(model_gateway.get("baseUrl"), "config.modelGateway.baseUrl")
    model = _optional_string(model_gateway.get("model"), "config.modelGateway.model")
    provider = _optional_string(model_gateway.get("provider"), "config.modelGateway.provider") or "custom"
    api_mode = _optional_string(model_gateway.get("apiMode"), "config.modelGateway.apiMode")
    api_key = _model_gateway_api_key(model_gateway)
    if base_url or model or provider or api_mode:
        model_cfg = cfg.get("model")
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            cfg["model"] = model_cfg
        if model:
            _deep_set_if_allowed(cfg, "model.default", model, field="modelGateway.model", options=options, changed=changed)
            _deep_set_if_allowed(cfg, "model.model", model, field="modelGateway.model", options=options, changed=changed)
        if base_url:
            _deep_set_if_allowed(cfg, "model.base_url", base_url.rstrip("/"), field="modelGateway.baseUrl", options=options, changed=changed)
        if provider:
            _deep_set_if_allowed(cfg, "model.provider", provider, field="modelGateway.provider", options=options, changed=changed)
        if api_mode:
            _deep_set_if_allowed(cfg, "model.api_mode", api_mode, field="modelGateway.apiMode", options=options, changed=changed)
        if api_key:
            _deep_set_if_allowed(cfg, "model.api_key_env", MODEL_GATEWAY_API_KEY_ENV, field="modelGateway.apiKey", options=options, changed=changed)

    approvals = config_payload.get("approvals") or {}
    if approvals and not isinstance(approvals, dict):
        raise RemoteConfigError("config.approvals must be a JSON object")
    if "mode" in approvals:
        _deep_set_if_allowed(
            cfg,
            "approvals.mode",
            _validate_enum(approvals["mode"], "config.approvals.mode", APPROVAL_MODE_VALUES),
            field="approvals.mode",
            options=options,
            changed=changed,
        )

    display = config_payload.get("display") or {}
    if display and not isinstance(display, dict):
        raise RemoteConfigError("config.display must be a JSON object")
    if "busy_input_mode" in display:
        _deep_set_if_allowed(
            cfg,
            "display.busy_input_mode",
            _validate_enum(
                display["busy_input_mode"],
                "config.display.busy_input_mode",
                BUSY_INPUT_MODE_VALUES,
            ),
            field="display.busy_input_mode",
            options=options,
            changed=changed,
        )

    hindsight = config_payload.get("hindsight") or {}
    if hindsight and not isinstance(hindsight, dict):
        raise RemoteConfigError("config.hindsight must be a JSON object")
    if hindsight:
        _deep_set_if_allowed(cfg, "memory.provider", "hindsight", field="hindsight", options=options, changed=changed)

    if cfg != original:
        save_config(cfg)
    return changed


def _build_user_instructions(config_payload: dict[str, Any]) -> str | None:
    user_instructions = config_payload.get("userInstructions")
    if user_instructions is None:
        user_instructions = config_payload.get("userMd")
    if user_instructions is None:
        user_instructions = config_payload.get("USER.md")
    if user_instructions is None:
        return None

    if isinstance(user_instructions, str):
        content = user_instructions.strip()
        return content + "\n" if content else None
    if not isinstance(user_instructions, dict):
        raise RemoteConfigError("config.userInstructions must be a string or JSON object")

    content = _optional_string(user_instructions.get("content"), "config.userInstructions.content")
    if content:
        return content.strip() + "\n"

    sections = user_instructions.get("sections") or []
    if not isinstance(sections, list):
        raise RemoteConfigError("config.userInstructions.sections must be a list")
    parts: list[str] = []
    title = _optional_string(user_instructions.get("title"), "config.userInstructions.title")
    if title:
        parts.append(f"# {title}")
    for idx, section in enumerate(sections):
        if isinstance(section, str):
            section_text = section.strip()
            if section_text:
                parts.append(section_text)
            continue
        if not isinstance(section, dict):
            raise RemoteConfigError(f"config.userInstructions.sections[{idx}] must be a string or JSON object")
        heading = _optional_string(section.get("heading"), f"config.userInstructions.sections[{idx}].heading")
        body = _optional_string(section.get("content"), f"config.userInstructions.sections[{idx}].content")
        bullets = section.get("bullets")
        lines: list[str] = []
        if heading:
            lines.append(f"## {heading}")
        if body:
            lines.append(body.strip())
        if bullets is not None:
            if not isinstance(bullets, list):
                raise RemoteConfigError(f"config.userInstructions.sections[{idx}].bullets must be a list")
            for bullet_idx, bullet in enumerate(bullets):
                bullet_text = _require_string(
                    bullet,
                    f"config.userInstructions.sections[{idx}].bullets[{bullet_idx}]",
                )
                lines.append(f"- {bullet_text}")
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts).strip() + "\n" if parts else None


def _apply_user_instructions(config_payload: dict[str, Any], options: dict[str, Any]) -> dict[str, Any] | None:
    content = _build_user_instructions(config_payload)
    if content is None:
        return None
    path = get_hermes_home() / "memories" / "USER.md"
    if _is_upgrade_mode(options) and path.exists() and path.read_text(encoding="utf-8").strip() and not _should_overwrite("userInstructions", options):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_file(path)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "backup": backup}


def _hindsight_value(source: dict[str, Any], *aliases: str, field: str) -> str | None:
    for alias in aliases:
        value = _optional_string(source.get(alias), f"config.hindsight.{field}")
        if value is not None:
            return value
    return None


def _build_hindsight_config(config_payload: dict[str, Any]) -> dict[str, Any] | None:
    hindsight = config_payload.get("hindsight") or {}
    if not hindsight:
        return None
    if not isinstance(hindsight, dict):
        raise RemoteConfigError("config.hindsight must be a JSON object")
    for key in hindsight:
        if key not in HINDSIGHT_ALLOWED_KEYS and key not in HINDSIGHT_ENV_KEYS:
            raise RemoteConfigError(f"config.hindsight.{key} is not supported")

    cfg: dict[str, Any] = {
        "mode": _hindsight_value(hindsight, "mode", field="mode") or "cloud",
        "bank_id": _hindsight_value(hindsight, "bankId", "bank_id", field="bank_id") or "hermes",
        "bank_id_template": (
            _hindsight_value(hindsight, "bankIdTemplate", "bank_id_template", field="bank_id_template")
            or "users-{user}"
        ),
    }

    string_fields = {
        "apiKey": ("apiKey", "api_key", "HINDSIGHT_API_KEY"),
        "api_url": ("apiUrl", "api_url", "HINDSIGHT_API_URL"),
        "llmApiKey": ("llmApiKey", "llm_api_key", "HINDSIGHT_LLM_API_KEY"),
        "llm_base_url": ("llmBaseUrl", "llm_base_url"),
        "llm_model": ("llmModel", "llm_model"),
        "budget": ("budget", "recallBudget", "recall_budget"),
        "recall_budget": ("recallBudget", "recall_budget"),
        "memory_mode": ("memoryMode", "memory_mode"),
        "retain_tags": ("retainTags", "retain_tags"),
        "retain_source": ("retainSource", "retain_source"),
        "retain_user_prefix": ("retainUserPrefix", "retain_user_prefix"),
        "retain_assistant_prefix": ("retainAssistantPrefix", "retain_assistant_prefix"),
    }
    for dest, aliases in string_fields.items():
        value = _hindsight_value(hindsight, *aliases, field=dest)
        if value is not None:
            cfg[dest] = value

    timeout = _optional_int(
        hindsight.get("timeout") if "timeout" in hindsight else hindsight.get("HINDSIGHT_TIMEOUT"),
        "config.hindsight.timeout",
    )
    if timeout is not None:
        cfg["timeout"] = timeout
    idle_timeout = _optional_int(
        hindsight.get("idleTimeout")
        if "idleTimeout" in hindsight
        else hindsight.get("idle_timeout")
        if "idle_timeout" in hindsight
        else hindsight.get("HINDSIGHT_IDLE_TIMEOUT"),
        "config.hindsight.idleTimeout",
    )
    if idle_timeout is not None:
        cfg["idle_timeout"] = idle_timeout

    cfg["banks"] = {
        "hermes": {
            "bankId": cfg["bank_id"],
            "budget": cfg.get("budget") or cfg.get("recall_budget") or "mid",
            "enabled": True,
        }
    }
    return cfg


def _apply_hindsight_config(config_payload: dict[str, Any], options: dict[str, Any]) -> dict[str, Any] | None:
    cfg = _build_hindsight_config(config_payload)
    if cfg is None:
        return None
    path = get_hermes_home() / "hindsight" / "config.json"
    if _is_upgrade_mode(options) and path.exists() and not _should_overwrite("hindsight", options):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_file(path)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "backup": backup}


def _skill_slugs(config_payload: dict[str, Any]) -> list[str]:
    skills = config_payload.get("skills") or {}
    if skills and not isinstance(skills, dict):
        raise RemoteConfigError("config.skills must be a JSON object")
    raw = skills.get("preinstall") or []
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RemoteConfigError("config.skills.preinstall must be a list")
    slugs: list[str] = []
    for idx, item in enumerate(raw):
        slug = _require_string(item, f"config.skills.preinstall[{idx}]")
        slugs.append(slug)
    return slugs


def _install_skill(slug: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "skills",
        "install",
        slug,
        "--source",
        "clawhub",
        "--force",
        "--yes",
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = (proc.stdout or "").strip()
    return {
        "slug": slug,
        "status": "success" if proc.returncode == 0 else "failed",
        "returnCode": proc.returncode,
        "output": output[-4000:],
    }


def _install_skills(config_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [_install_skill(slug) for slug in _skill_slugs(config_payload)]


def schema() -> dict[str, Any]:
    return {
        "approvals.mode": {
            "type": "enum",
            "values": list(APPROVAL_MODE_VALUES),
        },
        "display.busy_input_mode": {
            "type": "enum",
            "values": list(BUSY_INPUT_MODE_VALUES),
        },
        "userInstructions": {
            "type": "string|object",
            "description": "Write formatted Markdown to ~/.hermes/memories/USER.md.",
        },
        "hindsight": {
            "type": "object",
            "fields": {
                "mode": "cloud|local_external|local_embedded|local",
                "apiUrl": "Hindsight endpoint URL",
                "apiKey": "optional Hindsight API key",
                "bankIdTemplate": "defaults to users-{user}; {user} is current system user",
                "budget": "low|mid|high",
                "timeout": "integer seconds",
                "idleTimeout": "integer seconds",
            },
        },
    }


def apply_payload(path: str, *, skip_skills: bool = False) -> dict[str, Any]:
    payload = _read_payload(path)
    config_payload = payload.get("config") or {}
    options = _payload_options(payload)
    ensure_hermes_home()

    env_path = get_env_path()
    config_path = get_config_path()
    env_backup = _backup_file(env_path)
    config_backup = _backup_file(config_path)
    user_instructions_result: dict[str, Any] | None = None
    hindsight_result: dict[str, Any] | None = None

    try:
        env_changed = _apply_env(config_payload, options)
        config_changed = _apply_config_yaml(config_payload, options)
        user_instructions_result = _apply_user_instructions(config_payload, options)
        hindsight_result = _apply_hindsight_config(config_payload, options)
        skill_results = [] if skip_skills else _install_skills(config_payload)
    except Exception:
        _restore_file(env_path, env_backup)
        _restore_file(config_path, config_backup)
        if user_instructions_result:
            _rollback_file(Path(user_instructions_result["path"]), user_instructions_result.get("backup"))
        if hindsight_result:
            _rollback_file(Path(hindsight_result["path"]), hindsight_result.get("backup"))
        raise

    return {
        "ok": True,
        "taskId": payload.get("taskId"),
        "envChanged": env_changed,
        "configChanged": config_changed,
        "skills": skill_results,
        "userInstructions": user_instructions_result,
        "hindsight": hindsight_result,
        "overwriteExistingConfig": _overwrite_all(options),
        "overwriteFields": sorted(_overwrite_fields(options)),
        "backups": {
            "env": env_backup,
            "config": config_backup,
        },
        "paths": {
            "env": str(env_path),
            "config": str(config_path),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply tec01 remote install configuration")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p = sub.add_parser("apply", help="Apply a tec01 task payload")
    apply_p.add_argument("--payload", required=True)
    apply_p.add_argument("--skip-skills", action="store_true")
    sub.add_parser("schema", help="Print supported remote config schema")
    args = parser.parse_args(argv)

    try:
        if args.command == "schema":
            print(json.dumps(schema(), ensure_ascii=False, indent=2))
            return 0
        result = apply_payload(args.payload, skip_skills=args.skip_skills)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
