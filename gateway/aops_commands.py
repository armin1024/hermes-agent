"""AOPS local command responses and CLI bridge helpers."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent


LOCAL_LIST_SCHEMA = "local-command-list.v1"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_OUTPUT_LIMIT = 40000

_EXTRA_CLI_COMMANDS = {
    "acp": "Run the ACP adapter",
    "auth": "Manage authentication and credential pools",
    "backup": "Create a Hermes state backup",
    "chat": "Start or run a chat session",
    "claw": "OpenClaw migration helpers",
    "completion": "Generate shell completion",
    "config": "Show or edit configuration",
    "cron": "Manage scheduled tasks",
    "dashboard": "Run the local dashboard",
    "doctor": "Run diagnostics",
    "dump": "Dump runtime diagnostic information",
    "fallback": "Manage fallback provider chain",
    "gateway": "Manage messaging gateway",
    "hooks": "Manage shell hooks",
    "import": "Import a Hermes backup",
    "login": "Log in to a provider",
    "logout": "Log out of stored authentication",
    "logs": "View Hermes logs",
    "mcp": "Manage MCP servers",
    "memory": "Manage memory configuration",
    "plugins": "Manage plugins",
    "profile": "Manage Hermes profiles",
    "sessions": "Manage saved sessions",
    "setup": "Run setup wizard",
    "skills": "Search, install, inspect, or manage skills",
    "slack": "Slack app manifest helpers",
    "status": "Show Hermes status",
    "tools": "Manage tools",
    "update": "Update Hermes Agent",
    "uninstall": "Uninstall Hermes Agent",
    "version": "Show Hermes version",
    "webhook": "Manage webhook subscriptions",
    "whatsapp": "Manage WhatsApp setup",
}


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


def blocked_commands(config: Any) -> set[str]:
    raw: Any = _extra(config).get("blocked_commands", [])
    if isinstance(raw, str):
        items: Iterable[Any] = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = []

    env_raw = os.getenv("AOPS_BLOCKED_COMMANDS", "")
    env_items = env_raw.replace("\n", ",").split(",") if env_raw else []

    blocked: set[str] = set()
    for item in [*items, *env_items]:
        text = str(item or "").strip().lower().lstrip("/")
        if text:
            blocked.add(text.replace("_", "-"))
    return blocked


def _effective_command(command: str | None, raw_args: str) -> str | None:
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
        _effective_command(command, raw_args),
        _effective_command(canonical, raw_args),
    }
    return any(candidate in blocked for candidate in candidates if candidate)


def block_message(command: str | None) -> str:
    label = f"/{command}" if command else "this command"
    return f"Command `{label}` is blocked by AOPS config."


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


def _skill_items() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import SKILLS_DIR, _parse_frontmatter

    skills_root = SKILLS_DIR
    items: list[dict[str, Any]] = []
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
            name = str(frontmatter.get("name") or skill_md.parent.name)
            item_id = _safe_relative(skill_md.parent, skills_root)
            items.append(
                {
                    "id": item_id,
                    "name": name,
                    "description": frontmatter.get("description") or None,
                    "homepage": frontmatter.get("homepage") or frontmatter.get("url") or None,
                    "path": str(skill_md),
                }
            )
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    context = {
        "skillsRoot": str(skills_root),
    }
    return items, context


def _cron_items() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from cron import jobs as cron_jobs

    items: list[dict[str, Any]] = []
    enabled = 0
    disabled = 0
    for job in cron_jobs.list_jobs(include_disabled=True):
        is_enabled = bool(job.get("enabled", True)) and job.get("state") != "paused"
        enabled += 1 if is_enabled else 0
        disabled += 0 if is_enabled else 1
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        items.append(
            {
                "id": job.get("id"),
                "name": job.get("name") or job.get("id"),
                "enabled": is_enabled,
                "state": job.get("state") or ("enabled" if is_enabled else "disabled"),
                "schedule": schedule,
                "scheduleDisplay": job.get("schedule_display") or schedule.get("display"),
                "nextRunAt": job.get("next_run_at"),
                "lastRunAt": job.get("last_run_at"),
                "lastStatus": job.get("last_status"),
                "completedRuns": job.get("completed_runs", 0),
                "deliver": job.get("deliver"),
                "origin": job.get("origin"),
                "skills": job.get("skills") or [],
                "workdir": job.get("workdir"),
            }
        )
    context = {
        "storePath": str(cron_jobs.JOBS_FILE),
    }
    summary = {
        "enabled": enabled,
        "disabled": disabled,
    }
    return items, context, summary


def maybe_local_command(event: MessageEvent) -> str | None:
    command = event.get_command()
    if not command:
        return None
    canonical = command.strip().lower().replace("_", "-")
    raw_args = event.get_command_args().strip()
    normalized = raw_args.lower().strip()
    full_command = f"/{command} {raw_args}".strip()

    if canonical == "skills" and normalized in {"", "list"}:
        try:
            items, context = _skill_items()
            context.update(
                {
                    "agentId": "main",
                    "workspaceDir": os.getcwd(),
                }
            )
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

    if canonical in {"cron", "schedules", "timers"} and normalized in {"", "list", "show", "all"}:
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

    return None


def _cli_commands_for_bridge() -> dict[str, str]:
    from hermes_cli.commands import COMMAND_REGISTRY

    commands = dict(_EXTRA_CLI_COMMANDS)
    for cmd in COMMAND_REGISTRY:
        commands.setdefault(cmd.name, cmd.description)
        for alias in cmd.aliases:
            commands.setdefault(alias, cmd.description)
    return commands


def is_cli_bridge_command(command: str | None) -> bool:
    if not command:
        return False
    normalized = command.strip().lower().lstrip("/").replace("_", "-")
    return normalized == "hermes" or normalized in _cli_commands_for_bridge()


def _format_cli_output(argv: list[str], returncode: int, stdout: str, stderr: str, limit: int) -> str:
    output = stdout.strip()
    err = stderr.strip()
    if err:
        output = f"{output}\n{err}".strip() if output else err
    if not output:
        output = "Command returned no output."
    if len(output) > limit:
        output = output[:limit] + "\n...[truncated]"
    status = "ok" if returncode == 0 else f"exit {returncode}"
    return f"$ hermes {' '.join(shlex.quote(part) for part in argv)}\n[{status}]\n{output}"


async def run_cli_bridge(event: MessageEvent, config: Any) -> str:
    command = event.get_command()
    raw_args = event.get_command_args().strip()
    try:
        if command and command.lower().replace("_", "-") == "hermes":
            argv = shlex.split(raw_args)
        else:
            argv = [command.replace("_", "-") if command else "", *shlex.split(raw_args)]
    except ValueError as exc:
        return f"Invalid command syntax: {exc}"

    argv = [arg for arg in argv if arg]
    if not argv:
        return "Usage: /hermes <command> [args...]"

    extra = _extra(config)
    try:
        timeout = int(extra.get("command_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    try:
        output_limit = int(extra.get("command_output_limit", DEFAULT_OUTPUT_LIMIT))
    except (TypeError, ValueError):
        output_limit = DEFAULT_OUTPUT_LIMIT

    env = os.environ.copy()
    env.setdefault("HERMES_AOPS_COMMAND", "1")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hermes_cli.main",
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return f"Command timed out after {timeout}s: `hermes {' '.join(shlex.quote(part) for part in argv)}`"

    stdout = stdout_b.decode(errors="replace") if stdout_b else ""
    stderr = stderr_b.decode(errors="replace") if stderr_b else ""
    return _format_cli_output(argv, proc.returncode or 0, stdout, stderr, output_limit)


def _blocked_names(config: Any) -> set[str]:
    return blocked_commands(config)


def is_help_line_blocked(line: str, config: Any) -> bool:
    blocked = _blocked_names(config)
    if not blocked:
        return False
    try:
        import re

        names = re.findall(r"`/([^`\s]+)", line)
    except Exception:
        names = []
    if not names:
        return False
    normalized = {name.strip().lower().replace("_", "-") for name in names}
    return bool(normalized & blocked)


def filter_help_lines(lines: Iterable[str], config: Any) -> list[str]:
    return [line for line in lines if not is_help_line_blocked(line, config)]


def aops_cli_help_lines(config: Any) -> list[str]:
    blocked = _blocked_names(config)
    lines: list[str] = []
    seen: set[str] = set()
    for name, description in sorted(_cli_commands_for_bridge().items()):
        normalized = name.lower().replace("_", "-")
        if normalized in blocked or normalized in seen:
            continue
        seen.add(normalized)
        args = " <args...>" if normalized != "version" else ""
        lines.append(f"`/{normalized}{args}` -- {description}")
    lines.append("`/hermes <command> [args...]` -- Run a Hermes CLI command exactly")
    return lines
