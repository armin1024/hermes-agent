"""AOPS command support: local responses, command tree help, and filtering."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.aops_skillhub_bridge import execute_silent_skillhub_command
from gateway.platforms.base import MessageEvent

LOCAL_LIST_SCHEMA = "local-command-list.v1"
HELP_TREE_SCHEMA = "local-command-tree.v2"

_REMOVED_AOPS_COMMANDS = {"update", "debug"}
_AOPS_NATIVE_COMMANDS = {
    "new",
    "help",
    "commands",
    "profile",
    "status",
    "agents",
    "restart",
    "stop",
    "reasoning",
    "fast",
    "verbose",
    "footer",
    "yolo",
    "model",
    "personality",
    "retry",
    "undo",
    "sethome",
    "compress",
    "usage",
    "insights",
    "reload-mcp",
    "reload-skills",
    "approve",
    "deny",
    "title",
    "resume",
    "branch",
    "rollback",
    "background",
    "steer",
    "voice",
    "queue",
    "curator",
}

_CATEGORY_MAP = {
    "Session": "session",
    "Configuration": "configuration",
    "Tools & Skills": "tools",
    "Info": "info",
}

_DESCRIPTION_ZH = {
    "new": "开启新会话（新的会话 ID 和历史记录）。",
    "help": "显示可用命令。",
    "commands": "浏览全部命令和技能（分页）。",
    "profile": "显示当前 profile 名称和 home 目录。",
    "status": "显示会话信息。",
    "agents": "显示活跃 agent 和运行中的任务。",
    "restart": "在排空当前运行后优雅重启网关。",
    "stop": "停止所有后台进程。",
    "reasoning": "管理推理强度和显示方式。",
    "fast": "切换快速模式。",
    "verbose": "循环切换工具进度显示：关闭 -> 新项 -> 全部 -> 详细。",
    "footer": "切换最终回复中的网关运行元数据页脚。",
    "yolo": "切换 YOLO 模式（跳过所有危险命令审批）。",
    "model": "切换当前会话使用的模型。",
    "personality": "设置预定义人格。",
    "retry": "重试上一条消息（重新发送给 agent）。",
    "undo": "移除上一轮用户/助手交互。",
    "sethome": "将当前聊天设置为 home channel。",
    "compress": "手动压缩会话上下文。",
    "usage": "显示当前会话的 token 使用和速率限制。",
    "insights": "显示使用洞察和分析。",
    "reload-mcp": "从配置重新加载 MCP 服务器。",
    "reload-skills": "重新扫描 ~/.hermes/skills/ 中新增或移除的技能。",
    "approve": "批准待处理的危险命令。",
    "deny": "拒绝待处理的危险命令。",
    "title": "设置当前会话标题。",
    "resume": "恢复之前命名的会话。",
    "branch": "从当前会话分叉出一个新会话。",
    "rollback": "列出或恢复文件系统检查点。",
    "background": "在后台运行一个提示。",
    "steer": "在下一次工具调用后注入一条消息而不打断当前执行。",
    "voice": "切换语音模式。",
    "queue": "将提示排队到下一轮执行（不打断当前运行）。",
    "curator": "后台技能维护（状态、运行、固定、归档）。",
    "skills": "列出已安装技能。",
    "cron": "查看定时任务。",
}

def _choice(value: str, description: str) -> dict[str, str]:
    return {"value": value, "description": description}


def _param(
    name: str,
    description: str,
    *,
    required: bool = False,
    choices: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "required": required,
        "choices": choices or [],
    }


_USAGE_COMPLETIONS = {
    "title": [_param("name", "会话标题。", required=False)],
    "branch": [_param("name", "新分支会话名称。", required=False)],
    "compress": [_param("focus", "压缩时关注的话题。", required=False)],
    "rollback": [_param("number", "检查点编号。", required=False)],
    "background": [_param("prompt", "后台执行的提示内容。", required=True)],
    "queue": [_param("prompt", "排队到下一轮的提示内容。", required=True)],
    "steer": [_param("prompt", "下一次工具调用后注入的提示内容。", required=True)],
    "resume": [_param("name", "之前命名的会话名称。", required=False)],
    "model": [
        _param("model", "模型名称。", required=False),
        _param("provider", "provider 名称。", required=False),
        _param("global", "是否将切换持久化为全局设置。", required=False),
    ],
    "personality": [_param("name", "人格名称。", required=False)],
    "reasoning": [
        _param(
            "option",
            "推理强度或显示选项。",
            required=False,
            choices=[
                _choice("none", "将推理强度设置为无。"),
                _choice("minimal", "将推理强度设置为最小。"),
                _choice("low", "将推理强度设置为低。"),
                _choice("medium", "将推理强度设置为中。"),
                _choice("high", "将推理强度设置为高。"),
                _choice("xhigh", "将推理强度设置为超高。"),
                _choice("show", "显示推理配置。"),
                _choice("hide", "隐藏推理配置。"),
                _choice("on", "开启推理配置显示。"),
                _choice("off", "关闭推理配置显示。"),
            ],
        )
    ],
    "fast": [
        _param(
            "mode",
            "快速模式选项。",
            required=False,
            choices=[
                _choice("normal", "切换到普通模式。"),
                _choice("fast", "切换到快速模式。"),
                _choice("status", "显示快速模式状态。"),
                _choice("on", "开启快速模式。"),
                _choice("off", "关闭快速模式。"),
            ],
        )
    ],
    "footer": [
        _param(
            "mode",
            "页脚显示选项。",
            required=False,
            choices=[
                _choice("on", "开启最终回复页脚。"),
                _choice("off", "关闭最终回复页脚。"),
                _choice("status", "显示页脚状态。"),
            ],
        )
    ],
    "voice": [
        _param(
            "mode",
            "语音模式选项。",
            required=False,
            choices=[
                _choice("on", "开启语音模式。"),
                _choice("off", "关闭语音模式。"),
                _choice("tts", "切换到 TTS 语音模式。"),
                _choice("status", "显示语音模式状态。"),
            ],
        )
    ],
    "approve": [
        _param(
            "scope",
            "审批范围。",
            required=False,
            choices=[
                _choice("session", "仅批准当前会话。"),
                _choice("always", "始终批准同类命令。"),
            ],
        )
    ],
    "insights": [_param("days", "统计天数。", required=False)],
    "skills": [],
    "cron": [],
}

_SUBCOMMAND_DESCRIPTION_ZH = {
    ("reasoning", "none"): "将推理强度设置为无。",
    ("reasoning", "minimal"): "将推理强度设置为最小。",
    ("reasoning", "low"): "将推理强度设置为低。",
    ("reasoning", "medium"): "将推理强度设置为中。",
    ("reasoning", "high"): "将推理强度设置为高。",
    ("reasoning", "xhigh"): "将推理强度设置为超高。",
    ("reasoning", "show"): "显示推理配置。",
    ("reasoning", "hide"): "隐藏推理配置。",
    ("reasoning", "on"): "开启推理配置显示。",
    ("reasoning", "off"): "关闭推理配置显示。",
    ("fast", "normal"): "切换到普通模式。",
    ("fast", "fast"): "切换到快速模式。",
    ("fast", "status"): "显示快速模式状态。",
    ("fast", "on"): "开启快速模式。",
    ("fast", "off"): "关闭快速模式。",
    ("footer", "on"): "开启最终回复页脚。",
    ("footer", "off"): "关闭最终回复页脚。",
    ("footer", "status"): "显示页脚状态。",
    ("voice", "on"): "开启语音模式。",
    ("voice", "off"): "关闭语音模式。",
    ("voice", "tts"): "切换到 TTS 语音模式。",
    ("voice", "status"): "显示语音模式状态。",
    ("curator", "status"): "显示 curator 状态和技能统计。",
    ("curator", "run"): "立即执行一次 curator 审查。",
    ("curator", "pause"): "暂停 curator，直到恢复。",
    ("curator", "resume"): "恢复已暂停的 curator。",
    ("curator", "pin"): "固定一个技能，使 curator 不再自动迁移它。",
    ("curator", "unpin"): "取消固定一个技能。",
    ("curator", "restore"): "恢复一个已归档技能。",
}


@dataclass(frozen=True)
class HelpNode:
    type: str
    command: str
    full_command: str
    description: str
    dangerous: bool
    usage: str
    executable: bool
    completions: list[dict[str, Any]]
    children: list["HelpNode"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "command": self.command,
            "fullCommand": self.full_command,
            "description": self.description,
            "dangerous": self.dangerous,
            "usage": self.usage,
            "executable": self.executable,
            "completions": self.completions,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class LocalCommandResult:
    text: str
    content: list[dict[str, Any]] | None = None


def is_aops_event(event: MessageEvent) -> bool:
    return bool(event.source and event.source.platform == Platform.AOPS)


def _platform_config(config: Any) -> PlatformConfig | None:
    if isinstance(config, GatewayConfig):
        return config.platforms.get(Platform.AOPS)
    if isinstance(config, dict):
        platforms = config.get("platforms") or {}
        raw = platforms.get("aops") if isinstance(platforms, dict) else None
        if isinstance(raw, PlatformConfig):
            return raw
        if isinstance(raw, dict):
            return PlatformConfig.from_dict(raw)
    return None


def _extra(config: Any) -> dict[str, Any]:
    platform_cfg = _platform_config(config)
    if platform_cfg and isinstance(platform_cfg.extra, dict):
        return platform_cfg.extra
    return {}


def _normalized_items(raw: Any, env_var: str) -> set[str]:
    if isinstance(raw, str):
        items: Iterable[Any] = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = []
    env_raw = os.getenv(env_var, "")
    env_items = env_raw.replace("\n", ",").split(",") if env_raw else []
    values: set[str] = set()
    for item in [*items, *env_items]:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if not text.startswith("/"):
            text = f"/{text}"
        values.add(text.replace("_", "-"))
    return values


def blocked_commands(config: Any) -> set[str]:
    raw = _extra(config).get("blocked_commands", [])
    return _normalized_items(raw, "AOPS_BLOCKED_COMMANDS")


def dangerous_commands(config: Any) -> set[str]:
    raw = _extra(config).get("dangerous_commands", [])
    return _normalized_items(raw, "AOPS_DANGEROUS_COMMANDS")


def _effective_command(command: str | None, raw_args: str = "") -> str | None:
    if not command:
        return None
    normalized = command.strip().lower().lstrip("/").replace("_", "-")
    if normalized == "hermes":
        try:
            parts = shlex.split(raw_args)
        except ValueError:
            parts = raw_args.split()
        if parts:
            return parts[0].strip().lower().lstrip("/").replace("_", "-")
    return normalized


def is_blocked(config: Any, command: str | None, raw_args: str = "", canonical: str | None = None) -> bool:
    blocked = blocked_commands(config)
    if not blocked:
        return False
    candidates = {
        f"/{candidate}"
        for candidate in (
            _effective_command(command, raw_args),
            _effective_command(canonical, raw_args),
        )
        if candidate
    }
    return bool(candidates & blocked)


def block_message(command: str | None) -> str:
    label = f"/{command}" if command else "this command"
    return f"Command `{label}` is blocked by AOPS config."


def unsupported_message(command: str | None) -> str:
    label = f"/{command}" if command else "this command"
    return (
        f"Command `{label}` is not supported on AOPS. "
        "Use `/help` or `/commands` to view the supported command set."
    )


def _tokens(raw_args: str) -> list[str]:
    try:
        return shlex.split(raw_args)
    except ValueError:
        return raw_args.split()


_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")


def _skill_command_slug(name: str) -> str:
    cmd_name = str(name or "").strip().lower().replace(" ", "-").replace("_", "-")
    cmd_name = _SKILL_INVALID_CHARS.sub("", cmd_name)
    cmd_name = _SKILL_MULTI_HYPHEN.sub("-", cmd_name).strip("-")
    return f"/{cmd_name}" if cmd_name else ""


def _aops_skill_commands(config: Any) -> dict[str, dict[str, Any]]:
    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_disabled_skill_names
    except Exception:
        return {}

    blocked = blocked_commands(config)
    platform_disabled = get_disabled_skill_names(platform=Platform.AOPS.value)
    commands: dict[str, dict[str, Any]] = {}
    for cmd_key, info in get_skill_commands().items():
        normalized_key = str(cmd_key or "").strip().lower().replace("_", "-")
        if not normalized_key.startswith("/"):
            normalized_key = f"/{normalized_key.lstrip('/')}"
        if normalized_key in blocked:
            continue
        if str(info.get("name") or "").strip() in platform_disabled:
            continue
        commands[normalized_key] = info
    return commands


def _is_supported_custom_shape(canonical: str, raw_args: str) -> bool:
    args = _tokens(raw_args)
    if canonical == "skills":
        return not args or args == ["list"]
    if canonical == "cron":
        if not args or args == ["list"]:
            return True
        if args[:1] != ["history"]:
            return False
        if len(args) >= 2 and args[1] in {"before", "after"}:
            if args[1] == "after":
                return len(args) == 4
            return len(args) in {3, 4}
        return len(args) in {2, 3}
    if canonical == "curator":
        return True
    return False


def is_supported_command(command: str | None, raw_args: str = "", canonical: str | None = None) -> bool:
    normalized = _effective_command(canonical or command, raw_args)
    if not normalized:
        return False
    if normalized in {"skills", "cron", "curator"}:
        return _is_supported_custom_shape(normalized, raw_args)
    try:
        from agent.skill_commands import resolve_skill_command_key

        if resolve_skill_command_key(normalized) is not None:
            return True
    except Exception:
        pass
    return normalized in _AOPS_NATIVE_COMMANDS and normalized not in _REMOVED_AOPS_COMMANDS


def _list_response(
    *,
    type_: str,
    command: str,
    item_type: str,
    items: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    total: int | None = None,
    limit: int | None = None,
    error: dict[str, Any] | None = None,
) -> str:
    ok = error is None
    payload = {
        "schemaVersion": LOCAL_LIST_SCHEMA,
        "type": type_,
        "ok": ok,
        "command": command,
        "itemType": item_type,
        "total": len(items) if total is None else total,
        "count": len(items),
        "limit": limit,
        "hasMore": False if limit is None else (total or len(items)) > len(items),
        "context": context or {},
        "summary": summary or {},
        "items": items,
        "error": error,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def _to_ms(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _duration_ms(started_at: Any, finished_at: Any) -> Optional[int]:
    started = str(started_at or "").strip()
    finished = str(finished_at or "").strip()
    if not started or not finished:
        return None
    try:
        started_dt = datetime.fromisoformat(started)
        finished_dt = datetime.fromisoformat(finished)
    except ValueError:
        return None
    delta_ms = int((finished_dt - started_dt).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _compact_text(value: Any, *, limit: int = 160) -> Optional[str]:
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return None
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def _job_description(job: dict[str, Any]) -> Optional[str]:
    explicit = _compact_text(job.get("description"))
    if explicit:
        return explicit
    return _compact_text(job.get("prompt"))


def _status_to_delivery(status: str | None, delivery_error: str | None, delivered: Optional[bool]) -> str:
    if delivered is True:
        return "delivered"
    if delivered is False and delivery_error:
        return "not-delivered"
    if delivery_error:
        return "not-delivered"
    if status == "ok":
        return "unknown"
    return "not-requested"


def _skill_items() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from agent.skill_commands import get_skill_commands, scan_skill_commands
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import SKILLS_DIR, _parse_frontmatter, skill_matches_platform

    skills_root = SKILLS_DIR
    items: list[dict[str, Any]] = []
    command_by_path: dict[str, str] = {}
    command_by_name: dict[str, str] = {}
    try:
        commands = scan_skill_commands() or get_skill_commands()
        for command, info in commands.items():
            normalized_command = str(command or "").strip().lower().replace("_", "-")
            if not normalized_command.startswith("/"):
                normalized_command = f"/{normalized_command.lstrip('/')}"
            skill_md_path = str(info.get("skill_md_path") or "").strip()
            if skill_md_path:
                command_by_path[str(Path(skill_md_path).resolve())] = normalized_command
            name = str(info.get("name") or "").strip()
            if name:
                command_by_name[name] = normalized_command
    except Exception:
        command_by_path = {}
        command_by_name = {}
    if skills_root.exists():
        for skill_md in iter_skill_index_files(skills_root, "SKILL.md"):
            parts = set(skill_md.parts)
            if ".git" in parts or ".hub" in parts or "node_modules" in parts:
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
                frontmatter, _body = _parse_frontmatter(content)
            except Exception:
                frontmatter = {}
            try:
                if not skill_matches_platform(frontmatter):
                    continue
            except Exception:
                pass
            name = str(frontmatter.get("name") or skill_md.parent.name)
            item_id = _safe_relative(skill_md.parent, skills_root)
            command = (
                command_by_path.get(str(skill_md.resolve()))
                or command_by_name.get(name)
                or _skill_command_slug(name)
                or _skill_command_slug(skill_md.parent.name)
                or None
            )
            items.append(
                {
                    "id": item_id,
                    "name": name,
                    "description": frontmatter.get("description") or None,
                    "homepage": frontmatter.get("homepage") or frontmatter.get("url") or None,
                    "command": command,
                    "path": str(skill_md),
                }
            )
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    return items, {"skillsRoot": str(skills_root)}


def _cron_items() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from cron import jobs as cron_jobs

    jobs = cron_jobs.load_jobs()
    history_path = Path(cron_jobs.HISTORY_FILE)
    history_by_job: dict[str, dict[str, Any]] = {}
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            job_id = str(entry.get("job_id") or "").strip()
            if job_id:
                history_by_job[job_id] = entry
    items: list[dict[str, Any]] = []
    enabled = 0
    disabled = 0
    for job in jobs:
        latest_entry = history_by_job.get(str(job.get("id")), {})
        state = str(job.get("state") or "").lower()
        is_enabled = bool(job.get("enabled", True))
        enabled += 1 if is_enabled else 0
        disabled += 0 if is_enabled else 1
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        items.append(
            {
                "id": job.get("id"),
                "name": job.get("name") or job.get("id"),
                "description": _job_description(job),
                "enabled": is_enabled,
                "agentId": None,
                "sessionKey": None,
                "sessionTarget": None,
                "wakeMode": None,
                "deleteAfterRun": bool(job.get("repeat", {}).get("times") == 1 and schedule.get("kind") == "once"),
                "createdAtMs": _to_ms(job.get("created_at")),
                "updatedAtMs": _to_ms(job.get("updated_at") or job.get("created_at")),
                "scheduleText": job.get("schedule_display") or schedule.get("display"),
                "payloadKind": "agentTurn",
                "payloadSummary": (str(job.get("prompt") or "").strip() or None),
                "payloadModel": job.get("model"),
                "payloadFallbacks": None,
                "payloadThinking": None,
                "payloadTimeoutSeconds": None,
                "payloadAllowUnsafeExternalContent": None,
                "payloadLightContext": None,
                "payloadToolsAllow": job.get("enabled_toolsets"),
                "payloadExternalContentSource": None,
                "nextRunAtMs": _to_ms(job.get("next_run_at")),
                "lastRunAtMs": _to_ms(job.get("last_run_at")),
                "runningAtMs": None,
                "lastRunStatus": job.get("last_status"),
                "lastError": job.get("last_error"),
                "lastErrorReason": None,
                "lastDurationMs": (
                    latest_entry.get("duration_ms")
                    or _duration_ms(latest_entry.get("started_at"), latest_entry.get("finished_at"))
                ),
                "consecutiveErrors": None,
                "lastFailureAlertAtMs": None,
                "scheduleErrorCount": None,
                "lastDeliveryStatus": _status_to_delivery(
                    job.get("last_status"),
                    job.get("last_delivery_error") or latest_entry.get("delivery_error"),
                    None if latest_entry.get("silent") else (False if latest_entry.get("delivery_error") else None),
                ),
                "lastDeliveryError": job.get("last_delivery_error"),
                "lastDelivered": (
                    None
                    if latest_entry.get("silent")
                    else (False if (job.get("last_delivery_error") or latest_entry.get("delivery_error")) else None)
                ),
                "deliveryText": job.get("deliver"),
                "failureAlertText": None,
                "state": state or ("scheduled" if is_enabled else "disabled"),
                "origin": job.get("origin"),
                "skills": job.get("skills") or [],
                "workdir": job.get("workdir"),
            }
        )
    context = {"storePath": str(cron_jobs.JOBS_FILE)}
    summary = {"enabled": enabled, "disabled": disabled}
    return items, context, summary


def _history_summary_item(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "description": _job_description(job),
        "enabled": job.get("enabled"),
        "state": job.get("state"),
        "scheduleText": job.get("schedule_display") or schedule.get("display"),
        "nextRunAtMs": _to_ms(job.get("next_run_at")),
        "lastRunAtMs": _to_ms(job.get("last_run_at")),
    }


def _history_error(command: str, context: dict[str, Any], code: str, message: str, details: dict[str, Any] | None = None) -> str:
    return _list_response(
        type_="cron.history.list",
        command=command,
        item_type="cron.run",
        items=[],
        context=context,
        summary={},
        total=None,
        limit=20,
        error={"code": code, "message": message, "details": details or {}},
    )


def _read_cron_history(command_text: str, args: list[str]) -> str:
    from cron import jobs as cron_jobs

    direction = "latest"
    job_id: Optional[str] = None
    requested_anchor: Optional[int] = None

    if not args or args[0] != "history":
        raise ValueError("invalid history command")

    if len(args) >= 2 and args[1] in {"before", "after"}:
        direction = args[1]
        if len(args) < 3:
            return _history_error(
                command_text,
                {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": None, "direction": direction, "requestedAnchorTs": None, "anchorTs": None, "anchorEntry": None},
                "CRON_JOB_NOT_FOUND",
                "Missing cron job id for history query.",
            )
        job_id = args[2]
        if direction == "after" and len(args) < 4:
            return _history_error(
                command_text,
                {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": None, "anchorTs": None, "anchorEntry": None},
                "CRON_HISTORY_MISSING_ANCHOR",
                "The `/cron history after` command requires a tsMs anchor.",
            )
        if len(args) >= 4:
            try:
                requested_anchor = int(args[3])
            except ValueError:
                return _history_error(
                    command_text,
                    {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": args[3], "anchorTs": None, "anchorEntry": None},
                    "CRON_HISTORY_INVALID_ANCHOR",
                    "Invalid history anchor timestamp.",
                    {"anchor": args[3]},
                )
    else:
        if len(args) < 2:
            return _history_error(
                command_text,
                {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": None, "direction": direction, "requestedAnchorTs": None, "anchorTs": None, "anchorEntry": None},
                "CRON_JOB_NOT_FOUND",
                "Missing cron job id for history query.",
            )
        job_id = args[1]
        if len(args) >= 3:
            try:
                requested_anchor = int(args[2])
            except ValueError:
                return _history_error(
                    command_text,
                    {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": args[2], "anchorTs": None, "anchorEntry": None},
                    "CRON_HISTORY_INVALID_ANCHOR",
                    "Invalid history anchor timestamp.",
                    {"anchor": args[2]},
                )

    job = cron_jobs.get_job(job_id)
    if job is None:
        return _history_error(
            command_text,
            {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
            "CRON_JOB_NOT_FOUND",
            f"Cron job `{job_id}` not found.",
        )

    entries: list[dict[str, Any]] = []
    history_path = Path(cron_jobs.HISTORY_FILE)
    try:
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                if str(raw.get("job_id") or "") == str(job_id):
                    entries.append(raw)
    except Exception as exc:
        return _history_error(
            command_text,
            {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
            "CRON_HISTORY_READ_FAILED",
            str(exc),
        )

    if not entries:
        return _history_error(
            command_text,
            {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
            "CRON_HISTORY_NOT_FOUND",
            f"No history found for cron job `{job_id}`.",
        )

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        ts = _to_ms(entry.get("finished_at") or entry.get("timestamp") or entry.get("started_at"))
        delivered = None if entry.get("delivery_error") is None else False
        status = str(entry.get("status") or "ok").lower()
        normalized.append(
            {
                "ts": ts,
                "jobId": entry.get("job_id"),
                "description": entry.get("job_description") or _job_description(job),
                "action": "finished",
                "status": status,
                "error": entry.get("error"),
                "summary": entry.get("response_preview"),
                "delivered": delivered,
                "deliveryStatus": _status_to_delivery(status, entry.get("delivery_error"), delivered),
                "deliveryError": entry.get("delivery_error"),
                "sessionId": None,
                "sessionKey": None,
                "runAtMs": _to_ms(entry.get("started_at") or entry.get("timestamp")),
                "durationMs": entry.get("duration_ms") or _duration_ms(entry.get("started_at"), entry.get("finished_at")),
                "nextRunAtMs": _to_ms(entry.get("scheduled_for")),
                "model": job.get("model"),
                "provider": job.get("provider"),
                "usage": None,
                "jobName": entry.get("job_name") or job.get("name"),
            }
        )

    normalized.sort(key=lambda item: item.get("ts") or 0, reverse=True)

    anchor_entry = normalized[0]
    selected: list[dict[str, Any]]
    if requested_anchor is None:
        if direction == "after":
            return _history_error(
                command_text,
                {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": None, "anchorTs": None, "anchorEntry": None},
                "CRON_HISTORY_MISSING_ANCHOR",
                "The `/cron history after` command requires a tsMs anchor.",
            )
        selected = normalized[:20]
    else:
        index = next((idx for idx, item in enumerate(normalized) if item.get("ts") == requested_anchor), None)
        if index is None:
            return _history_error(
                command_text,
                {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
                "CRON_HISTORY_ANCHOR_NOT_FOUND",
                "The requested history anchor was not found.",
            )
        anchor_entry = normalized[index]
        if direction == "before":
            selected = normalized[index + 1:index + 21]
        elif direction == "after":
            start = max(0, index - 20)
            selected = normalized[start:index]
        else:
            selected = normalized[index:index + 20]

    context = {
        "storePath": str(cron_jobs.JOBS_FILE),
        "logPath": str(cron_jobs.HISTORY_FILE),
        "jobId": job_id,
        "direction": direction,
        "requestedAnchorTs": requested_anchor,
        "anchorTs": anchor_entry.get("ts"),
        "anchorEntry": anchor_entry,
    }
    summary = {"task": _history_summary_item(job)}
    return _list_response(
        type_="cron.history.list",
        command=command_text,
        item_type="cron.run",
        items=selected,
        context=context,
        summary=summary,
        total=len(normalized),
        limit=20,
    )


def maybe_local_command(event: MessageEvent) -> str | LocalCommandResult | None:
    command = event.get_command()
    skillhub_results = execute_silent_skillhub_command(event)
    if skillhub_results is not None:
        content = [result.payload for result in skillhub_results]
        text = json.dumps(content[0], ensure_ascii=False, indent=2) if content else ""
        return LocalCommandResult(text=text, content=content)
    if not command:
        return None
    canonical = command.strip().lower().replace("_", "-")
    raw_args = event.get_command_args().strip()
    args = _tokens(raw_args)
    full_command = f"/{command} {raw_args}".strip()

    if canonical == "skills" and _is_supported_custom_shape("skills", raw_args):
        try:
            items, context = _skill_items()
            context.update({"agentId": "main", "workspaceDir": os.getcwd()})
            return _list_response(
                type_="skills.list",
                command=full_command,
                item_type="skill",
                items=items,
                context=context,
            )
        except Exception as exc:
            return _list_response(
                type_="skills.list",
                command=full_command,
                item_type="skill",
                items=[],
                error={"code": "SKILLS_READ_FAILED", "message": str(exc)},
            )

    if canonical == "cron":
        if not args or args == ["list"]:
            try:
                items, context, summary = _cron_items()
                return _list_response(
                    type_="cron.list",
                    command=full_command,
                    item_type="cron.task",
                    items=items,
                    context=context,
                    summary=summary,
                )
            except Exception as exc:
                return _list_response(
                    type_="cron.list",
                    command=full_command,
                    item_type="cron.task",
                    items=[],
                    error={"code": "CRON_STORE_READ_FAILED", "message": str(exc)},
                )
        if args[:1] == ["history"]:
            return _read_cron_history(full_command, args)

    return None


def _dangerous(config: Any, full_command: str) -> bool:
    return full_command.strip().lower().replace("_", "-") in dangerous_commands(config)


def _command_category(category: str) -> str:
    return _CATEGORY_MAP.get(category, "info")


def _usage_for_command(name: str, args_hint: str, prefix: str = "/") -> str:
    return f"{prefix}{name}{(' ' + args_hint) if args_hint else ''}"


def _node(
    *,
    type_: str,
    command: str,
    full_command: str,
    description: str,
    dangerous: bool,
    usage: str,
    executable: bool,
    completions: list[dict[str, Any]] | None = None,
    children: list[HelpNode] | None = None,
) -> HelpNode:
    return HelpNode(
        type=type_,
        command=command,
        full_command=full_command,
        description=description,
        dangerous=dangerous,
        usage=usage,
        executable=executable,
        completions=completions or [],
        children=children or [],
    )


def _build_official_nodes(config: Any) -> list[HelpNode]:
    from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates

    overrides = _resolve_config_gates()
    nodes: list[HelpNode] = []
    for cmd in COMMAND_REGISTRY:
        if cmd.name in _REMOVED_AOPS_COMMANDS or cmd.name in {"skills", "cron"}:
            continue
        if cmd.name not in _AOPS_NATIVE_COMMANDS:
            continue
        if not _is_gateway_available(cmd, overrides):
            continue
        full_command = f"/{cmd.name}"
        if cmd.name == "curator":
            continue
        children: list[HelpNode] = []
        nodes.append(
            _node(
                type_=_command_category(cmd.category),
                command=full_command,
                full_command=full_command,
                description=_DESCRIPTION_ZH.get(cmd.name, cmd.description),
                dangerous=_dangerous(config, full_command),
                usage=_usage_for_command(cmd.name, cmd.args_hint),
                executable=("<" not in cmd.args_hint),
                completions=list(_USAGE_COMPLETIONS.get(cmd.name, [])),
                children=children,
            )
        )
    return nodes


def _skill_command_nodes(config: Any) -> list[HelpNode]:
    nodes: list[HelpNode] = []
    for cmd_key, info in sorted(_aops_skill_commands(config).items()):
        description = str(info.get("description") or "").strip() or f"Invoke the {info.get('name') or cmd_key} skill"
        nodes.append(
            _node(
                type_="tools",
                command=cmd_key,
                full_command=cmd_key,
                description=description,
                dangerous=_dangerous(config, cmd_key),
                usage=f"{cmd_key} [prompt]",
                executable=True,
            )
        )
    return nodes


def _skills_node(config: Any) -> HelpNode:
    full_command = "/skills"
    child_full = "/skills list"
    return _node(
        type_="custom",
        command=full_command,
        full_command=full_command,
        description="列出已安装技能。",
        dangerous=_dangerous(config, full_command),
        usage="/skills",
        executable=True,
        children=[
            _node(
                type_="custom",
                command="list",
                full_command=child_full,
                description="列出已安装技能。",
                dangerous=_dangerous(config, child_full),
                usage=child_full,
                executable=True,
            )
        ],
    )


def _cron_node(config: Any) -> HelpNode:
    full_command = "/cron"
    list_full = "/cron list"
    history_full = "/cron history"
    history_before = "/cron history before"
    history_after = "/cron history after"
    history_node = _node(
        type_="custom",
        command="history",
        full_command=history_full,
        description="查看定时任务运行历史。",
        dangerous=_dangerous(config, history_full),
        usage="/cron history <id> [tsMs]",
        executable=False,
        completions=[
            _param("id", "定时任务 ID。", required=True),
            _param("tsMs", "历史锚点时间戳（毫秒）。", required=False),
        ],
        children=[
            _node(
                type_="custom",
                command="before",
                full_command=history_before,
                description="查看指定时间戳之前的历史记录。",
                dangerous=_dangerous(config, history_before),
                usage="/cron history before <id> [tsMs]",
                executable=False,
                completions=[
                    _param("id", "定时任务 ID。", required=True),
                    _param("tsMs", "历史锚点时间戳（毫秒）。", required=False),
                ],
            ),
            _node(
                type_="custom",
                command="after",
                full_command=history_after,
                description="查看指定时间戳之后的历史记录。",
                dangerous=_dangerous(config, history_after),
                usage="/cron history after <id> <tsMs>",
                executable=False,
                completions=[
                    _param("id", "定时任务 ID。", required=True),
                    _param("tsMs", "历史锚点时间戳（毫秒）。", required=True),
                ],
            ),
        ],
    )
    return _node(
        type_="custom",
        command=full_command,
        full_command=full_command,
        description="查看定时任务。",
        dangerous=_dangerous(config, full_command),
        usage="/cron",
        executable=True,
        children=[
            _node(
                type_="custom",
                command="list",
                full_command=list_full,
                description="查看定时任务。",
                dangerous=_dangerous(config, list_full),
                usage=list_full,
                executable=True,
            ),
            history_node,
        ],
    )


def _curator_node(config: Any) -> HelpNode:
    full_command = "/curator"
    children: list[HelpNode] = []
    for sub, usage, executable, completions in [
        ("status", "/curator status", True, []),
        ("run", "/curator run", True, []),
        ("pause", "/curator pause", True, []),
        ("resume", "/curator resume", True, []),
        ("pin", "/curator pin <skill>", False, [_param("skill", "技能名称。", required=True)]),
        ("unpin", "/curator unpin <skill>", False, [_param("skill", "技能名称。", required=True)]),
        ("restore", "/curator restore <skill>", False, [_param("skill", "技能名称。", required=True)]),
    ]:
        child_full = f"{full_command} {sub}"
        children.append(
            _node(
                type_="tools",
                command=sub,
                full_command=child_full,
                description=_SUBCOMMAND_DESCRIPTION_ZH[("curator", sub)],
                dangerous=_dangerous(config, child_full),
                usage=usage,
                executable=executable,
                completions=completions,
            )
        )
    return _node(
        type_="tools",
        command=full_command,
        full_command=full_command,
        description=_DESCRIPTION_ZH["curator"],
        dangerous=_dangerous(config, full_command),
        usage="/curator",
        executable=False,
        children=children,
    )


def _count_nodes(nodes: list[HelpNode]) -> tuple[int, int, int]:
    total = 0
    executable = 0
    dangerous = 0
    stack = list(nodes)
    while stack:
        node = stack.pop()
        total += 1
        executable += 1 if node.executable else 0
        dangerous += 1 if node.dangerous else 0
        stack.extend(node.children)
    return total, executable, dangerous


def help_tree_response(config: Any) -> str:
    nodes = _build_official_nodes(config)
    nodes.append(_skills_node(config))
    nodes.append(_cron_node(config))
    nodes = [node for node in nodes if node.full_command != "/curator"]
    nodes.append(_curator_node(config))
    nodes.extend(_skill_command_nodes(config))
    top_level_count = len(nodes)
    total_count, executable_count, dangerous_count = _count_nodes(nodes)
    payload = {
        "schemaVersion": HELP_TREE_SCHEMA,
        "type": "command.tree",
        "ok": True,
        "command": "/help",
        "context": {"channel": "aops"},
        "summary": {
            "topLevelCount": top_level_count,
            "totalNodeCount": total_count,
            "executableCount": executable_count,
            "dangerousCount": dangerous_count,
        },
        "items": [node.to_dict() for node in nodes],
        "error": None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_line_command(line: str) -> str | None:
    match = re.search(r"`/([^`\s\[]+)", line)
    if not match:
        return None
    return match.group(1).replace("_", "-").lower()


def filter_help_lines(lines: Iterable[str], config: Any) -> list[str]:
    blocked = {item.lstrip("/") for item in blocked_commands(config)}
    filtered: list[str] = []
    for line in lines:
        command = _parse_line_command(line)
        if command is None:
            filtered.append(line)
            continue
        if command in _REMOVED_AOPS_COMMANDS or command in blocked:
            continue
        if command not in _AOPS_NATIVE_COMMANDS:
            continue
        filtered.append(line)
    return filtered


def aops_text_command_lines() -> list[str]:
    return [
        "`/skills` -- List installed skills",
        "`/skills list` -- List installed skills",
        "`/cron` -- Show scheduled tasks",
        "`/cron list` -- Show scheduled tasks",
        "`/cron history <id> [tsMs]` -- Show cron run history",
        "`/cron history before <id> [tsMs]` -- Show cron history before an anchor",
        "`/cron history after <id> <tsMs>` -- Show cron history after an anchor",
    ]


def aops_skill_command_lines(config: Any) -> list[str]:
    lines: list[str] = []
    for cmd_key, info in sorted(_aops_skill_commands(config).items()):
        description = str(info.get("description") or "").strip() or "Skill command"
        lines.append(f"`{cmd_key}` -- {description}")
    return lines


def is_cli_bridge_command(command: str | None) -> bool:
    return False


async def run_cli_bridge(event: MessageEvent, config: Any) -> str:
    del event, config
    return unsupported_message("hermes")


def run_curator_command(raw_args: str) -> tuple[int, str]:
    from hermes_cli import curator as curator_cli

    argv = _tokens(raw_args)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = curator_cli.cli_main(argv)
    output = stdout.getvalue().strip()
    err = stderr.getvalue().strip()
    text = output
    if err:
        text = f"{text}\n{err}".strip() if text else err
    code = int(exit_code or 0)
    text = text or "Command returned no output."
    if code:
        text = f"{text}\n(exit {code})"
    return code, text
