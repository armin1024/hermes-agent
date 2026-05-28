"""Backend-only bridge for AOPS silent SkillHub commands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

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
        model = raw.get("model")
    return {
        "parentMessageId": metadata.get("id"),
        "botId": metadata.get("botId"),
        "agentId": metadata.get("agentId"),
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


def _list_market_items() -> list[dict[str, Any]]:
    from tools.skills_hub import ClawHubSource

    source = ClawHubSource()
    catalog = source._load_catalog_index()
    items: list[dict[str, Any]] = []
    for meta in catalog:
        extra = dict(meta.extra or {})
        slug = meta.identifier
        if not slug:
            continue
        items.append(
            {
                "slug": slug,
                "displayName": meta.name,
                "summary": meta.description,
                "tags": list(meta.tags or []),
                "stats": _coerce_stats(extra),
                "updatedAt": _entry_updated_ms(extra),
                "latestVersion": _latest_version(extra),
            }
        )
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


def _installed_path(slug: str) -> str | None:
    from tools.skills_hub import HubLockFile

    entry = HubLockFile().get_installed(slug)
    if not isinstance(entry, dict):
        return None
    path = entry.get("install_path")
    return str(path) if path else None


def _install_skill(slug: str) -> tuple[bool, dict[str, Any]]:
    from hermes_cli.skills_hub import do_install

    ok, message = _capture_cli_call(
        do_install,
        slug,
        skip_confirm=True,
        invalidate_cache=True,
    )
    payload = {
        "ok": ok,
        "action": "install",
        "slug": slug,
        "message": message,
        "installedPath": _installed_path(slug),
    }
    return ok, payload


def _uninstall_skill(slug: str) -> tuple[bool, dict[str, Any]]:
    from hermes_cli.skills_hub import do_uninstall

    ok, message = _capture_cli_call(
        do_uninstall,
        slug,
        skip_confirm=True,
        invalidate_cache=True,
    )
    payload = {
        "ok": ok,
        "action": "uninstall",
        "slug": slug,
        "message": message,
    }
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
                        "body": {"items": items},
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
                "body": body,
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
                "body": body,
            },
        )
        return [result, _done_result(event, command)]

    return [_error_payload(event, command, "UNSUPPORTED_COMMAND", f"Unsupported clawhub subcommand: {action}")]
