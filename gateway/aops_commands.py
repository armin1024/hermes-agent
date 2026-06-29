"""AOPS command support: local responses, command tree help, and filtering."""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shlex
import time
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
    "security",
    "securty",
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
    "toolsets",
    "skills",
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
    "toolsets": "查看或切换当前 profile 的 AOPS 工具集。",
    "skills": "列出已安装技能。",
    "cron": "查看定时任务。",
    "soul": "查看或编辑当前 profile 的 SOUL.md 指令。",
    "user": "查看或编辑当前 profile 的 memories/USER.md 指令。",
    "busy": "查看或切换 busy 输入策略。",
    "security": "查看或切换安全审批策略。",
    "securty": "查看或切换安全审批策略。",
}

_TOOLSET_DESCRIPTION_ZH = {
    "web": "执行网页搜索和网页内容抓取。内网纯终端环境通常不可用，除非已配置可访问的搜索或抓取服务。",
    "browser": "驱动浏览器进行页面导航、点击、输入和滚动。无图形浏览器的 Linux 服务器默认不可用。",
    "terminal": "执行终端命令、管理进程和读取命令输出。适合服务器运维。",
    "file": "读取、搜索、创建和修改文件。适合配置、日志和代码处理。",
    "code_execution": "运行受控代码片段，用于数据处理、脚本验证和快速计算。",
    "vision": "分析图片内容。需要视觉模型或相关服务配置。",
    "video": "分析视频内容。需要支持视频的模型或后端服务。",
    "image_gen": "生成图片。需要图像生成后端或 API Key。",
    "video_gen": "生成视频。需要视频生成后端或 API Key。",
    "x_search": "搜索 X/Twitter 内容。需要 xAI 或 X 相关凭证。",
    "moa": "使用多个模型协作回答。通常需要外部模型凭证。",
    "tts": "文本转语音。纯终端服务器默认不需要启用。",
    "skills": "列出、查看和管理 Hermes Skills。",
    "todo": "维护任务计划和待办事项。",
    "memory": "读写长期记忆。",
    "session_search": "搜索历史会话内容。",
    "clarify": "在信息不足时发起澄清问题。",
    "delegation": "委派子任务给其他 agent 或执行单元。",
    "cronjob": "创建、查看、更新和运行定时任务。",
    "messaging": "通过已配置平台发送消息。",
    "homeassistant": "控制 Home Assistant 智能家居。需要 HASS_TOKEN 和服务地址。",
    "spotify": "控制 Spotify 播放、搜索和媒体库。需要 Spotify 凭证。",
    "discord": "读取和参与 Discord 会话。需要 Discord 平台配置。",
    "discord_admin": "管理 Discord 频道、角色和帖子。需要 Discord 管理权限配置。",
    "yuanbao": "使用元宝平台相关消息能力。需要平台配置。",
    "computer_use": "控制 macOS 桌面。Linux 纯终端服务器不可用。",
}

_LINUX_TERMINAL_UNSUPPORTED_REASONS = {
    "web": "内网 Linux 服务器通常无法访问公网搜索/抓取服务，默认关闭。",
    "browser": "需要可用浏览器或浏览器自动化运行环境，纯终端服务器默认关闭。",
    "vision": "需要视觉模型或外部服务配置，默认关闭。",
    "video": "需要视频理解模型或外部服务配置，默认关闭。",
    "image_gen": "需要图像生成服务或 API Key，默认关闭。",
    "video_gen": "需要视频生成服务或 API Key，默认关闭。",
    "x_search": "需要 X/xAI 凭证和外网访问，默认关闭。",
    "moa": "需要额外模型供应商凭证，默认关闭。",
    "tts": "需要语音服务或音频输出场景，纯终端默认关闭。",
    "homeassistant": "需要 Home Assistant 服务配置，默认关闭。",
    "spotify": "需要 Spotify 凭证和外网服务，默认关闭。",
    "discord": "需要 Discord 平台配置，默认关闭。",
    "discord_admin": "需要 Discord 平台和管理权限配置，默认关闭。",
    "yuanbao": "需要元宝平台配置，默认关闭。",
    "computer_use": "仅适用于 macOS 桌面控制，Linux 纯终端不可用。",
}

