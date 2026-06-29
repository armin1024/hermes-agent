"""Backend-only bridge for AOPS silent SkillHub commands."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from rich.console import Console

from gateway.platforms.base import MessageEvent


@dataclass(frozen=True)
class SkillHubBridgeResult:
    command: str
    payload: dict[str, Any]
    done: bool = False


def is_silent_skillhub_command(event: MessageEvent) -> bool:
    text = (getattr(event, "text", "") or "").strip()
    if not text.startswith("/bash clawhub "):
        return False
    raw = getattr(event, "raw_message", None)
    if not isinstance(raw, dict):
        return False
    metadata = raw.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("silent"), bool):
        return bool(metadata.get("silent"))
    if isinstance(metadata, dict) and str(metadata.get("messageType") or "").strip().lower() == "silent":
        return True
    if isinstance(raw.get("silent"), bool):
        return bool(raw.get("silent"))
    if str(raw.get("messageType") or "").strip().lower() == "silent":
        return True
    return False


def _metadata(event: MessageEvent) -> dict[str, Any]:
    raw = getattr(event, "raw_message", None)
    if not isinstance(raw, dict):
        return {}
    meta = raw.get("metadata")
    if isinstance(meta, dict):
        return dict(meta)
    return {}


def _request_context(event: MessageEvent) -> dict[str, Any]:
    raw = getattr(event, "raw_message", None)
    metadata = _metadata(event)
    model = None
    if isinstance(raw, dict):
        model = raw.get("model") or metadata.get("model")
    return {
        "parentMessageId": metadata.get("id") or (raw.get("id") if isinstance(raw, dict) else None) or getattr(event, "message_id", None),
        "botId": metadata.get("botId") or (raw.get("botId") if isinstance(raw, dict) else None),
        "agentId": metadata.get("agentId") or (raw.get("agentId") if isinstance(raw, dict) else None) or (raw.get("agentKey") if isinstance(raw, dict) else None),
        "model": model,
        "silent": True,
    }


def _error_payload(event: MessageEvent, command: str, code: str, message: str, *, details: dict[str, Any] | None = None) -> SkillHubBridgeResult:
    return SkillHubBridgeResult(
        command=command,
        payload={
            "schemaVersion": "aops.skillhub.result.v1",
            "type": "commandResult",
            "ok": False,
            "command": command,
            "context": _request_context(event),
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
        },
    )


def _done_result(event: MessageEvent, command: str) -> SkillHubBridgeResult:
    return SkillHubBridgeResult(
        command=command,
        done=True,
        payload={
            "schemaVersion": "aops.skillhub.result.v1",
            "type": "commandResult",
            "ok": True,
            "command": command,
            "context": _request_context(event),
            "done": True,
        },
    )


def _entry_updated_ms(extra: dict[str, Any]) -> int | None:
    value = extra.get("updatedAt") or extra.get("updated_at")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None
    return None


def _coerce_stats(extra: dict[str, Any]) -> dict[str, int]:
    stats = extra.get("stats")
    if isinstance(stats, dict):
        downloads = stats.get("downloads", 0)
        stars = stats.get("stars", 0)
        return {
            "downloads": int(downloads or 0),
            "stars": int(stars or 0),
        }
    return {"downloads": 0, "stars": 0}


def _latest_version(extra: dict[str, Any]) -> dict[str, Any]:
    latest = extra.get("latestVersion")
    if isinstance(latest, dict):
        return {"version": latest.get("version")}
    if isinstance(latest, str):
        return {"version": latest}
    return {"version": None}


def _shell_value(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw, comments=True, posix=True)
        if parts:
            return parts[0].strip() or None
    except ValueError:
        pass
    value = raw.split("#", 1)[0].strip().strip("'\"")
    return value or None


def _load_clawhub_registry_from_shell_init() -> None:
    if os.environ.get("CLAWHUB_REGISTRY"):
        return
    for name in (".bashrc", ".bash_profile", ".profile"):
        path = Path.home() / name
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].lstrip()
            if not stripped.startswith("CLAWHUB_REGISTRY="):
                continue
            value = _shell_value(stripped.split("=", 1)[1])
            if value:
                os.environ["CLAWHUB_REGISTRY"] = value
                return


def _clawhub_base_url() -> str:
    _load_clawhub_registry_from_shell_init()
    from tools.skills_hub import ClawHubSource

    if hasattr(ClawHubSource, "configured_base_url"):
        return str(ClawHubSource.configured_base_url()).rstrip("/")
    registry = os.environ.get("CLAWHUB_REGISTRY", "").strip().rstrip("/")
    if registry:
        return f"{registry}/api/v1"
    return str(ClawHubSource.BASE_URL).rstrip("/")


def _configure_clawhub_source_base_url() -> str:
    """Keep the shared Skills Hub ClawHub adapter aligned with CLAWHUB_REGISTRY."""
    base_url = _clawhub_base_url()
    from tools.skills_hub import ClawHubSource

    ClawHubSource.BASE_URL = base_url
    return base_url


@contextmanager
def _clawhub_only_source_router():
    """Force hermes_cli.skills_hub install resolution to ClawHub only.

    `/bash clawhub ...` is an AOPS backend bridge for Tec01's configured
    SkillHub/ClawHub registry. The generic CLI installer searches HermesIndex,
    GitHub, skills.sh, official, marketplace, etc. when no --source is provided;
    that is both slow and undesirable in closed intranet deployments. Keep this
    override scoped to the bridge call so normal `hermes skills install` retains
    its full-source behavior.
    """
    import tools.skills_hub as skills_hub

    original = skills_hub.create_source_router

    def _create_clawhub_sources(auth=None):  # noqa: ARG001 - signature mirrors original
        _configure_clawhub_source_base_url()
        return [skills_hub.ClawHubSource()]

    skills_hub.create_source_router = _create_clawhub_sources
    try:
        yield
    finally:
        skills_hub.create_source_router = original


def _market_item_from_raw(item: dict[str, Any]) -> dict[str, Any] | None:
    slug = item.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    display_name = item.get("displayName") or item.get("name") or slug
    summary = item.get("summary") or item.get("description") or ""
    return {
        "slug": slug,
        "displayName": str(display_name),
        "summary": str(summary),
        "tags": [str(tag) for tag in item.get("tags", [])] if isinstance(item.get("tags"), list) else [],
        "stats": _coerce_stats(item),
        "updatedAt": _entry_updated_ms(item),
        "latestVersion": _latest_version(item),
    }


def _list_market_items() -> list[dict[str, Any]]:
    base_url = _configure_clawhub_source_base_url()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    max_pages = 50

    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(f"{base_url}/skills", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        raw_items = data.get("items", data) if isinstance(data, dict) else data
        if not isinstance(raw_items, list) or not raw_items:
            break
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = _market_item_from_raw(raw_item)
            if not item or item["slug"] in seen:
                continue
            seen.add(item["slug"])
            items.append(item)
        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not isinstance(cursor, str) or not cursor:
            break

    return items


def _capture_cli_call(fn, *args, **kwargs) -> tuple[bool, str]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=120, record=True)
    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            fn(*args, console=console, **kwargs)
        return True, stream.getvalue().strip()
    except Exception as exc:
        return False, (stream.getvalue().strip() or str(exc))


def _message_indicates_failure(message: str) -> bool:
    lowered = message.lower()
    failure_markers = (
        "error:",
        "installation blocked:",
        "could not fetch",
        "no skill named",
        "cannot install",
        "cancelled.",
        "not a hub-installed skill",
    )
    return any(marker in lowered for marker in failure_markers)


def _scan_policy_summary_from_message(message: str) -> dict[str, Any]:
    match = re.search(
        r"Scan policy ignored for trusted AOPS install:\s*verdict=([^\s]+)\s+findings=(\d+)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    return {
        "scanIgnored": True,
        "scanVerdict": match.group(1),
        "scanFindingsCount": int(match.group(2)),
    }


def _installed_path(slug: str) -> str | None:
    from tools.skills_hub import HubLockFile

    entry = HubLockFile().get_installed(slug)
    if not isinstance(entry, dict):
        return None
    path = entry.get("install_path")
    return str(path) if path else None


def _refresh_skill_runtime() -> None:
    """Best-effort refresh so SkillHub changes are visible to the next turn."""
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


def _install_skill(slug: str) -> tuple[bool, dict[str, Any]]:
    from hermes_cli.skills_hub import do_install

    _configure_clawhub_source_base_url()
    with _clawhub_only_source_router():
        ok, message = _capture_cli_call(
            do_install,
            slug,
            force=True,
            skip_confirm=True,
            invalidate_cache=True,
            ignore_scan_policy=True,
        )
    installed_path = _installed_path(slug)
    if _message_indicates_failure(message) or not installed_path:
        ok = False
    payload = {
        "ok": ok,
        "action": "install",
        "slug": slug,
        "message": message,
        "installedPath": installed_path,
    }
    payload.update(_scan_policy_summary_from_message(message))
    if not ok:
        payload["error"] = {
            "code": "INSTALL_FAILED",
            "message": message or f"Failed to install '{slug}'.",
            "details": {},
        }
    else:
        _refresh_skill_runtime()
    return ok, payload


def _uninstall_skill(slug: str) -> tuple[bool, dict[str, Any]]:
    from gateway.aops_skill_uninstall import (
        is_not_hub_installed_message,
        uninstall_local_skill_fallback,
    )
    from hermes_cli.skills_hub import do_uninstall

    _configure_clawhub_source_base_url()
    ok, message = _capture_cli_call(
        do_uninstall,
        slug,
        skip_confirm=True,
        invalidate_cache=True,
    )
    if _message_indicates_failure(message):
        ok = False
    source = "hub"
    removed_path = None
    if not ok and is_not_hub_installed_message(message):
        local = uninstall_local_skill_fallback(slug)
        if local.get("ok"):
            ok = True
            source = "local"
            removed_path = local.get("removedPath")
            local_message = str(local.get("message") or "").strip()
            message = local_message or f"Uninstalled local skill '{slug}'."
        else:
            err = local.get("error") if isinstance(local.get("error"), dict) else {}
            message = str(err.get("message") or message or f"Failed to uninstall '{slug}'.")
    payload = {
        "ok": ok,
        "action": "uninstall",
        "slug": slug,
        "source": source,
        "removedPath": removed_path,
        "message": message,
    }
    if not ok:
        payload["error"] = {
            "code": "UNINSTALL_FAILED",
            "message": message or f"Failed to uninstall '{slug}'.",
            "details": {},
        }
    else:
        _refresh_skill_runtime()
    return ok, payload


def execute_silent_skillhub_command(event: MessageEvent) -> list[SkillHubBridgeResult] | None:
    if not is_silent_skillhub_command(event):
        return None

    text = (getattr(event, "text", "") or "").strip()
    command = text[len("/bash "):].strip()
    parts = command.split()
    if len(parts) < 2 or parts[0] != "clawhub":
        return [_error_payload(event, command, "UNSUPPORTED_COMMAND", "Only clawhub commands are supported.")]

    action = parts[1]
    if action == "explore":
        if parts[2:] != ["--json"]:
            return [_error_payload(event, command, "INVALID_COMMAND", "Only `clawhub explore --json` is supported.")]
        try:
            items = _list_market_items()
            return [
                SkillHubBridgeResult(
                    command=command,
                    payload={
                        "schemaVersion": "aops.skillhub.result.v1",
                        "type": "commandResult",
                        "ok": True,
                        "command": command,
                        "context": _request_context(event),
                        "items": items,
                    },
                )
            ]
        except Exception as exc:
            return [_error_payload(event, command, "EXPLORE_FAILED", str(exc))]

    if action == "install":
        slug = parts[2] if len(parts) >= 3 else ""
        if not slug:
            return [_error_payload(event, command, "INVALID_COMMAND", "Missing skill slug for install.")]
        ok, body = _install_skill(slug)
        result = SkillHubBridgeResult(
            command=command,
            payload={
                "schemaVersion": "aops.skillhub.result.v1",
                "type": "commandResult",
                "ok": ok,
                "command": command,
                "context": _request_context(event),
                **body,
            },
        )
        return [result, _done_result(event, command)]

    if action == "uninstall":
        slug = parts[2] if len(parts) >= 3 else ""
        if not slug:
            return [_error_payload(event, command, "INVALID_COMMAND", "Missing skill slug for uninstall.")]
        ok, body = _uninstall_skill(slug)
        result = SkillHubBridgeResult(
            command=command,
            payload={
                "schemaVersion": "aops.skillhub.result.v1",
                "type": "commandResult",
                "ok": ok,
                "command": command,
                "context": _request_context(event),
                **body,
            },
        )
        return [result, _done_result(event, command)]

    return [_error_payload(event, command, "UNSUPPORTED_COMMAND", f"Unsupported clawhub subcommand: {action}")]