_SKILL_DESCRIPTION_ZH = {
    "code-review": "辅助进行代码审查，关注缺陷、风险和测试缺口。",
    "debug": "辅助定位和修复运行时问题。",
    "docs": "辅助编写、整理或更新文档。",
    "testing": "辅助设计和执行测试。",
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
        _param(
            "subcommand",
            "模型子指令。",
            required=False,
            choices=[
                _choice("list", "获取当前模型网关可用模型列表。"),
                _choice("status", "获取当前生效模型信息，不请求模型列表接口。"),
                _choice("current", "同 status，获取当前生效模型信息。"),
                _choice("use", "切换模型。"),
            ],
        ),
        _param("provider", "provider 名称。", required=False),
        _param("model", "模型名称。", required=False),
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
    "toolsets": [
        _param(
            "subcommand",
            "工具集子指令。",
            required=False,
            choices=[
                _choice("list", "获取当前 agent/profile 的工具集列表。"),
                _choice("enable", "启用工具集。"),
                _choice("disable", "关闭工具集。"),
                _choice("set", "按 true/false 设置工具集。"),
            ],
        ),
        _param("name", "工具集名称，例如 web、terminal、file。", required=False),
        _param("enabled", "true 或 false，仅 set 子指令需要。", required=False),
    ],
    "cron": [],
    "security": [
        _param(
            "mode",
            "安全策略模式。",
            required=False,
            choices=[
                _choice("off", "完全访问权限，不触发普通危险命令审批。"),
                _choice("manual", "默认权限，危险命令需要人工审批。"),
                _choice("smart", "自动审查，低风险自动通过，高风险请求审批。"),
            ],
        )
    ],
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


class LocalCommandResult(str):
    """String-compatible local command reply with optional AOPS metadata."""

    text: str
    content: list[dict[str, Any]] | None
    metadata: dict[str, Any] | None

    def __new__(
        cls,
        text: str,
        content: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        obj = str.__new__(cls, text or "")
        obj.text = text or ""
        obj.content = content
        obj.metadata = metadata
        return obj


@dataclass(frozen=True)
class AopsCommandExtension:
    name: str
    type: str = "custom"
    description: str = ""
    usage: str | None = None
    executable: bool = True
    completions: list[dict[str, Any]] | None = None
    children: list[dict[str, Any]] | None = None


_AOPS_COMMAND_EXTENSIONS: dict[str, AopsCommandExtension] = {}


def register_aops_command_extension(extension: AopsCommandExtension) -> None:
    """Register an AOPS-only command so /help and support checks stay in sync."""
    name = str(extension.name or "").strip().lower().replace("_", "-").lstrip("/")
    if not name:
        raise ValueError("AOPS command extension name is required")
    _AOPS_COMMAND_EXTENSIONS[name] = AopsCommandExtension(
        name=name,
        type=extension.type or "custom",
        description=extension.description or f"执行 /{name} 指令。",
        usage=extension.usage or f"/{name}",
        executable=bool(extension.executable),
        completions=list(extension.completions or []),
        children=list(extension.children or []),
    )


def unregister_aops_command_extension(name: str) -> None:
    _AOPS_COMMAND_EXTENSIONS.pop(str(name or "").strip().lower().replace("_", "-").lstrip("/"), None)


def _aops_registered_command_names() -> set[str]:
    return set(_AOPS_COMMAND_EXTENSIONS.keys())


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


def _cron_schedule_flags(job: dict[str, Any]) -> dict[str, Any]:
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    repeat = job.get("repeat") if isinstance(job.get("repeat"), dict) else {}
    kind = str(schedule.get("kind") or "").strip() or None
    times = repeat.get("times")
    completed = repeat.get("completed", 0) or 0
    is_one_shot = bool(kind == "once")
    schedule_warning = None
    if kind == "once" and times is None:
        schedule_warning = "once schedule with forever repeat is terminal after one run; convert schedule to an interval for recurring execution"
    if times is None:
        repeat_text = "forever" if kind in {"interval", "cron"} else None
    elif times == 1:
        repeat_text = "once"
    else:
        repeat_text = f"{completed}/{times}" if completed else f"{times} times"
    return {
        "scheduleKind": kind,
        "repeatText": repeat_text,
        "isOneShot": is_one_shot,
        "scheduleWarning": schedule_warning,
    }


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
        if not args or args == ["list"]:
            return True
        if args[:1] in (["enable"], ["disable"]):
            return len(args) >= 2
        if args[:1] == ["set"]:
            return len(args) >= 3
        if args[:1] in (["uninstall"], ["remove"]):
            return len(args) >= 2
        return False
    if canonical == "cron":
        if not args or args == ["list"]:
            return True
        if args[:1] == ["remove"]:
            return len(args) <= 2
        if args[:1] != ["history"]:
            return False
        if len(args) >= 2 and args[1] in {"before", "after"}:
            if args[1] == "after":
                return len(args) == 4
            return len(args) in {3, 4}
        return len(args) in {2, 3}
    if canonical == "curator":
        return True
    if canonical == "toolsets":
        return True
    if canonical in {"soul", "user"}:
        if not args or args == ["get"]:
            return True
        if args[:1] in (["set"], ["append"]):
            return len(args) >= 2
        return False
    if canonical == "busy":
        return (not args) or (len(args) == 1 and args[0] in {"status", "queue", "steer", "interrupt"})
    return False


def is_supported_command(command: str | None, raw_args: str = "", canonical: str | None = None) -> bool:
    normalized = _effective_command(canonical or command, raw_args)
    if not normalized:
        return False
    if normalized in {"skills", "cron", "curator", "toolsets", "soul", "user", "busy"}:
        return _is_supported_custom_shape(normalized, raw_args)
    try:
        from agent.skill_commands import resolve_skill_command_key

        if resolve_skill_command_key(normalized) is not None:
            return True
    except Exception:
        pass
    return (
        normalized in (_AOPS_NATIVE_COMMANDS | _aops_registered_command_names())
        and normalized not in _REMOVED_AOPS_COMMANDS
    )


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
    updated: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
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
    if updated is not None:
        payload["updated"] = updated
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _single_response(
    *,
    type_: str,
    command: str,
    data: dict[str, Any],
    ok: bool = True,
    error: dict[str, Any] | None = None,
) -> str:
    payload = {
        "schemaVersion": LOCAL_LIST_SCHEMA,
        "type": type_,
        "ok": ok,
        "command": command,
        **data,
        "error": error,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_INSTRUCTION_CONTENT_PREVIEW_LIMIT = 300
_BUSY_MODES = {"queue", "steer", "interrupt"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _instruction_path(kind: str) -> Path:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    if kind == "soul":
        return home / "SOUL.md"
    return home / "memories" / "USER.md"


def _read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _atomic_write_text(path: Path, content: str) -> None:
    from utils import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        atomic_replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _json_payload_after_action(raw_args: str, action: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    text = str(raw_args or "").strip()
    prefix = str(action or "").strip()
    if prefix and text.lower().startswith(prefix.lower()):
        text = text[len(prefix):].strip()
    if not text:
        return None, {
            "code": "AOPS_INSTRUCTION_INVALID_JSON",
            "message": "Expected JSON payload, for example {\"content\":\"...\"}.",
        }
    try:
        payload = json.loads(text)
    except Exception as exc:
        return None, {"code": "AOPS_INSTRUCTION_INVALID_JSON", "message": str(exc)}
    if not isinstance(payload, dict):
        return None, {
            "code": "AOPS_INSTRUCTION_INVALID_JSON",
            "message": "Payload must be a JSON object.",
        }
    return payload, None


def _instruction_error_response(command: str, kind: str, code: str, message: str, path: Path | None = None) -> str:
    return _single_response(
        type_=f"{kind}.updated",
        command=command,
        data={
            "path": str(path) if path else str(_instruction_path(kind)),
            "effectiveImmediately": False,
        },
        ok=False,
        error={"code": code, "message": message},
    )


def _instruction_command(kind: str, command_text: str, raw_args: str, args: list[str]) -> LocalCommandResult:
    action = args[0].lower() if args else "get"
    path = _instruction_path(kind)
    type_prefix = "soul" if kind == "soul" else "user"
    label = "SOUL.md" if kind == "soul" else "memories/USER.md"

    if action in {"", "get"}:
        content = _read_text_file(path)
        text = _single_response(
            type_=f"{type_prefix}.status",
            command=command_text,
            data={
                "path": str(path),
                "content": content,
                "contentLength": len(content),
                "updatedAtMs": _to_ms(datetime.fromtimestamp(path.stat().st_mtime)) if path.exists() else None,
                "effectiveImmediately": True,
                "message": f"Current {label} content.",
            },
        )
        return LocalCommandResult(text=text, metadata={})

    if action not in {"set", "append"}:
        text = _instruction_error_response(
            command_text,
            type_prefix,
            "AOPS_INSTRUCTION_INVALID_JSON",
            f"Usage: /{type_prefix} [get|set|append] {{json}}",
            path,
        )
        return LocalCommandResult(text=text, metadata={})

    payload, error = _json_payload_after_action(raw_args, action)
    if error:
        text = _instruction_error_response(command_text, type_prefix, error["code"], error["message"], path)
        return LocalCommandResult(text=text, metadata={})
    assert payload is not None
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        text = _instruction_error_response(
            command_text,
            type_prefix,
            "AOPS_INSTRUCTION_EMPTY_CONTENT",
            "Payload field 'content' must be a non-empty string.",
            path,
        )
        return LocalCommandResult(text=text, metadata={})
    try:
        from tools.memory_tool import _scan_memory_content

        scan_error = _scan_memory_content(content)
    except Exception:
        scan_error = None
    if scan_error:
        text = _instruction_error_response(
            command_text,
            type_prefix,
            "AOPS_INSTRUCTION_THREAT_DETECTED",
            scan_error,
            path,
        )
        return LocalCommandResult(text=text, metadata={})

    before = _read_text_file(path)
    if action == "append" and before:
        new_content = before.rstrip("\n") + "\n\n" + content.strip() + "\n"
    elif action == "append":
        new_content = content.strip() + "\n"
    else:
        new_content = content
        if not new_content.endswith("\n"):
            new_content += "\n"
    _atomic_write_text(path, new_content)
    now = _now_ms()
    preview = new_content[:_INSTRUCTION_CONTENT_PREVIEW_LIMIT]
    if len(new_content) > _INSTRUCTION_CONTENT_PREVIEW_LIMIT:
        preview += "…"
    text = _single_response(
        type_=f"{type_prefix}.updated",
        command=command_text,
        data={
            "path": str(path),
            "contentPreview": preview,
            "contentLength": len(new_content),
            "updatedAtMs": now,
            "operation": action,
            "effectiveImmediately": True,
            "message": f"Updated {label}. Future turns will reload the updated instruction file.",
        },
    )
    return LocalCommandResult(
        text=text,
        metadata={
            "effects": {
                "invalidateAgentCache": True,
                "reason": f"aops_{type_prefix}_updated",
                "path": str(path),
            }
        },
    )


def _busy_current_mode() -> str:
    mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
    if not mode:
        try:
            import yaml
            from hermes_constants import get_hermes_home

            config_path = Path(get_hermes_home()) / "config.yaml"
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            display = cfg.get("display") if isinstance(cfg, dict) else {}
            mode = str((display or {}).get("busy_input_mode") or "").strip().lower()
        except Exception:
            mode = ""
    return mode if mode in _BUSY_MODES else "interrupt"


def _save_busy_input_mode(mode: str) -> bool:
    try:
        from hermes_constants import get_hermes_home
        from utils import atomic_roundtrip_yaml_update

        config_path = Path(get_hermes_home()) / "config.yaml"
        atomic_roundtrip_yaml_update(config_path, "display.busy_input_mode", mode)
        try:
            os.chmod(config_path, 0o600)
        except (OSError, NotImplementedError):
            pass
        return True
    except Exception:
        return False


def _busy_command(command_text: str, args: list[str]) -> LocalCommandResult:
    mode = args[0].lower() if args else "status"
    if mode == "status":
        mode = _busy_current_mode()
        text = _single_response(
            type_="busy.status",
            command=command_text,
            data={
                "mode": mode,
                "effectiveImmediately": True,
                "usage": "/busy [queue|steer|interrupt|status]",
            },
        )
        return LocalCommandResult(text=text, metadata={})
    if mode not in _BUSY_MODES:
        text = _single_response(
            type_="busy.updated",
            command=command_text,
            data={
                "mode": _busy_current_mode(),
                "effectiveImmediately": False,
                "usage": "/busy [queue|steer|interrupt|status]",
            },
            ok=False,
            error={"code": "AOPS_BUSY_INVALID_MODE", "message": f"Unsupported busy mode: {mode}"},
        )
        return LocalCommandResult(text=text, metadata={})
    saved = _save_busy_input_mode(mode)
    text = _single_response(
        type_="busy.updated",
        command=command_text,
        data={
            "mode": mode,
            "saved": saved,
            "effectiveImmediately": True,
            "usage": "/busy [queue|steer|interrupt|status]",
        },
    )
    return LocalCommandResult(
        text=text,
        metadata={
            "effects": {
                "busyInputMode": mode,
                "reason": "aops_busy_updated",
            }
        },
    )


def _cfg_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _reasoning_status_payload(command_text: str, cfg: dict[str, Any]) -> str:
    from hermes_constants import get_hermes_home, parse_reasoning_effort

    raw_effort = str(_cfg_get(cfg, "agent", "reasoning_effort", default="") or "").strip().lower()
    parsed = parse_reasoning_effort(raw_effort)
    if parsed is None:
        level = "medium"
        enabled = True
        source = "default" if not raw_effort else "invalid_config_default"
        is_default = True
    elif parsed.get("enabled") is False:
        level = "none"
        enabled = False
        source = "config"
        is_default = False
    else:
        level = str(parsed.get("effort") or "medium")
        enabled = True
        source = "config"
        is_default = False
    show_reasoning = _cfg_get(
        cfg,
        "display",
        "platforms",
        "aops",
        "show_reasoning",
        default=_cfg_get(cfg, "display", "show_reasoning", default=False),
    )
    return _single_response(
        type_="reasoning.status",
        command=command_text,
        data={
            "level": level,
            "enabled": enabled,
            "source": source,
            "isDefault": is_default,
            "defaultLevel": "medium",
            "configuredLevel": raw_effort or None,
            "showReasoning": bool(show_reasoning),
            "scope": "global",
            "configPath": str(get_hermes_home() / "config.yaml"),
            "commands": {
                "status": "/reasoning",
                "set": "/reasoning <none|minimal|low|medium|high|xhigh>",
                "show": "/reasoning show",
                "hide": "/reasoning hide",
            },
        },
    )


def _reasoning_command(command_text: str, args: list[str]) -> str:
    from hermes_cli.config import load_config, save_config
    from hermes_constants import get_hermes_home, parse_reasoning_effort

    cfg = load_config()
    if not args or args == ["status"]:
        return _reasoning_status_payload(command_text, cfg)
    if len(args) != 1:
        return _single_response(
            type_="reasoning.status",
            command=command_text,
            data={"allowed": ["none", "minimal", "low", "medium", "high", "xhigh", "show", "hide", "on", "off", "status"]},
            ok=False,
            error={"code": "REASONING_USAGE", "message": "Usage: /reasoning [none|minimal|low|medium|high|xhigh|show|hide|status]"},
        )
    option = str(args[0] or "").strip().lower().replace("_", "-")
    if option in {"show", "on"}:
        display = cfg.setdefault("display", {})
        if not isinstance(display, dict):
            display = {}
            cfg["display"] = display
        platforms = display.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            platforms = {}
            display["platforms"] = platforms
        aops_display = platforms.setdefault("aops", {})
        if not isinstance(aops_display, dict):
            aops_display = {}
            platforms["aops"] = aops_display
        aops_display["show_reasoning"] = True
        save_config(cfg)
        return _single_response(
            type_="reasoning.updated",
            command=command_text,
            data={
                "level": _reasoning_level_from_config(cfg),
                "showReasoning": True,
                "scope": "global",
                "persisted": True,
                "configPath": str(get_hermes_home() / "config.yaml"),
            },
        )
    if option in {"hide", "off"}:
        display = cfg.setdefault("display", {})
        if not isinstance(display, dict):
            display = {}
            cfg["display"] = display
        platforms = display.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            platforms = {}
            display["platforms"] = platforms
        aops_display = platforms.setdefault("aops", {})
        if not isinstance(aops_display, dict):
            aops_display = {}
            platforms["aops"] = aops_display
        aops_display["show_reasoning"] = False
        save_config(cfg)
        return _single_response(
            type_="reasoning.updated",
            command=command_text,
            data={
                "level": _reasoning_level_from_config(cfg),
                "showReasoning": False,
                "scope": "global",
                "persisted": True,
                "configPath": str(get_hermes_home() / "config.yaml"),
            },
        )
    parsed = parse_reasoning_effort(option)
    if parsed is None:
        return _single_response(
            type_="reasoning.status",
            command=command_text,
            data={"allowed": ["none", "minimal", "low", "medium", "high", "xhigh", "show", "hide", "status"]},
            ok=False,
            error={"code": "REASONING_INVALID_OPTION", "message": f"Unsupported reasoning option: {args[0]}"},
        )
    agent = cfg.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        cfg["agent"] = agent
    agent["reasoning_effort"] = option
    save_config(cfg)
    return _single_response(
        type_="reasoning.updated",
        command=command_text,
        data={
            "level": "none" if parsed.get("enabled") is False else str(parsed.get("effort") or option),
            "enabled": parsed.get("enabled"),
            "showReasoning": bool(_cfg_get(cfg, "display", "platforms", "aops", "show_reasoning", default=_cfg_get(cfg, "display", "show_reasoning", default=False))),
            "scope": "global",
            "persisted": True,
            "configPath": str(get_hermes_home() / "config.yaml"),
        },
    )


def _reasoning_level_from_config(cfg: dict[str, Any]) -> str:
    from hermes_constants import parse_reasoning_effort

    raw_effort = str(_cfg_get(cfg, "agent", "reasoning_effort", default="") or "").strip().lower()
    parsed = parse_reasoning_effort(raw_effort)
    if parsed is None:
        return "medium"
    if parsed.get("enabled") is False:
        return "none"
    return str(parsed.get("effort") or "medium")


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def _to_ms(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = int(value)
        if timestamp <= 0:
            return None
        return timestamp if timestamp >= 10_000_000_000 else timestamp * 1000
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        timestamp = int(numeric)
        if timestamp <= 0:
            return None
        return timestamp if timestamp >= 10_000_000_000 else timestamp * 1000
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_history_value(entry: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in entry:
            continue
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _duration_ms(started_at: Any, finished_at: Any) -> Optional[int]:
    started = str(started_at or "").strip()
    finished = str(finished_at or "").strip()
    if not started or not finished:
        return None
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        finished_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta_ms = int((finished_dt - started_dt).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _history_duration_ms(entry: dict[str, Any]) -> Optional[int]:
    duration = _coerce_non_negative_int(
        _first_history_value(
            entry,
            "durationMs",
            "duration_ms",
            "elapsedMs",
            "elapsed_ms",
            "latencyMs",
            "latency_ms",
            "usage",
        )
    )
    if duration is not None:
        return duration
    return _duration_ms(entry.get("started_at") or entry.get("startedAt"), entry.get("finished_at") or entry.get("finishedAt"))


def _history_next_run_ms(entry: dict[str, Any], job: dict[str, Any]) -> Optional[int]:
    for value in (
        _first_history_value(entry, "nextRunAtMs", "next_run_at_ms"),
        _first_history_value(entry, "next_run_at_after", "nextRunAtAfter", "next_run_at", "nextRunAt"),
        _first_history_value(entry, "scheduled_for", "scheduledFor"),
    ):
        ms = _to_ms(value)
        if ms is not None:
            return ms
    return None


def _history_usage(entry: dict[str, Any], duration_ms: Optional[int]) -> dict[str, Any] | None:
    raw_usage = entry.get("usage")
    usage: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    for key in ("usage_details", "usageDetails", "token_usage", "tokenUsage"):
        value = entry.get(key)
        if isinstance(value, dict):
            for usage_key, usage_value in value.items():
                usage.setdefault(usage_key, usage_value)
    if duration_ms is not None:
        usage.setdefault("durationMs", duration_ms)
    return usage or None


def _compact_text(value: Any, *, limit: int = 160) -> Optional[str]:
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return None
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def _job_description(job: dict[str, Any]) -> Optional[str]:
    try:
        from cron.jobs import job_description as _cron_job_description

        description = _cron_job_description(job)
        if description:
            return description
    except Exception:
        pass
    explicit = _compact_text(job.get("description"))
    if explicit:
        return explicit
    for key in ("prompt", "name", "script", "id"):
        value = _compact_text(job.get(key))
        if value:
            return value
    skills = job.get("skills")
    if isinstance(skills, list) and skills:
        return _compact_text(skills[0])
    skill = _compact_text(job.get("skill"))
    if skill:
        return skill
    return None


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


def _iter_jsonl_reverse(path: Path, *, chunk_size: int = 64 * 1024):
    """Yield text lines from a JSONL file newest-first without reading it all."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                parts = buffer.split(b"\n")
                buffer = parts[0]
                for raw_line in reversed(parts[1:]):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line:
                        yield line
            if buffer.strip():
                yield buffer.decode("utf-8", errors="replace").strip()
    except OSError:
        return


def _iter_cron_history_entries_newest_first(path: Path):
    if not path.exists():
        return
    for line in _iter_jsonl_reverse(path):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            yield entry


def _skill_description_zh(name: str, frontmatter: dict[str, Any], description: Any) -> str:
    for key in ("description_zh", "descriptionZh", "zh_description"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    normalized = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    mapped = _SKILL_DESCRIPTION_ZH.get(normalized)
    if mapped:
        return mapped
    return str(description or "").strip()


def _skill_ref_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").lstrip("/")


def _skills_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "enabled": sum(1 for item in items if item.get("enabled") is True),
        "disabled": sum(1 for item in items if item.get("disabled") is True),
        "categories": len({item.get("category") or "uncategorized" for item in items}),
    }


def _skill_items() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from agent.skill_commands import get_skill_commands, scan_skill_commands
    from agent.skill_utils import iter_skill_index_files
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills
    from tools.skills_tool import SKILLS_DIR, _parse_frontmatter, skill_matches_platform

    skills_root = SKILLS_DIR
    items: list[dict[str, Any]] = []
    disabled = get_disabled_skills(load_config())
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
            category = item_id.split("/", 1)[0] if "/" in item_id else None
            description = frontmatter.get("description") or None
            enabled = name not in disabled
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
                    "description": description,
                    "descriptionZh": _skill_description_zh(name, frontmatter, description),
                    "category": category,
                    "enabled": enabled,
                    "disabled": not enabled,
                    "homepage": frontmatter.get("homepage") or frontmatter.get("url") or None,
                    "command": command,
                    "path": str(skill_md),
                }
            )
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    return items, {"skillsRoot": str(skills_root)}


def _skills_command(command_text: str, args: list[str]) -> str:
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills

    action = str(args[0] if args else "list").strip().lower().replace("_", "-")
    if action in {"", "list"}:
        try:
            items, context = _skill_items()
            context.update({"agentId": "main", "workspaceDir": os.getcwd()})
            return _list_response(
                type_="skills.list",
                command=command_text,
                item_type="skill",
                items=items,
                context=context,
                summary=_skills_summary(items),
            )
        except Exception as exc:
            return _list_response(
                type_="skills.list",
                command=command_text,
                item_type="skill",
                items=[],
                error={"code": "SKILLS_READ_FAILED", "message": str(exc)},
            )

    if action not in {"enable", "disable", "set", "uninstall", "remove"}:
        return _list_response(
            type_="skills.updated",
            command=command_text,
            item_type="skill",
            items=[],
            error={
                "code": "SKILLS_USAGE",
                "message": "Usage: /skills list | /skills enable <name> | /skills disable <name> | /skills set <name> <true|false> | /skills uninstall <name>",
            },
        )
    if len(args) < 2:
        return _list_response(
            type_="skills.updated",
            command=command_text,
            item_type="skill",
            items=[],
            error={"code": "SKILL_NAME_REQUIRED", "message": "Skill name is required."},
        )

    if action in {"uninstall", "remove"}:
        from gateway.aops_skill_uninstall import (
            is_not_hub_installed_message,
            uninstall_local_skill_fallback,
        )
        from tools.skills_hub import uninstall_skill

        target_ref = " ".join(args[1:]).strip()
        try:
            ok, message = uninstall_skill(target_ref)
        except Exception as exc:
            ok, message = False, str(exc)
        if ok:
            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache

                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
            try:
                from agent.skill_commands import reload_skills

                reload_skills()
            except Exception:
                pass
            items_after, context_after = _skill_items()
            context_after.update({"agentId": "main", "workspaceDir": os.getcwd()})
            return _list_response(
                type_="skills.updated",
                command=command_text,
                item_type="skill",
                items=items_after,
                context=context_after,
                summary=_skills_summary(items_after),
                updated={
                    "name": target_ref,
                    "action": "uninstall",
                    "source": "hub",
                    "removedPath": None,
                    "message": message,
                },
            )
        if not is_not_hub_installed_message(message):
            return _list_response(
                type_="skills.updated",
                command=command_text,
                item_type="skill",
                items=[],
                error={"code": "SKILL_UNINSTALL_FAILED", "message": message or f"Failed to uninstall `{target_ref}`."},
            )
        try:
            items, context = _skill_items()
        except Exception as exc:
            return _list_response(
                type_="skills.updated",
                command=command_text,
                item_type="skill",
                items=[],
                error={"code": "SKILLS_READ_FAILED", "message": str(exc)},
            )
        result = uninstall_local_skill_fallback(target_ref, items=items)
        if not result.get("ok"):
            return _list_response(
                type_="skills.updated",
                command=command_text,
                item_type="skill",
                items=[],
                context=result.get("context") or context,
                summary=_skills_summary(items),
                error=result.get("error") or {"code": "SKILL_UNINSTALL_FAILED", "message": f"Failed to uninstall `{target_ref}`."},
            )
        items_after, context_after = _skill_items()
        context_after.update({"agentId": "main", "workspaceDir": os.getcwd()})
        return _list_response(
            type_="skills.updated",
            command=command_text,
            item_type="skill",
            items=items_after,
            context=context_after,
            summary=_skills_summary(items_after),
            updated={
                "name": result.get("name") or target_ref,
                "action": "uninstall",
                "source": "local",
                "removedPath": result.get("removedPath"),
                "message": result.get("message"),
            },
        )

    requested_enabled = action == "enable"
    target_ref = " ".join(args[1:]).strip()
    if action == "set":
        if len(args) < 3 or str(args[-1]).strip().lower() not in {"true", "false"}:
            return _list_response(
                type_="skills.updated",
                command=command_text,
                item_type="skill",
                items=[],
                error={"code": "SKILL_SET_USAGE", "message": "Usage: /skills set <name> <true|false>"},
            )
        requested_enabled = str(args[-1]).strip().lower() == "true"
        target_ref = " ".join(args[1:-1]).strip()

    try:
        items, context = _skill_items()
    except Exception as exc:
        return _list_response(
            type_="skills.updated",
            command=command_text,
            item_type="skill",
            items=[],
            error={"code": "SKILLS_READ_FAILED", "message": str(exc)},
        )

    target_key = _skill_ref_key(target_ref)
    matched = None
    for item in items:
        candidates = {
            _skill_ref_key(item.get("name")),
            _skill_ref_key(item.get("id")),
            _skill_ref_key(item.get("command")),
        }
        if target_key in candidates:
            matched = item
            break
    if matched is None:
        return _list_response(
            type_="skills.updated",
            command=command_text,
            item_type="skill",
            items=[],
            context=context,
            summary=_skills_summary(items),
            error={"code": "SKILL_NOT_FOUND", "message": f"Skill `{target_ref}` not found."},
        )

    cfg = load_config()
    disabled = get_disabled_skills(cfg)
    skill_name = str(matched.get("name") or "")
    if requested_enabled:
        disabled.discard(skill_name)
    else:
        disabled.add(skill_name)
    save_disabled_skills(cfg, disabled)

    items_after, context_after = _skill_items()
    updated_item = next(
        (item for item in items_after if _skill_ref_key(item.get("name")) == _skill_ref_key(skill_name)),
        {**matched, "enabled": requested_enabled, "disabled": not requested_enabled},
    )
    context_after.update({"agentId": "main", "workspaceDir": os.getcwd()})
    return _list_response(
        type_="skills.updated",
        command=command_text,
        item_type="skill",
        items=[updated_item],
        context=context_after,
        summary=_skills_summary(items_after),
        updated={"name": skill_name, "enabled": requested_enabled},
    )


def _aops_agent_id(event: MessageEvent) -> str:
    raw = event.raw_message if isinstance(event.raw_message, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    value = (
        raw.get("agentId")
        or metadata.get("agentId")
        or getattr(event.source, "agent_key", None)
        or ""
    )
    text = str(value or "").strip()
    return text or "main"


def _toolset_context(event: MessageEvent) -> dict[str, Any]:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    profile_name = "default"
    try:
        if home.parent.name == "profiles":
            profile_name = home.name or "default"
    except Exception:
        profile_name = "default"
    return {
        "platform": "aops",
        "agentId": _aops_agent_id(event),
        "profileName": profile_name,
        "profileHome": str(home),
        "configPath": str(home / "config.yaml"),
    }


def _normalize_toolset_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _merge_toolset_ui_config(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    toolsets_cfg = source.get("toolsets")
    if isinstance(toolsets_cfg, dict):
        for key in ("disabled", "unsupportedReasons", "unsupported_reasons"):
            if key in toolsets_cfg:
                target[key] = toolsets_cfg[key]


def _raw_aops_toolset_ui_config() -> dict[str, Any]:
    """Read AOPS toolset UI config directly from the active profile file.

    `/toolsets` uses `load_config()` for effective runtime toolset state, but
    the UI-disabled list is a small AOPS/Tec01 presentation setting. Reading it
    directly avoids stale merged-config cache edge cases after one-click updates
    or manual config edits, and lets us support the common platform-scoped
    shapes used by templates.
    """
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "config.yaml"
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    merged: dict[str, Any] = {}
    _merge_toolset_ui_config(merged, raw.get("aops"))
    platforms = raw.get("platforms")
    aops_platform = platforms.get("aops") if isinstance(platforms, dict) else {}
    _merge_toolset_ui_config(merged, aops_platform)
    if isinstance(aops_platform, dict):
        extra = aops_platform.get("extra")
        _merge_toolset_ui_config(merged, extra)
    return merged


def _configured_toolset_ui_state(cfg: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    toolsets_cfg: dict[str, Any] = {}
    aops_cfg = cfg.get("aops") if isinstance(cfg, dict) else {}
    if isinstance(aops_cfg, dict) and isinstance(aops_cfg.get("toolsets"), dict):
        toolsets_cfg.update(aops_cfg["toolsets"])
    toolsets_cfg.update(_raw_aops_toolset_ui_config())

    raw_disabled = toolsets_cfg.get("disabled")
    if isinstance(raw_disabled, list):
        ui_disabled = {_normalize_toolset_name(item) for item in raw_disabled if _normalize_toolset_name(item)}
        reasons: dict[str, str] = {}
    else:
        ui_disabled = set(_LINUX_TERMINAL_UNSUPPORTED_REASONS)
        reasons = dict(_LINUX_TERMINAL_UNSUPPORTED_REASONS)
    raw_reasons = toolsets_cfg.get("unsupportedReasons")
    if raw_reasons is None:
        raw_reasons = toolsets_cfg.get("unsupported_reasons")
    if isinstance(raw_reasons, dict):
        for raw_name, raw_reason in raw_reasons.items():
            name = _normalize_toolset_name(raw_name)
            if not name:
                continue
            reason = str(raw_reason or "").strip()
            if reason:
                reasons[name] = reason
            else:
                reasons.pop(name, None)
    return ui_disabled, reasons


def _toolset_item(
    name: str,
    label: str,
    description: str,
    *,
    enabled: bool,
    configured: bool,
    ui_disabled: bool,
    unsupported_reason: str | None,
) -> dict[str, Any]:
    from toolsets import resolve_toolset

    return {
        "name": name,
        "label": re.sub(r"^[^\w\u4e00-\u9fff]+", "", str(label or "")).strip() or name,
        "description": description,
        "descriptionZh": _TOOLSET_DESCRIPTION_ZH.get(name) or description,
        "enabled": enabled,
        "disabled": ui_disabled,
        "configured": configured,
        "configurable": configured and not ui_disabled,
        "unsupportedReason": unsupported_reason,
        "tools": resolve_toolset(name),
    }


def _toolset_payload(
    *,
    command_text: str,
    event: MessageEvent,
    type_: str = "toolsets.list",
    updated: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> str:
    item_list = items or []
    payload = {
        "schemaVersion": LOCAL_LIST_SCHEMA,
        "type": type_,
        "ok": error is None,
        "command": command_text,
        "itemType": "toolset",
        "total": len(item_list),
        "count": len(item_list),
        "limit": None,
        "hasMore": False,
        "context": _toolset_context(event),
        "summary": summary or {},
        "items": item_list,
        "error": error,
    }
    if updated is not None:
        payload["updated"] = updated
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _toolset_list_data() -> tuple[list[dict[str, Any]], dict[str, Any], set[str], dict[str, tuple[str, str]]]:
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_aops_dashboard_toolsets,
        _toolset_allowed_for_platform,
        _toolset_has_keys,
    )

    cfg = load_config()
    enabled = _get_aops_dashboard_toolsets(cfg, include_default_mcp_servers=False)
    ui_disabled, unsupported_reasons = _configured_toolset_ui_state(cfg)
    definitions = {
        name: (label, description)
        for name, label, description in _get_effective_configurable_toolsets()
        if _toolset_allowed_for_platform(name, "cli")
    }
    items: list[dict[str, Any]] = []
    for name in sorted(definitions):
        label, description = definitions[name]
        configured = bool(_toolset_has_keys(name, cfg))
        items.append(
            _toolset_item(
                name,
                label,
                description,
                enabled=name in enabled,
                configured=configured,
                ui_disabled=name in ui_disabled,
                unsupported_reason=unsupported_reasons.get(name),
            )
        )
    summary = {
        "enabled": sum(1 for item in items if item["enabled"]),
        "disabled": sum(1 for item in items if not item["enabled"]),
        "configured": sum(1 for item in items if item["configured"]),
        "uiDisabled": sum(1 for item in items if item["disabled"]),
    }
    return items, summary, enabled, definitions


def _toolsets_command(command_text: str, event: MessageEvent, args: list[str]) -> str:
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _save_aops_dashboard_toolsets

    action = str(args[0] if args else "list").strip().lower().replace("_", "-")
    if action in {"", "list"}:
        try:
            items, summary, _enabled, _definitions = _toolset_list_data()
            return _toolset_payload(command_text=command_text, event=event, items=items, summary=summary)
        except Exception as exc:
            return _toolset_payload(
                command_text=command_text,
                event=event,
                error={"code": "TOOLSETS_READ_FAILED", "message": str(exc)},
            )

    if action not in {"enable", "disable", "set"}:
        return _toolset_payload(
            command_text=command_text,
            event=event,
            type_="toolsets.updated",
            error={
                "code": "TOOLSETS_USAGE",
                "message": "Usage: /toolsets list | /toolsets enable <name> | /toolsets disable <name> | /toolsets set <name> <true|false>",
            },
        )

    if len(args) < 2:
        return _toolset_payload(
            command_text=command_text,
            event=event,
            type_="toolsets.updated",
            error={"code": "TOOLSET_NAME_REQUIRED", "message": "Toolset name is required."},
        )

    target = str(args[1] or "").strip().lower()
    requested_enabled: bool
    if action == "set":
        if len(args) != 3 or str(args[2]).strip().lower() not in {"true", "false"}:
            return _toolset_payload(
                command_text=command_text,
                event=event,
                type_="toolsets.updated",
                error={"code": "TOOLSET_SET_USAGE", "message": "Usage: /toolsets set <name> <true|false>"},
            )
        requested_enabled = str(args[2]).strip().lower() == "true"
    else:
        requested_enabled = action == "enable"

    try:
        _items, _summary, enabled, definitions = _toolset_list_data()
    except Exception as exc:
        return _toolset_payload(
            command_text=command_text,
            event=event,
            type_="toolsets.updated",
            error={"code": "TOOLSETS_READ_FAILED", "message": str(exc)},
        )

    if target not in definitions:
        return _toolset_payload(
            command_text=command_text,
            event=event,
            type_="toolsets.updated",
            error={"code": "TOOLSET_NOT_FOUND", "message": f"Toolset `{target}` not found."},
        )

    cfg = load_config()
    updated_enabled = set(enabled)
    if requested_enabled:
        updated_enabled.add(target)
    else:
        updated_enabled.discard(target)
    _save_aops_dashboard_toolsets(cfg, updated_enabled)

    items, summary, _enabled_after, _definitions_after = _toolset_list_data()
    updated_item = [item for item in items if item.get("name") == target]
    return _toolset_payload(
        command_text=command_text,
        event=event,
        type_="toolsets.updated",
        items=updated_item,
        summary=summary,
        updated={"name": target, "enabled": requested_enabled},
    )


def _cron_channel_error(command_text: str, channel_value: Any) -> str:
    return _list_response(
        type_="cron.list",
        command=command_text,
        item_type="cron.task",
        items=[],
        error={
            "code": "CRON_INVALID_CHANNEL",
            "message": "Cron channel must be one of: tec01, anyi.",
            "details": {"channel": channel_value},
        },
    )


def _cron_parse_list_channel(args: list[str]) -> list[str] | None:
    if not args:
        return None
    if args == ["list"]:
        return None
    if len(args) == 2 and args[0] == "list":
        channel = args[1].strip().lower()
        if channel in {"tec01", "anyi"}:
            return [channel]
    raise ValueError("invalid cron list channel")


def _cron_item(job: dict[str, Any], latest_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    latest_entry = latest_entry or {}
    state = str(job.get("state") or "").lower()
    is_enabled = bool(job.get("enabled", True))
    is_schedulable = is_enabled and state not in {"paused", "completed", "deleted", "disabled"}
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    schedule_flags = _cron_schedule_flags(job)
    channel = list(job.get("channel") or ["tec01"])
    channel_id = _cron_channel_id(job)
    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "description": _job_description(job),
        "enabled": is_enabled,
        "agentId": None,
        "sessionKey": None,
        "sessionTarget": None,
        "wakeMode": None,
        "deleteAfterRun": schedule_flags["isOneShot"],
        **schedule_flags,
        "createdAtMs": _to_ms(job.get("created_at")),
        "updatedAtMs": _to_ms(job.get("updated_at") or job.get("created_at")),
        "scheduleText": job.get("schedule_display") or schedule.get("display"),
        "payloadKind": "agentTurn",
        "payloadSummary": (str(job.get("prompt") or "").strip() or None),
        "prompt": (str(job.get("prompt") or "").strip() or None),
        "payloadModel": job.get("model"),
        "payloadFallbacks": None,
        "payloadThinking": None,
        "payloadTimeoutSeconds": None,
        "payloadAllowUnsafeExternalContent": None,
        "payloadLightContext": None,
        "payloadToolsAllow": job.get("enabled_toolsets"),
        "payloadExternalContentSource": None,
        "nextRunAtMs": _to_ms(job.get("next_run_at")) if is_schedulable else None,
        "lastRunAtMs": _to_ms(job.get("last_run_at")),
        "runningAtMs": None,
        "lastRunStatus": job.get("last_status"),
        "lastError": job.get("last_error"),
        "lastErrorReason": None,
        "lastDurationMs": _history_duration_ms(latest_entry),
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
        "channelId": channel_id,
        "failureAlertText": None,
        "state": state or ("scheduled" if is_enabled else "disabled"),
        "origin": job.get("origin"),
        "skills": job.get("skills") or [],
        "workdir": job.get("workdir"),
        "channel": channel,
    }


def _cron_items(channel_filter: list[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from cron import jobs as cron_jobs

    jobs = cron_jobs.list_jobs(include_disabled=True)
    history_path = Path(cron_jobs.HISTORY_FILE)
    history_by_job: dict[str, dict[str, Any]] = {}
    wanted_job_ids = {str(job.get("id") or "").strip() for job in jobs if str(job.get("id") or "").strip()}
    if history_path.exists() and wanted_job_ids:
        for entry in _iter_cron_history_entries_newest_first(history_path):
            job_id = str(entry.get("job_id") or "").strip()
            if job_id and job_id in wanted_job_ids and job_id not in history_by_job:
                history_by_job[job_id] = entry
                if len(history_by_job) >= len(wanted_job_ids):
                    break
    items: list[dict[str, Any]] = []
    enabled = 0
    disabled = 0
    for job in jobs:
        channel = list(job.get("channel") or ["tec01"])
        if channel_filter and not any(item in channel for item in channel_filter):
            continue
        latest_entry = history_by_job.get(str(job.get("id")), {})
        is_enabled = bool(job.get("enabled", True))
        enabled += 1 if is_enabled else 0
        disabled += 0 if is_enabled else 1
        items.append(_cron_item(job, latest_entry))
    context = {"storePath": str(cron_jobs.JOBS_FILE)}
    summary = {"enabled": enabled, "disabled": disabled}
    return items, context, summary


def _expand_env_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\${([^}]+)}", lambda m: os.environ.get(m.group(1), m.group(0)), text)


_SECRET_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MODEL_KEY_FIELDS = ("api_key", "apiKey", "apikey", "key", "token", "authorization", "auth_token")
_MODEL_KEY_ENV_FIELDS = (
    "key_env",
    "api_key_env",
    "apiKeyEnv",
    "apikey_env",
    "apiKeyENV",
    "token_env",
    "api_key_ref",
    "apiKeyRef",
)
_MODEL_STATUS_TOKENS = {"status", "current", "stutus", "stauts", "stattus", "statsu", "curent"}
_MODEL_LIST_TOKENS = {"list"}
_MODEL_RESERVED_TOKENS = {*_MODEL_STATUS_TOKENS, *_MODEL_LIST_TOKENS, "use"}
_MODEL_LIST_CACHE: dict[tuple[str, str, str, str, str], tuple[float, dict[str, Any]]] = {}


def _model_list_cache_key(endpoint: dict[str, Any]) -> tuple[str, str, str, str, str]:
    api_key = str(endpoint.get("apiKey") or "")
    api_key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else ""
    return (
        str(endpoint.get("provider") or ""),
        str(endpoint.get("baseUrl") or ""),
        str(endpoint.get("apiMode") or ""),
        str(endpoint.get("apiKeyRef") or ""),
        api_key_digest,
    )


def _infer_aops_api_mode(provider: Any, base_url: Any) -> str:
    provider_text = str(provider or "").strip().lower()
    url_lower = str(base_url or "").strip().rstrip("/").lower()
    hostname = ""
    if url_lower:
        try:
            from urllib.parse import urlparse

            hostname = urlparse(url_lower).hostname or ""
        except Exception:
            hostname = ""
    if url_lower.endswith("/anthropic") or hostname == "api.anthropic.com":
        return "anthropic_messages"
    if hostname == "api.kimi.com" and "/coding" in url_lower:
        return "anthropic_messages"
    if hostname == "api.openai.com":
        return "codex_responses"
    if provider_text == "bedrock" or (hostname.startswith("bedrock-runtime.") and hostname.endswith("amazonaws.com")):
        return "bedrock_converse"
    return "chat_completions"


def _looks_unresolved_secret_ref(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and re.search(r"\{[^}]*\}", text))


def _env_value(name: Any) -> str:
    key = str(name or "").strip()
    if not key:
        return ""
    value = os.environ.get(key)
    if value:
        return value
    try:
        from hermes_cli.config import get_env_value

        return str(get_env_value(key) or "").strip()
    except Exception:
        return ""


def _resolve_secret_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("env:"):
        return _env_value(text[4:])
    if text.startswith("$") and "{" not in text:
        return _env_value(text[1:])
    expanded = _expand_env_ref(text)
    if expanded != text:
        return expanded
    if _SECRET_ENV_NAME_RE.match(text):
        return _env_value(text) or text
    if _looks_unresolved_secret_ref(text):
        return ""
    return expanded


def _first_mapping_value(mapping: Any, fields: Iterable[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for field in fields:
        value = mapping.get(field)
        if value is not None and str(value).strip():
            return value
    return None


def _resolve_model_api_key(mapping: Any) -> tuple[str, str]:
    raw_value = _first_mapping_value(mapping, _MODEL_KEY_FIELDS)
    if raw_value is not None:
        resolved = _resolve_secret_ref(raw_value)
        return (resolved if not _looks_unresolved_secret_ref(resolved) else ""), str(raw_value).strip()
    env_name = _first_mapping_value(mapping, _MODEL_KEY_ENV_FIELDS)
    if env_name is not None:
        return _env_value(env_name), str(env_name).strip()
    return "", ""


def _is_reserved_model_token(value: Any) -> bool:
    text = str(value or "").strip().lower()
    normalized = re.sub(r"^[^a-z0-9_.:/-]+|[^a-z0-9_.:/-]+$", "", text)
    return normalized in _MODEL_RESERVED_TOKENS


def _redact_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "***" if text else ""
    return f"{text[:4]}...{text[-4:]}"


def _resolve_aops_model_endpoint(event: MessageEvent | None = None) -> dict[str, Any]:
    from gateway import aops_state
    from hermes_cli.config import get_compatible_custom_providers, load_config

    cfg = load_config()
    pref = None
    pref_key = None
    raw = event.raw_message if event and isinstance(event.raw_message, dict) else {}
    agent_key = str(raw.get("agentKey") or raw.get("agentId") or "main")
    if event and event.source:
        pref_key = aops_state.preference_key(
            platform=str(event.source.platform.value if event.source.platform else "aops"),
            channel_id=str(event.source.chat_id or ""),
            agent_key=agent_key,
        )
        pref = aops_state.get_model_preference(pref_key)
        if pref and _is_reserved_model_token(pref.get("model")):
            try:
                aops_state.delete_model_preference(pref_key)
            except Exception:
                pass
            pref = None

    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    current_model = ""
    current_provider = "custom"
    base_url = ""
    api_key = ""
    api_key_ref = ""
    api_mode = ""
    source = "config.model"
    if isinstance(model_cfg, dict):
        current_model = str(model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name") or "")
        current_provider = str(model_cfg.get("provider") or current_provider)
        base_url = _expand_env_ref(model_cfg.get("base_url"))
        api_key, api_key_ref = _resolve_model_api_key(model_cfg)
        api_mode = str(model_cfg.get("api_mode") or "")
    elif isinstance(model_cfg, str):
        current_model = model_cfg

    if pref:
        current_model = str(pref.get("model") or current_model)
        current_provider = str(pref.get("provider") or current_provider)
        base_url = _expand_env_ref(pref.get("base_url")) or base_url
        pref_api_key, pref_api_key_ref = _resolve_model_api_key(pref)
        api_key = pref_api_key or api_key
        api_key_ref = pref_api_key_ref or api_key_ref
        api_mode = str(pref.get("api_mode") or api_mode)
        source = "aops.channelPreference"

    if isinstance(cfg, dict):
        try:
            custom_providers = get_compatible_custom_providers(cfg)
        except Exception:
            custom_providers = cfg.get("custom_providers") if isinstance(cfg.get("custom_providers"), list) else []
        provider_norm = current_provider.strip().lower()
        for entry in custom_providers or []:
            if not isinstance(entry, dict):
                continue
            names = {
                str(entry.get("name") or "").strip().lower(),
                str(entry.get("provider_key") or "").strip().lower(),
            }
            if provider_norm and provider_norm not in names and provider_norm.replace("custom:", "") not in names:
                continue
            base_url = base_url or _expand_env_ref(entry.get("base_url") or entry.get("url") or entry.get("api"))
            entry_api_key, entry_api_key_ref = _resolve_model_api_key(entry)
            api_key = api_key or entry_api_key
            api_key_ref = api_key_ref or entry_api_key_ref
            api_mode = api_mode or str(entry.get("api_mode") or entry.get("transport") or "")
            source = source if source == "aops.channelPreference" else "config.custom_providers"
            break

    if isinstance(cfg, dict) and not base_url:
        providers = cfg.get("providers")
        if isinstance(providers, dict):
            entry = providers.get(current_provider) or providers.get(current_provider.strip().lower())
            if isinstance(entry, dict):
                base_url = _expand_env_ref(entry.get("base_url") or entry.get("api") or entry.get("url"))
                api_key, api_key_ref = _resolve_model_api_key(entry)
                api_mode = api_mode or str(entry.get("api_mode") or entry.get("transport") or "")
                source = "config.providers"

    if not api_key:
        for env_name in ("MODEL_GATEWAY_API_KEY", "AOPS_MODEL_GATEWAY_KEY", "CUSTOM_API_KEY", "MODEL_API_KEY", "LM_API_KEY"):
            api_key = _env_value(env_name)
            if api_key:
                api_key_ref = env_name
                break

    api_mode = api_mode or _infer_aops_api_mode(current_provider, base_url)
    return {
        "provider": current_provider,
        "model": current_model,
        "baseUrl": base_url.rstrip("/"),
        "apiKey": api_key,
        "apiKeyRef": api_key_ref,
        "apiMode": api_mode,
        "source": source,
        "preferenceKey": pref_key,
    }


def _model_item(model_id: str, *, current_model: str = "", provider: str = "custom") -> dict[str, Any]:
    return {
        "id": model_id,
        "model": model_id,
        "displayName": model_id,
        "current": bool(current_model and model_id == current_model),
        "command": f"/model use {provider or 'custom'} {model_id}",
    }


def _current_model_payload(event: MessageEvent | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    from hermes_constants import get_hermes_home
    from hermes_cli.models import probe_api_models

    endpoint = _resolve_aops_model_endpoint(event)
    started = time.monotonic()
    models: list[str] | None = None
    probe: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    if not endpoint["baseUrl"]:
        error = {
            "code": "MODEL_GATEWAY_NOT_CONFIGURED",
            "message": "当前用户未配置模型网关 base_url，无法查询模型列表。",
        }
    else:
        cache_ttl = 0.0
        try:
            cache_ttl = float(os.getenv("AOPS_MODEL_LIST_CACHE_TTL_SECONDS", "60"))
        except ValueError:
            cache_ttl = 60.0
        cache_key = _model_list_cache_key(endpoint)
        cached = _MODEL_LIST_CACHE.get(cache_key)
        if cached and cache_ttl > 0 and (time.monotonic() - cached[0]) <= cache_ttl:
            probe = dict(cached[1])
            probe["cache_hit"] = True
        else:
            probe = {}
        try_alternate = os.getenv("AOPS_MODEL_LIST_TRY_ALTERNATE", "").strip().lower() in {"1", "true", "yes", "on"}
        if not probe:
            probe = probe_api_models(
                endpoint["apiKey"],
                endpoint["baseUrl"],
                timeout=float(os.getenv("AOPS_MODEL_LIST_TIMEOUT", "1.5")),
                api_mode=endpoint["apiMode"],
                try_alternate=try_alternate,
            )
            if isinstance(probe.get("models"), list):
                _MODEL_LIST_CACHE[cache_key] = (time.monotonic(), dict(probe))
        raw_models = probe.get("models")
        if isinstance(raw_models, list):
            models = [str(item) for item in raw_models if str(item or "").strip()]
        else:
            error = {
                "code": "MODEL_GATEWAY_FETCH_FAILED",
                "message": "无法从当前模型网关获取模型列表。",
                "details": {
                    "probedUrl": probe.get("probed_url"),
                    "resolvedBaseUrl": probe.get("resolved_base_url"),
                    "suggestedBaseUrl": probe.get("suggested_base_url"),
                    "baseUrl": endpoint["baseUrl"],
                    "apiKeyConfigured": bool(endpoint["apiKey"]),
                    "apiKeyPreview": _redact_secret(endpoint["apiKey"]),
                    "apiMode": endpoint["apiMode"],
                },
            }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    current_model = str(endpoint.get("model") or "")
    payload = {
        "providers": [
            {
                "slug": endpoint["provider"],
                "name": endpoint["provider"],
                "baseUrl": endpoint["baseUrl"],
                "apiMode": endpoint["apiMode"],
                "source": endpoint["source"],
                "authenticated": bool(endpoint["apiKey"]),
                "models": models or [],
                "totalModels": len(models or []),
                "isCurrent": True,
            }
        ] if endpoint["baseUrl"] else [],
        "items": [
            _model_item(model_id, current_model=current_model, provider=endpoint["provider"])
            for model_id in (models or [])
        ],
        "model": current_model,
        "provider": endpoint["provider"],
        "selected": {
            "model": current_model,
            "provider": endpoint["provider"],
            "base_url": endpoint["baseUrl"],
            "api_mode": endpoint["apiMode"],
        },
        "error": error,
    }
    context = {
        "preferenceKey": endpoint["preferenceKey"],
        "statePath": str(get_hermes_home() / "aops" / "channel-state.json"),
        "source": endpoint["source"],
        "baseUrl": endpoint["baseUrl"],
        "apiKeyConfigured": bool(endpoint["apiKey"]),
        "apiKeyPreview": _redact_secret(endpoint["apiKey"]),
        "apiMode": endpoint["apiMode"],
        "probedUrl": probe.get("probed_url"),
        "resolvedBaseUrl": probe.get("resolved_base_url"),
        "suggestedBaseUrl": probe.get("suggested_base_url"),
        "usedFallback": probe.get("used_fallback"),
        "cacheHit": bool(probe.get("cache_hit")),
        "elapsedMs": elapsed_ms,
    }
    return payload, context


def _aops_model_list(command_text: str, event: MessageEvent) -> str:
    payload, context = _current_model_payload(event)
    return _list_response(
        type_="model.list",
        command=command_text,
        item_type="model",
        items=payload.get("items") or [],
        context={
            **context,
            "currentModel": payload.get("model"),
            "currentProvider": payload.get("provider"),
            "selected": payload.get("selected"),
            "providers": payload.get("providers") or [],
        },
        summary={"model": payload.get("model"), "provider": payload.get("provider")},
        error=payload.get("error"),
    )


def _aops_model_status(command_text: str, event: MessageEvent) -> str:
    from hermes_constants import get_hermes_home
    from hermes_cli.config import get_config_path

    endpoint = _resolve_aops_model_endpoint(event)
    data = {
        "model": endpoint.get("model") or "",
        "modelId": endpoint.get("model") or "",
        "provider": endpoint.get("provider") or "",
        "providerLabel": endpoint.get("provider") or "",
        "baseUrl": endpoint.get("baseUrl") or "",
        "apiMode": endpoint.get("apiMode") or "",
        "apiKeyConfigured": bool(endpoint.get("apiKey")),
        "apiKeyPreview": _redact_secret(str(endpoint.get("apiKey") or "")),
        "source": endpoint.get("source") or "",
        "scope": "aops-channel" if endpoint.get("source") == "aops.channelPreference" else "config",
        "preferenceKey": endpoint.get("preferenceKey"),
        "statePath": str(get_hermes_home() / "aops" / "channel-state.json"),
        "configPath": str(get_config_path()),
        "commands": {
            "list": "/model list",
            "status": "/model status",
            "switch": f"/model use {endpoint.get('provider') or 'custom'} <model>",
        },
    }
    return _single_response(
        type_="model.status",
        command=command_text,
        data=data,
    )


def _persist_aops_model_config(
    *,
    model: str,
    provider: str,
    base_url: str,
    api_key: str,
    api_key_ref: str,
    api_mode: str,
) -> dict[str, Any]:
    from hermes_cli.config import get_config_path, load_config, read_raw_config, save_config

    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    raw_cfg = read_raw_config()
    raw_model = raw_cfg.get("model") if isinstance(raw_cfg, dict) else {}
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
        cfg["model"] = model_cfg

    model_cfg["provider"] = provider or "custom"
    model_cfg["default"] = model
    model_cfg["model"] = model
    if base_url:
        model_cfg["base_url"] = base_url
    if api_mode:
        model_cfg["api_mode"] = api_mode
    raw_key = _first_mapping_value(raw_model, _MODEL_KEY_FIELDS) if isinstance(raw_model, dict) else None
    raw_key_env = _first_mapping_value(raw_model, _MODEL_KEY_ENV_FIELDS) if isinstance(raw_model, dict) else None
    if raw_key is not None and not _looks_unresolved_secret_ref(raw_key):
        model_cfg["api_key"] = str(raw_key).strip()
    elif raw_key_env is not None:
        model_cfg["api_key_env"] = str(raw_key_env).strip()
        model_cfg.pop("api_key", None)
    elif api_key_ref and _SECRET_ENV_NAME_RE.match(api_key_ref):
        model_cfg["api_key_env"] = api_key_ref
        model_cfg.pop("api_key", None)
    elif api_key:
        model_cfg["api_key"] = api_key

    save_config(cfg)
    return {"updated": True, "path": str(get_config_path())}


def _aops_model_use(command_text: str, event: MessageEvent, args: list[str]) -> str:
    from gateway import aops_state
    from hermes_cli.model_switch import switch_model
    from hermes_cli.config import load_config

    if args[:1] == ["use"]:
        if len(args) < 3:
            return _single_response(
                type_="model.switch",
                command=command_text,
                data={"allowedUsage": "/model use <provider> <model>"},
                ok=False,
                error={"code": "MODEL_USAGE", "message": "Usage: /model use <provider> <model>"},
            )
        explicit_provider = args[1]
        model_input = " ".join(args[2:]).strip()
    else:
        return _single_response(
            type_="model.switch",
            command=command_text,
            data={"allowedUsage": "/model use <provider> <model>"},
            ok=False,
            error={"code": "MODEL_USAGE", "message": "Usage: /model use <provider> <model>"},
        )

    if not model_input or _is_reserved_model_token(model_input):
        return _single_response(
            type_="model.switch",
            command=command_text,
            data={"allowedUsage": "/model use <provider> <model>"},
            ok=False,
            error={"code": "MODEL_USAGE", "message": "Usage: /model use <provider> <model>"},
        )

    cfg = load_config()
    endpoint = _resolve_aops_model_endpoint(event)
    current_model = str(endpoint.get("model") or "")
    current_provider = str(endpoint.get("provider") or "custom")
    current_base_url = str(endpoint.get("baseUrl") or "")
    current_api_key = str(endpoint.get("apiKey") or "")
    current_api_key_ref = str(endpoint.get("apiKeyRef") or "")
    current_api_mode = str(endpoint.get("apiMode") or "")

    provider_alias = str(explicit_provider or "").strip().lower()
    current_provider_aliases = {
        "",
        "custom",
        current_provider.strip().lower(),
        str(endpoint.get("source") or "").strip().lower(),
    }
    if provider_alias in current_provider_aliases or provider_alias.startswith("custom:"):
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        agent_key = str(raw.get("agentKey") or raw.get("agentId") or "main")
        pref_key = aops_state.preference_key(
            platform=str(event.source.platform.value if event.source and event.source.platform else "aops"),
            channel_id=str(event.source.chat_id if event.source else ""),
            agent_key=agent_key,
        )
        target_provider = explicit_provider or current_provider or "custom"
        preference = {
            "model": model_input,
            "provider": target_provider,
            "api_key": current_api_key,
            "api_key_ref": current_api_key_ref,
            "base_url": current_base_url,
            "api_mode": current_api_mode,
        }
        aops_state.set_model_preference(pref_key, preference)
        config_update = _persist_aops_model_config(
            model=model_input,
            provider=target_provider,
            base_url=current_base_url,
            api_key=current_api_key,
            api_key_ref=current_api_key_ref,
            api_mode=current_api_mode,
        )
        return _single_response(
            type_="model.switch",
            command=command_text,
            data={
                "model": model_input,
                "provider": target_provider,
                "providerLabel": target_provider,
                "baseUrl": current_base_url,
                "apiMode": current_api_mode,
                "apiKeyConfigured": bool(current_api_key),
                "scope": "aops-channel",
                "persisted": True,
                "configUpdated": config_update["updated"],
                "configPath": config_update["path"],
                "preferenceKey": pref_key,
                "source": endpoint.get("source"),
            },
        )

    try:
        from hermes_cli.config import get_compatible_custom_providers
        custom_providers = get_compatible_custom_providers(cfg)
    except Exception:
        custom_providers = cfg.get("custom_providers") if isinstance(cfg, dict) else None
    user_providers = cfg.get("providers") if isinstance(cfg, dict) else None

    result = switch_model(
        raw_input=model_input,
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
        current_api_key=current_api_key,
        is_global=False,
        explicit_provider=explicit_provider,
        user_providers=user_providers,
        custom_providers=custom_providers,
    )
    if not result.success:
        return _single_response(
            type_="model.switch",
            command=command_text,
            data={"requestedModel": model_input, "requestedProvider": explicit_provider},
            ok=False,
            error={"code": "MODEL_SWITCH_FAILED", "message": result.error_message},
        )

    raw = event.raw_message if isinstance(event.raw_message, dict) else {}
    agent_key = str(raw.get("agentKey") or raw.get("agentId") or "main")
    pref_key = aops_state.preference_key(
        platform=str(event.source.platform.value if event.source and event.source.platform else "aops"),
        channel_id=str(event.source.chat_id if event.source else ""),
        agent_key=agent_key,
    )
    preference = {
        "model": result.new_model,
        "provider": result.target_provider,
        "api_key": result.api_key,
        "api_key_ref": current_api_key_ref,
        "base_url": result.base_url,
        "api_mode": result.api_mode,
    }
    aops_state.set_model_preference(pref_key, preference)
    config_update = _persist_aops_model_config(
        model=result.new_model,
        provider=result.target_provider,
        base_url=result.base_url,
        api_key=result.api_key,
        api_key_ref=current_api_key_ref,
        api_mode=result.api_mode,
    )
    return _single_response(
        type_="model.switch",
        command=command_text,
        data={
            "model": result.new_model,
            "provider": result.target_provider,
            "providerLabel": result.provider_label or result.target_provider,
            "scope": "aops-channel",
            "persisted": True,
            "configUpdated": config_update["updated"],
            "configPath": config_update["path"],
            "preferenceKey": pref_key,
        },
    )


def _security_mode_info(mode: str) -> dict[str, str]:
    labels = {
        "off": "完全访问权限",
        "manual": "默认权限",
        "smart": "自动审查",
    }
    descriptions = {
        "off": "普通危险命令审批关闭；hardline 禁止项仍不可绕过。",
        "manual": "危险命令需要用户人工审批。",
        "smart": "低风险命令可由辅助模型自动通过，高风险或不确定场景继续请求审批。",
    }
    return {"mode": mode, "label": labels.get(mode, mode), "description": descriptions.get(mode, "")}


def _security_command(command_text: str, args: list[str]) -> str:
    from hermes_cli.config import load_config, save_config
    from hermes_constants import get_hermes_home

    aliases = {
        "full": "off",
        "full-access": "off",
        "完全访问权限": "off",
        "default": "manual",
        "默认权限": "manual",
        "auto": "smart",
        "自动审查": "smart",
    }
    cfg = load_config()
    approvals = cfg.setdefault("approvals", {})
    if not isinstance(approvals, dict):
        approvals = {}
        cfg["approvals"] = approvals
    current = str(approvals.get("mode") or "manual").strip().lower()
    destructive_slash_confirm = bool(approvals.get("destructive_slash_confirm", True))
    if not args or args == ["status"]:
        return _single_response(
            type_="security.status",
            command=command_text,
            data={
                "current": _security_mode_info(current),
                "destructiveSlashConfirm": destructive_slash_confirm,
                "configPath": str(get_hermes_home() / "config.yaml"),
            },
        )
    if args[0] != "set" or len(args) < 2:
        return _single_response(
            type_="security.status",
            command=command_text,
            data={
                "current": _security_mode_info(current),
                "destructiveSlashConfirm": destructive_slash_confirm,
                "allowedModes": ["off", "manual", "smart"],
            },
            ok=False,
            error={"code": "SECURITY_USAGE", "message": "Usage: /security set <off|manual|smart>"},
        )
    requested = aliases.get(str(args[1]).strip().lower(), str(args[1]).strip().lower())
    if requested not in {"off", "manual", "smart"}:
        return _single_response(
            type_="security.status",
            command=command_text,
            data={
                "current": _security_mode_info(current),
                "destructiveSlashConfirm": destructive_slash_confirm,
                "allowedModes": ["off", "manual", "smart"],
            },
            ok=False,
            error={"code": "SECURITY_INVALID_MODE", "message": f"Unsupported security mode: {args[1]}"},
        )
    approvals["mode"] = requested
    approvals["destructive_slash_confirm"] = requested != "off"
    save_config(cfg)
    return _single_response(
        type_="security.updated",
        command=command_text,
        data={
            "previous": _security_mode_info(current),
            "current": _security_mode_info(requested),
            "destructiveSlashConfirm": bool(approvals.get("destructive_slash_confirm", True)),
            "persisted": True,
            "configPath": str(get_hermes_home() / "config.yaml"),
        },
    )


def _history_summary_item(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    schedule_flags = _cron_schedule_flags(job)
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "description": _job_description(job),
        "enabled": job.get("enabled"),
        "state": job.get("state"),
        "scheduleText": job.get("schedule_display") or schedule.get("display"),
        "deleteAfterRun": schedule_flags["isOneShot"],
        **schedule_flags,
        "nextRunAtMs": _to_ms(job.get("next_run_at")),
        "lastRunAtMs": _to_ms(job.get("last_run_at")),
        "deliveryText": job.get("deliver"),
        "channelId": _cron_channel_id(job),
        "channel": list(job.get("channel") or ["tec01"]),
    }


def _cron_channel_id(job: dict[str, Any] | None) -> str | None:
    if not isinstance(job, dict):
        return None
    origin = job.get("origin")
    if isinstance(origin, dict):
        value = origin.get("chat_id") or origin.get("channelId") or origin.get("channel_id")
        if value is not None and str(value).strip():
            return str(value).strip()
    value = job.get("channelId") or job.get("channel_id")
    if value is not None and str(value).strip():
        return str(value).strip()
    return None


def _cron_validate_channel_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Cron channelId must be a non-empty string.")
    channel_id = value.strip()
    if not channel_id:
        raise ValueError("Cron channelId must be a non-empty string.")
    return channel_id


def _cron_origin_for_channel_id(channel_id: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    origin = dict(base or {})
    origin["platform"] = "aops"
    origin["chat_id"] = channel_id
    return origin


def _history_entry_channel(entry: dict[str, Any], job: dict[str, Any]) -> list[str]:
    try:
        from cron.jobs import normalize_channel_selection

        return normalize_channel_selection(entry.get("channel"), strict=False)
    except Exception:
        channel = entry.get("channel")
        if isinstance(channel, str):
            items = [channel]
        elif isinstance(channel, list):
            items = channel
        else:
            items = ["tec01"]
        normalized: list[str] = []
        for item in items:
            text = str(item or "").strip().lower()
            if text in {"tec01", "anyi"} and text not in normalized:
                normalized.append(text)
        return normalized or ["tec01"]


def _history_output_snapshot(entry: dict[str, Any]) -> dict[str, str]:
    """Best-effort snapshot recovery for legacy history rows without metadata."""
    text = ""
    output_path = str(entry.get("output_path") or entry.get("outputPath") or "").strip()
    if output_path:
        try:
            path = Path(output_path).expanduser()
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")[:12000]
        except Exception:
            text = ""
    if not text:
        text = str(entry.get("response_preview") or entry.get("summary") or "")
    snapshot: dict[str, str] = {}
    name_match = re.search(r"(?m)^#\s*Cron Job:\s*(.+?)\s*$", text)
    if name_match:
        snapshot["job_name"] = name_match.group(1).strip()
    schedule_match = re.search(r"(?m)^\*\*Schedule:\*\*\s*(.+?)\s*$", text)
    if schedule_match:
        snapshot["schedule_display"] = schedule_match.group(1).strip()
    prompt_match = re.search(r"(?s)##\s*Prompt\s*\n(.+?)(?:\n##\s+|\Z)", text)
    if prompt_match:
        prompt = prompt_match.group(1).strip()
        if prompt:
            snapshot["prompt"] = prompt[:500]
    return snapshot


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


def _read_output_history_entries(cron_jobs: Any, job_id: str) -> list[dict[str, Any]]:
    output_dir = Path(cron_jobs.OUTPUT_DIR) / str(job_id)
    if not output_dir.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("*.md"), key=lambda item: item.stat().st_mtime):
        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        dt = datetime.fromtimestamp(stat.st_mtime).astimezone()
        entries.append({
            "job_id": job_id,
            "job_name": None,
            "job_description": None,
            "status": "ok",
            "error": None,
            "response_preview": _compact_text(text, limit=300),
            "delivery_error": None,
            "finished_at": dt.isoformat(),
            "started_at": None,
            "timestamp": dt.isoformat(),
            "duration_ms": None,
            "output_path": str(path),
        })
    return entries


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

    entries: list[dict[str, Any]] = []
    history_path = Path(cron_jobs.HISTORY_FILE)
    try:
        if history_path.exists():
            found_anchor = requested_anchor is None
            collected_after_anchor = 0
            for raw in _iter_cron_history_entries_newest_first(history_path):
                if str(raw.get("job_id") or "") == str(job_id):
                    entries.append(raw)
                    if requested_anchor is None and direction == "latest" and len(entries) >= 20:
                        break
                    raw_ts = _to_ms(
                        _first_history_value(
                            raw,
                            "finished_at",
                            "finishedAt",
                            "timestamp",
                            "ts",
                            "started_at",
                            "startedAt",
                        )
                    )
                    if requested_anchor is not None:
                        if raw_ts == requested_anchor:
                            found_anchor = True
                        elif found_anchor and direction in {"latest", "before"}:
                            collected_after_anchor += 1
                            if collected_after_anchor >= 20:
                                break
    except Exception as exc:
        return _history_error(
            command_text,
            {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
            "CRON_HISTORY_READ_FAILED",
            str(exc),
        )
    if not entries:
        entries = _read_output_history_entries(cron_jobs, str(job_id))

    job = cron_jobs.get_job(job_id)
    job_deleted = job is None
    if job is None and entries:
        latest = entries[-1]
        job = {
            "id": job_id,
            "name": latest.get("job_name"),
            "description": latest.get("job_description") or latest.get("job_name") or latest.get("response_preview") or "",
            "prompt": latest.get("job_description") or latest.get("response_preview") or "",
            "enabled": False,
            "state": "deleted",
            "schedule": {"kind": "once", "display": "deleted"},
            "schedule_display": latest.get("schedule_display") or "deleted",
            "next_run_at": None,
            "last_run_at": latest.get("finished_at") or latest.get("timestamp"),
            "repeat": {"times": 1, "completed": 1},
        }
    if job is None:
        return _history_error(
            command_text,
            {"storePath": str(cron_jobs.JOBS_FILE), "logPath": str(cron_jobs.HISTORY_FILE), "jobId": job_id, "direction": direction, "requestedAnchorTs": requested_anchor, "anchorTs": None, "anchorEntry": None},
            "CRON_JOB_NOT_FOUND",
            f"Cron job `{job_id}` not found.",
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
        legacy_snapshot = _history_output_snapshot(entry)
        entry_job_name = entry.get("job_name") or legacy_snapshot.get("job_name")
        entry_prompt = entry.get("prompt") or legacy_snapshot.get("prompt")
        entry_schedule_text = entry.get("schedule_display") or legacy_snapshot.get("schedule_display")
        if not entry_schedule_text and isinstance(entry.get("schedule"), dict):
            entry_schedule_text = entry.get("schedule", {}).get("display")
        entry_channel_id = _cron_channel_id(entry)
        ts = _to_ms(
            _first_history_value(
                entry,
                "finished_at",
                "finishedAt",
                "timestamp",
                "ts",
                "started_at",
                "startedAt",
            )
        )
        delivered = None if entry.get("delivery_error") is None else False
        status = str(entry.get("status") or "ok").lower()
        duration_ms = _history_duration_ms(entry)
        normalized.append(
            {
                "ts": ts,
                "jobId": entry.get("job_id"),
                "description": entry.get("job_description") or _compact_text(entry_prompt) or _compact_text(entry_job_name) or _compact_text(entry.get("response_preview")),
                "action": "finished",
                "status": status,
                "error": entry.get("error"),
                "summary": entry.get("response_preview"),
                "delivered": delivered,
                "deliveryStatus": _status_to_delivery(status, entry.get("delivery_error"), delivered),
                "deliveryError": entry.get("delivery_error"),
                "sessionId": entry.get("session_id") or entry.get("sessionId"),
                "sessionKey": entry.get("session_key") or entry.get("sessionKey"),
                "runAtMs": _to_ms(_first_history_value(entry, "started_at", "startedAt", "run_at", "runAt", "timestamp")),
                "durationMs": duration_ms,
                "nextRunAtMs": _history_next_run_ms(entry, job),
                "model": entry.get("model"),
                "provider": entry.get("provider"),
                "usage": _history_usage(entry, duration_ms),
                "jobName": entry_job_name,
                "prompt": entry_prompt,
                "scheduleText": entry_schedule_text,
                "deliveryText": entry.get("deliver"),
                "channelId": entry_channel_id,
                "channel": _history_entry_channel(entry, job),
                "outputPath": entry.get("output_path"),
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
        "jobDeleted": job_deleted,
    }
    summary = {
        "task": _history_summary_item(job),
        "jobDeleted": job_deleted,
        "message": "该定时任务当前不存在；根据历史记录判断，它可能是一次性任务，执行后已自动删除。" if job_deleted else None,
    }
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


def _cron_single_error(command: str, type_: str, code: str, message: str, details: dict[str, Any] | None = None) -> str:
    return _single_response(
        type_=type_,
        command=command,
        data={},
        ok=False,
        error={"code": code, "message": message, "details": details or {}},
    )


def _cron_decode_payload(command_text: str, raw_json: str, type_: str) -> dict[str, Any] | str:
    if not raw_json.strip():
        return _cron_single_error(command_text, type_, "CRON_MISSING_PAYLOAD", "Expected a JSON object payload.")
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return _cron_single_error(command_text, type_, "CRON_INVALID_JSON", str(exc), {"payload": raw_json})
    if not isinstance(payload, dict):
        return _cron_single_error(command_text, type_, "CRON_INVALID_JSON", "Cron payload must be a JSON object.", {"payload": payload})
    return payload


def _cron_task_response(type_: str, command_text: str, job: dict[str, Any], *, message: str | None = None) -> str:
    task = _cron_item(job)
    data: dict[str, Any] = {"task": task, "channel": list(task.get("channel") or ["tec01"])}
    if message:
        data["message"] = message
    return _single_response(type_=type_, command=command_text, data=data)


def _cron_task_result(
    type_: str,
    command_text: str,
    job: dict[str, Any],
    *,
    message: str | None = None,
    trigger_now: bool = False,
) -> LocalCommandResult:
    text = _cron_task_response(type_, command_text, job, message=message)
    metadata: dict[str, Any] = {}
    if trigger_now and job.get("id"):
        metadata = {"effects": {"triggerCronJobId": str(job["id"])}}
    return LocalCommandResult(text=text, metadata=metadata)


def _cron_origin_from_event(event: MessageEvent | None) -> dict[str, Any] | None:
    source = getattr(event, "source", None)
    if not source or not getattr(source, "chat_id", None):
        return None
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform) or "aops"
    origin: dict[str, Any] = {
        "platform": str(platform_value),
        "chat_id": str(source.chat_id),
    }
    for attr in ("chat_name", "chat_type", "user_id", "user_name", "thread_id", "parent_chat_id", "profile"):
        value = getattr(source, attr, None)
        if value is not None and str(value).strip():
            origin[attr] = str(value)
    return origin


def _cron_create(command_text: str, raw_args: str, event: MessageEvent | None = None) -> str | LocalCommandResult:
    from cron import jobs as cron_jobs

    raw_payload = raw_args.strip()[len("create"):].strip()
    payload = _cron_decode_payload(command_text, raw_payload, "cron.created")
    if isinstance(payload, str):
        return payload
    prompt = str(payload.get("prompt") or "").strip()
    schedule = str(payload.get("schedule") or "").strip()
    if not prompt:
        return _cron_single_error(command_text, "cron.created", "CRON_MISSING_PROMPT", "Cron create requires prompt.")
    if not schedule:
        return _cron_single_error(command_text, "cron.created", "CRON_MISSING_SCHEDULE", "Cron create requires schedule.")
    origin = _cron_origin_from_event(event)
    if "channelId" in payload:
        try:
            origin = _cron_origin_for_channel_id(_cron_validate_channel_id(payload.get("channelId")), origin)
        except ValueError as exc:
            return _cron_single_error(command_text, "cron.created", "CRON_INVALID_CHANNEL_ID", str(exc), {"channelId": payload.get("channelId")})
    try:
        job = cron_jobs.create_job(
            prompt=prompt,
            schedule=schedule,
            name=payload.get("name"),
            deliver=payload.get("deliver") or ("origin" if origin else "aops"),
            origin=origin,
            channel=payload.get("channel"),
        )
        trigger_now = payload.get("triggerNow") is True and payload.get("enabled") is not False
        if payload.get("enabled") is False:
            job = cron_jobs.pause_job(job["id"]) or job
    except getattr(cron_jobs, "InvalidCronChannel") as exc:
        return _cron_single_error(command_text, "cron.created", "CRON_INVALID_CHANNEL", str(exc), {"channel": payload.get("channel")})
    except ValueError as exc:
        return _cron_single_error(command_text, "cron.created", "CRON_INVALID_SCHEDULE", str(exc))
    except Exception as exc:
        return _cron_single_error(command_text, "cron.created", "CRON_CREATE_FAILED", str(exc))
    message = f"Created cron job `{job.get('name') or job.get('id')}`."
    if trigger_now:
        message += " Immediate trigger accepted."
        return _cron_task_result("cron.created", command_text, job, message=message, trigger_now=True)
    return _cron_task_response("cron.created", command_text, job, message=message)


def _cron_resolve_ref(cron_jobs: Any, command_text: str, type_: str, job_ref: str) -> dict[str, Any] | str | None:
    try:
        return cron_jobs.resolve_job_ref(job_ref)
    except getattr(cron_jobs, "AmbiguousJobReference") as exc:
        matches = [_history_summary_item(match) for match in getattr(exc, "matches", [])]
        return _single_response(
            type_=type_,
            command=command_text,
            data={"matches": matches},
            ok=False,
            error={"code": "CRON_JOB_AMBIGUOUS", "message": str(exc)},
        )


def _cron_update(command_text: str, raw_args: str, event: MessageEvent | None = None) -> str | LocalCommandResult:
    from cron import jobs as cron_jobs

    rest = raw_args.strip()[len("update"):].strip()
    parts = rest.split(None, 1)
    if len(parts) < 2:
        return _cron_single_error(command_text, "cron.updated", "CRON_UPDATE_USAGE", "Usage: /cron update <id|name> {json}")
    job_ref, raw_payload = parts
    resolved = _cron_resolve_ref(cron_jobs, command_text, "cron.updated", job_ref)
    if isinstance(resolved, str):
        return resolved
    if not resolved:
        return _cron_single_error(command_text, "cron.updated", "CRON_JOB_NOT_FOUND", f"Cron job `{job_ref}` not found.")
    payload = _cron_decode_payload(command_text, raw_payload, "cron.updated")
    if isinstance(payload, str):
        return payload
    allowed = {"name", "prompt", "schedule", "channel", "deliver"}
    updates = {key: payload[key] for key in allowed if key in payload}
    origin = _cron_origin_from_event(event)
    if "channelId" in payload:
        try:
            base_origin = resolved.get("origin") if isinstance(resolved.get("origin"), dict) else origin
            updates["origin"] = _cron_origin_for_channel_id(_cron_validate_channel_id(payload.get("channelId")), base_origin)
            if str(updates.get("deliver") or "").strip().lower() != "local":
                updates["deliver"] = "origin"
        except ValueError as exc:
            return _cron_single_error(command_text, "cron.updated", "CRON_INVALID_CHANNEL_ID", str(exc), {"channelId": payload.get("channelId")})
    if "deliver" not in updates and str(resolved.get("deliver") or "local").strip().lower() == "local":
        updates["deliver"] = "origin" if origin else "aops"
    if "origin" not in updates and origin and not resolved.get("origin") and str(updates.get("deliver") or resolved.get("deliver") or "").strip().lower() == "origin":
        updates["origin"] = origin
    try:
        job = resolved
        if updates:
            updated = cron_jobs.update_job(str(resolved["id"]), updates)
            job = updated or job
        if payload.get("enabled") is False:
            job = cron_jobs.pause_job(str(job["id"])) or job
        elif payload.get("enabled") is True:
            job = cron_jobs.resume_job(str(job["id"])) or job
        trigger_now = payload.get("triggerNow") is True and job.get("enabled", True)
    except getattr(cron_jobs, "InvalidCronChannel") as exc:
        return _cron_single_error(command_text, "cron.updated", "CRON_INVALID_CHANNEL", str(exc), {"channel": payload.get("channel")})
    except ValueError as exc:
        return _cron_single_error(command_text, "cron.updated", "CRON_UPDATE_FAILED", str(exc))
    except Exception as exc:
        return _cron_single_error(command_text, "cron.updated", "CRON_UPDATE_FAILED", str(exc))
    message = f"Updated cron job `{job.get('name') or job.get('id')}`."
    if trigger_now:
        message += " Immediate trigger accepted."
        return _cron_task_result("cron.updated", command_text, job, message=message, trigger_now=True)
    return _cron_task_response("cron.updated", command_text, job, message=message)


def _cron_ref_action(command_text: str, args: list[str], *, action: str, type_: str) -> str | LocalCommandResult:
    from cron import jobs as cron_jobs

    if len(args) < 2:
        return _cron_single_error(command_text, type_, f"CRON_{action.upper()}_MISSING_REF", f"Usage: /cron {action} <id|name>")
    job_ref = args[1]
    try:
        if action == "pause":
            job = cron_jobs.pause_job(job_ref)
        elif action == "resume":
            job = cron_jobs.resume_job(job_ref)
        elif action == "trigger":
            job = cron_jobs.resolve_job_ref(job_ref)
        else:
            job = None
    except getattr(cron_jobs, "AmbiguousJobReference") as exc:
        matches = [_history_summary_item(match) for match in getattr(exc, "matches", [])]
        return _single_response(
            type_=type_,
            command=command_text,
            data={"matches": matches},
            ok=False,
            error={"code": "CRON_JOB_AMBIGUOUS", "message": str(exc)},
        )
    except Exception as exc:
        return _cron_single_error(command_text, type_, f"CRON_{action.upper()}_FAILED", str(exc))
    if not job:
        return _cron_single_error(command_text, type_, "CRON_JOB_NOT_FOUND", f"Cron job `{job_ref}` not found.")
    past = {"pause": "paused", "resume": "resumed", "trigger": "triggered"}.get(action, action)
    if action == "trigger":
        return _cron_task_result(
            type_,
            command_text,
            job,
            message=f"Cron job `{job.get('name') or job.get('id')}` immediate trigger accepted.",
            trigger_now=True,
        )
    return _cron_task_response(type_, command_text, job, message=f"Cron job `{job.get('name') or job.get('id')}` {past}.")


def _cron_remove(command_text: str, args: list[str]) -> str:
    from cron import jobs as cron_jobs

    context = {
        "storePath": str(cron_jobs.JOBS_FILE),
        "jobRef": args[1] if len(args) > 1 else None,
    }
    if len(args) < 2:
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": context},
            ok=False,
            error={
                "code": "CRON_REMOVE_MISSING_REF",
                "message": "Usage: /cron remove <id|name>",
            },
        )

    job_ref = args[1]
    try:
        job = cron_jobs.resolve_job_ref(job_ref)
    except getattr(cron_jobs, "AmbiguousJobReference") as exc:
        matches = [
            {
                "id": match.get("id"),
                "name": match.get("name"),
                "description": _job_description(match),
                "scheduleText": match.get("schedule_display") or (match.get("schedule") or {}).get("display"),
                "channel": list(match.get("channel") or ["tec01"]),
            }
            for match in getattr(exc, "matches", [])
        ]
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": context, "matches": matches},
            ok=False,
            error={
                "code": "CRON_REMOVE_AMBIGUOUS_REF",
                "message": str(exc),
            },
        )
    except Exception as exc:
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": context},
            ok=False,
            error={
                "code": "CRON_REMOVE_FAILED",
                "message": str(exc),
            },
        )

    if job is None:
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": context},
            ok=False,
            error={
                "code": "CRON_JOB_NOT_FOUND",
                "message": f"Cron job `{job_ref}` not found.",
            },
        )

    summary = _history_summary_item(job)
    try:
        removed = cron_jobs.remove_job(job["id"])
    except Exception as exc:
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": {**context, "jobId": job.get("id")}, "task": summary},
            ok=False,
            error={
                "code": "CRON_REMOVE_FAILED",
                "message": str(exc),
            },
        )

    if not removed:
        return _single_response(
            type_="cron.removed",
            command=command_text,
            data={"context": {**context, "jobId": job.get("id")}, "task": summary},
            ok=False,
            error={
                "code": "CRON_JOB_NOT_FOUND",
                "message": f"Cron job `{job_ref}` not found.",
            },
        )

    return _single_response(
        type_="cron.removed",
        command=command_text,
        data={
            "context": {**context, "jobId": job.get("id")},
            "task": summary,
            "channel": list((summary or {}).get("channel") or ["tec01"]),
            "removed": True,
            "message": f"Removed cron job `{job.get('name') or job.get('id')}`.",
        },
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
        return _skills_command(full_command, args)

    if canonical == "toolsets":
        return _toolsets_command(full_command, event, args)

    if canonical in {"soul", "user"}:
        return _instruction_command(canonical, full_command, raw_args, args)

    if canonical == "busy":
        return _busy_command(full_command, args)

    if canonical == "cron":
        if not args or args[:1] == ["list"]:
            try:
                channel_filter = _cron_parse_list_channel(args)
                items, context, summary = _cron_items(channel_filter)
                return _list_response(
                    type_="cron.list",
                    command=full_command,
                    item_type="cron.task",
                    items=items,
                    context=context,
                    summary=summary,
                    extra={"channel": channel_filter} if channel_filter else None,
                )
            except ValueError:
                return _cron_channel_error(full_command, args[1] if len(args) > 1 else None)
            except Exception as exc:
                return _list_response(
                    type_="cron.list",
                    command=full_command,
                    item_type="cron.task",
                    items=[],
                    error={"code": "CRON_STORE_READ_FAILED", "message": str(exc)},
                )
        if args[:1] == ["create"]:
            return _cron_create(full_command, raw_args, event)
        if args[:1] == ["update"]:
            return _cron_update(full_command, raw_args, event)
        if args[:1] in (["pause"], ["disable"]):
            return _cron_ref_action(full_command, args, action="pause", type_="cron.paused")
        if args[:1] in (["resume"], ["enable"]):
            return _cron_ref_action(full_command, args, action="resume", type_="cron.resumed")
        if args[:1] == ["trigger"]:
            return _cron_ref_action(full_command, args, action="trigger", type_="cron.triggered")
        if args[:1] == ["history"]:
            return _read_cron_history(full_command, args)
        if args[:1] == ["remove"]:
            return _cron_remove(full_command, args)

    if canonical == "model":
        try:
            normalized_args = [arg.lower() for arg in args]
            if len(normalized_args) == 1 and normalized_args[0] in _MODEL_STATUS_TOKENS:
                return _aops_model_status(full_command, event)
            if not args or (len(normalized_args) == 1 and normalized_args[0] in _MODEL_LIST_TOKENS):
                return _aops_model_list(full_command, event)
            return _aops_model_use(full_command, event, args)
        except Exception as exc:
            normalized_args = [arg.lower() for arg in args]
            is_status = len(normalized_args) == 1 and normalized_args[0] in _MODEL_STATUS_TOKENS
            is_list = (not args) or (len(normalized_args) == 1 and normalized_args[0] in _MODEL_LIST_TOKENS)
            return _single_response(
                type_="model.status" if is_status else ("model.list" if is_list else "model.switch"),
                command=full_command,
                data={},
                ok=False,
                error={
                    "code": "MODEL_STATUS_FAILED" if is_status else ("MODEL_LIST_FAILED" if is_list else "MODEL_SWITCH_FAILED"),
                    "message": str(exc),
                },
            )

    if canonical == "reasoning":
        return _reasoning_command(full_command, args)

    if canonical in {"security", "securty"}:
        return _security_command(full_command, args)

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
        if cmd.name in _REMOVED_AOPS_COMMANDS or cmd.name in {"skills", "cron", "security", "securty"}:
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


def _extension_child_node(config: Any, parent: str, child: dict[str, Any]) -> HelpNode:
    child_name = str(child.get("command") or child.get("name") or "").strip().lower().replace("_", "-").lstrip("/")
    child_full = str(child.get("fullCommand") or child.get("full_command") or "").strip()
    if not child_full:
        child_full = f"/{parent} {child_name}".strip()
    return _node(
        type_=str(child.get("type") or "custom"),
        command=child_name,
        full_command=child_full,
        description=str(child.get("description") or f"执行 {child_full}。"),
        dangerous=_dangerous(config, child_full),
        usage=str(child.get("usage") or child_full),
        executable=bool(child.get("executable", True)),
        completions=list(child.get("completions") or []),
        children=[
            _extension_child_node(config, parent, nested)
            for nested in child.get("children", [])
            if isinstance(nested, dict)
        ],
    )


def _extension_nodes(config: Any) -> list[HelpNode]:
    nodes: list[HelpNode] = []
    for name, extension in sorted(_AOPS_COMMAND_EXTENSIONS.items()):
        full_command = f"/{name}"
        if is_blocked(config, name):
            continue
        nodes.append(
            _node(
                type_=extension.type,
                command=full_command,
                full_command=full_command,
                description=extension.description,
                dangerous=_dangerous(config, full_command),
                usage=extension.usage or full_command,
                executable=extension.executable,
                completions=list(extension.completions or []),
                children=[
                    _extension_child_node(config, name, child)
                    for child in (extension.children or [])
                    if isinstance(child, dict)
                ],
            )
        )
    return nodes


def _skills_node(config: Any) -> HelpNode:
    full_command = "/skills"
    child_full = "/skills list"
    enable_full = "/skills enable"
    disable_full = "/skills disable"
    set_full = "/skills set"
    uninstall_full = "/skills uninstall"
    remove_full = "/skills remove"
    return _node(
        type_="custom",
        command=full_command,
        full_command=full_command,
        description="列出、切换或卸载已安装技能。",
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
            ),
            _node(
                type_="custom",
                command="enable",
                full_command=enable_full,
                description="启用技能。",
                dangerous=_dangerous(config, enable_full),
                usage="/skills enable <name>",
                executable=True,
                completions=[_param("name", "技能名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="disable",
                full_command=disable_full,
                description="禁用技能。",
                dangerous=_dangerous(config, disable_full),
                usage="/skills disable <name>",
                executable=True,
                completions=[_param("name", "技能名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="set",
                full_command=set_full,
                description="按 true/false 设置技能。",
                dangerous=_dangerous(config, set_full),
                usage="/skills set <name> <true|false>",
                executable=True,
                completions=[
                    _param("name", "技能名称。", required=True),
                    _param(
                        "enabled",
                        "是否启用。",
                        required=True,
                        choices=[
                            _choice("true", "启用。"),
                            _choice("false", "禁用。"),
                        ],
                    ),
                ],
            ),
            _node(
                type_="custom",
                command="uninstall",
                full_command=uninstall_full,
                description="卸载技能；hub 技能优先走 hub 卸载，本地技能安全删除。",
                dangerous=True,
                usage="/skills uninstall <name>",
                executable=True,
                completions=[_param("name", "技能名称、ID 或命令。", required=True)],
            ),
            _node(
                type_="custom",
                command="remove",
                full_command=remove_full,
                description="卸载技能，等价于 /skills uninstall。",
                dangerous=True,
                usage="/skills remove <name>",
                executable=True,
                completions=[_param("name", "技能名称、ID 或命令。", required=True)],
            ),
        ],
    )


def _toolsets_node(config: Any) -> HelpNode:
    full_command = "/toolsets"
    list_full = "/toolsets list"
    enable_full = "/toolsets enable"
    disable_full = "/toolsets disable"
    set_full = "/toolsets set"
    return _node(
        type_="custom",
        command=full_command,
        full_command=full_command,
        description="查看或切换当前 profile 的 AOPS 工具集。",
        dangerous=_dangerous(config, full_command),
        usage="/toolsets",
        executable=True,
        children=[
            _node(
                type_="custom",
                command="list",
                full_command=list_full,
                description="获取当前 agent/profile 的工具集列表。",
                dangerous=_dangerous(config, list_full),
                usage=list_full,
                executable=True,
            ),
            _node(
                type_="custom",
                command="enable",
                full_command=enable_full,
                description="启用工具集。",
                dangerous=_dangerous(config, enable_full),
                usage="/toolsets enable <name>",
                executable=True,
                completions=[_param("name", "工具集名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="disable",
                full_command=disable_full,
                description="关闭工具集。",
                dangerous=_dangerous(config, disable_full),
                usage="/toolsets disable <name>",
                executable=True,
                completions=[_param("name", "工具集名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="set",
                full_command=set_full,
                description="按 true/false 设置工具集。",
                dangerous=_dangerous(config, set_full),
                usage="/toolsets set <name> <true|false>",
                executable=True,
                completions=[
                    _param("name", "工具集名称。", required=True),
                    _param(
                        "enabled",
                        "是否启用。",
                        required=True,
                        choices=[
                            _choice("true", "启用。"),
                            _choice("false", "关闭。"),
                        ],
                    ),
                ],
            ),
        ],
    )


def _cron_node(config: Any) -> HelpNode:
    full_command = "/cron"
    list_full = "/cron list"
    create_full = "/cron create"
    update_full = "/cron update"
    pause_full = "/cron pause"
    resume_full = "/cron resume"
    enable_full = "/cron enable"
    disable_full = "/cron disable"
    trigger_full = "/cron trigger"
    remove_full = "/cron remove"
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
                usage="/cron list [channel]",
                executable=True,
                completions=[
                    _param(
                        "channel",
                        "渠道筛选，可选 tec01 或 anyi。缺省返回全部任务。",
                        required=False,
                        choices=[
                            _choice("tec01", "Tec01 UI 渠道。"),
                            _choice("anyi", "安逸公众号渠道。"),
                        ],
                    ),
                ],
            ),
            _node(
                type_="custom",
                command="create",
                full_command=create_full,
                description="创建定时任务。",
                dangerous=_dangerous(config, create_full),
                usage="/cron create {json}",
                executable=False,
                completions=[_param("payload", "JSON 对象，包含 prompt、schedule、channel、channelId 等。", required=True)],
            ),
            _node(
                type_="custom",
                command="update",
                full_command=update_full,
                description="更新定时任务。",
                dangerous=_dangerous(config, update_full),
                usage="/cron update <id|name> {json}",
                executable=False,
                completions=[
                    _param("idOrName", "定时任务 ID 或唯一名称。", required=True),
                    _param("payload", "JSON 对象，包含 name、prompt、schedule、channel、channelId 等。", required=True),
                ],
            ),
            _node(
                type_="custom",
                command="pause",
                full_command=pause_full,
                description="暂停定时任务。",
                dangerous=_dangerous(config, pause_full),
                usage="/cron pause <id|name>",
                executable=False,
                completions=[_param("idOrName", "定时任务 ID 或唯一名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="resume",
                full_command=resume_full,
                description="恢复定时任务。",
                dangerous=_dangerous(config, resume_full),
                usage="/cron resume <id|name>",
                executable=False,
                completions=[_param("idOrName", "定时任务 ID 或唯一名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="enable",
                full_command=enable_full,
                description="启用定时任务，同 resume。",
                dangerous=_dangerous(config, enable_full),
                usage="/cron enable <id|name>",
                executable=False,
                completions=[_param("idOrName", "定时任务 ID 或唯一名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="disable",
                full_command=disable_full,
                description="禁用定时任务，同 pause。",
                dangerous=_dangerous(config, disable_full),
                usage="/cron disable <id|name>",
                executable=False,
                completions=[_param("idOrName", "定时任务 ID 或唯一名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="trigger",
                full_command=trigger_full,
                description="立即触发定时任务。",
                dangerous=_dangerous(config, trigger_full),
                usage="/cron trigger <id|name>",
                executable=False,
                completions=[_param("idOrName", "定时任务 ID 或唯一名称。", required=True)],
            ),
            _node(
                type_="custom",
                command="remove",
                full_command=remove_full,
                description="删除定时任务。",
                dangerous=_dangerous(config, remove_full),
                usage="/cron remove <id|name>",
                executable=False,
                completions=[
                    _param("idOrName", "定时任务 ID 或唯一名称。", required=True),
                ],
            ),
            history_node,
        ],
    )


def _security_node(config: Any) -> HelpNode:
    full_command = "/security"
    set_full = "/security set"
    return _node(
        type_="configuration",
        command=full_command,
        full_command=full_command,
        description=_DESCRIPTION_ZH["security"],
        dangerous=_dangerous(config, full_command),
        usage="/security",
        executable=True,
        completions=list(_USAGE_COMPLETIONS.get("security", [])),
        children=[
            _node(
                type_="configuration",
                command="set",
                full_command=set_full,
                description="切换安全审批策略。",
                dangerous=_dangerous(config, set_full),
                usage="/security set <off|manual|smart>",
                executable=False,
                completions=list(_USAGE_COMPLETIONS.get("security", [])),
            )
        ],
    )


def _instruction_node(config: Any, kind: str) -> HelpNode:
    full_command = f"/{kind}"
    get_full = f"/{kind} get"
    set_full = f"/{kind} set"
    append_full = f"/{kind} append"
    target = "SOUL.md" if kind == "soul" else "memories/USER.md"
    return _node(
        type_="configuration",
        command=full_command,
        full_command=full_command,
        description=f"查看或编辑当前 profile 的 {target}。",
        dangerous=_dangerous(config, full_command),
        usage=f"/{kind} [get|set|append]",
        executable=True,
        completions=[
            _param(
                "subcommand",
                "指令文件操作。",
                required=False,
                choices=[
                    _choice("get", f"读取 {target}。"),
                    _choice("set", f"覆盖写入 {target}。"),
                    _choice("append", f"追加到 {target}。"),
                ],
            ),
        ],
        children=[
            _node(
                type_="configuration",
                command="get",
                full_command=get_full,
                description=f"读取当前 profile 的 {target}。",
                dangerous=_dangerous(config, get_full),
                usage=get_full,
                executable=True,
            ),
            _node(
                type_="configuration",
                command="set",
                full_command=set_full,
                description=f"覆盖写入当前 profile 的 {target}，后续新 turn 立即生效。",
                dangerous=_dangerous(config, set_full),
                usage=f"/{kind} set {{\"content\":\"...\"}}",
                executable=False,
                completions=[_param("payload", "JSON 对象，包含 content。", required=True)],
            ),
            _node(
                type_="configuration",
                command="append",
                full_command=append_full,
                description=f"追加内容到当前 profile 的 {target}，后续新 turn 立即生效。",
                dangerous=_dangerous(config, append_full),
                usage=f"/{kind} append {{\"content\":\"...\"}}",
                executable=False,
                completions=[_param("payload", "JSON 对象，包含 content。", required=True)],
            ),
        ],
    )


def _busy_node(config: Any) -> HelpNode:
    full_command = "/busy"
    return _node(
        type_="configuration",
        command=full_command,
        full_command=full_command,
        description="查看或切换 busy 输入策略，命令返回后立即生效。",
        dangerous=_dangerous(config, full_command),
        usage="/busy [queue|steer|interrupt|status]",
        executable=True,
        completions=[
            _param(
                "mode",
                "busy 输入策略。",
                required=False,
                choices=[
                    _choice("queue", "忙碌时排队到下一轮。"),
                    _choice("steer", "忙碌时注入当前运行。"),
                    _choice("interrupt", "忙碌时中断当前运行。"),
                    _choice("status", "查看当前策略。"),
                ],
            )
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


def help_tree_response(config: Any, command_text: str = "/help") -> str:
    nodes = _build_official_nodes(config)
    nodes.append(_skills_node(config))
    nodes.append(_toolsets_node(config))
    nodes.append(_cron_node(config))
    nodes.append(_instruction_node(config, "soul"))
    nodes.append(_instruction_node(config, "user"))
    nodes.append(_busy_node(config))
    if not is_blocked(config, "security"):
        nodes.append(_security_node(config))
    nodes = [node for node in nodes if node.full_command != "/curator"]
    nodes.append(_curator_node(config))
    nodes.extend(_extension_nodes(config))
    nodes.extend(_skill_command_nodes(config))
    deduped: list[HelpNode] = []
    seen: set[str] = set()
    for node in nodes:
        key = node.full_command.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(node)
    nodes = deduped
    top_level_count = len(nodes)
    total_count, executable_count, dangerous_count = _count_nodes(nodes)
    payload = {
        "schemaVersion": HELP_TREE_SCHEMA,
        "type": "command.tree",
        "ok": True,
        "command": command_text,
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
        "`/skills enable <name>` -- Enable an installed skill",
        "`/skills disable <name>` -- Disable an installed skill",
        "`/skills set <name> <true|false>` -- Enable or disable an installed skill",
        "`/toolsets` -- List or update AOPS toolsets",
        "`/toolsets list` -- List AOPS toolsets",
        "`/toolsets enable <name>` -- Enable an AOPS toolset",
        "`/toolsets disable <name>` -- Disable an AOPS toolset",
        "`/toolsets set <name> <true|false>` -- Enable or disable an AOPS toolset",
        "`/cron` -- Show scheduled tasks",
        "`/cron list [channel]` -- Show scheduled tasks, optionally filtered by tec01 or anyi",
        "`/cron create {json}` -- Create a scheduled task",
        "`/cron update <id|name> {json}` -- Update a scheduled task",
        "`/cron pause <id|name>` -- Pause a scheduled task",
        "`/cron resume <id|name>` -- Resume a scheduled task",
        "`/cron trigger <id|name>` -- Trigger a scheduled task immediately",
        "`/cron remove <id|name>` -- Remove a scheduled task",
        "`/cron history <id> [tsMs]` -- Show cron run history",
        "`/cron history before <id> [tsMs]` -- Show cron history before an anchor",
        "`/cron history after <id> <tsMs>` -- Show cron history after an anchor",
        "`/soul` -- Show current SOUL.md instructions",
        "`/soul set {json}` -- Replace SOUL.md instructions",
        "`/soul append {json}` -- Append to SOUL.md instructions",
        "`/user` -- Show current memories/USER.md instructions",
        "`/user set {json}` -- Replace memories/USER.md instructions",
        "`/user append {json}` -- Append to memories/USER.md instructions",
        "`/busy [queue|steer|interrupt|status]` -- Show or switch busy input mode",
        "`/security` -- Show current approval policy",
        "`/security set <off|manual|smart>` -- Switch approval policy",
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
