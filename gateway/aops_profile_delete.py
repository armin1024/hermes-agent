"""AOPS profile deletion confirmation and preview helpers.

The destructive operation itself runs in a detached worker (see
``hermes_cli.profile_delete_worker``).  Keeping confirmation state here makes
the AOPS command path cheap and lets the gateway send the acceptance reply
before the profile's own service is stopped.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_CONFIRMATION_TTL_SECONDS = 5 * 60
_PENDING_LOCK = threading.RLock()
_PENDING: dict[str, dict[str, Any]] = {}
_IN_PROGRESS: dict[str, float] = {}


def _profile_context() -> tuple[str, Path]:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    if home.parent.name == "profiles":
        return home.name, home
    return "default", home


def _event_scope(event: Any, profile: str) -> tuple[str, str, str, str]:
    source = getattr(event, "source", None)
    return (
        profile,
        str(getattr(source, "user_id", None) or ""),
        str(getattr(source, "chat_id", None) or ""),
        str(getattr(source, "thread_id", None) or ""),
    )


def _cleanup_expired(now: float | None = None) -> None:
    cutoff = time.time() if now is None else now
    for token, item in list(_PENDING.items()):
        if float(item.get("expiresAtMs", 0)) <= cutoff * 1000:
            _PENDING.pop(token, None)
    for profile, started in list(_IN_PROGRESS.items()):
        if cutoff - started > 15 * 60:
            _IN_PROGRESS.pop(profile, None)


def _service_name(profile: str, profile_home: Path) -> str:
    old_home = __import__("os").environ.get("HERMES_HOME")
    try:
        __import__("os").environ["HERMES_HOME"] = str(profile_home)
        from hermes_cli.gateway import get_service_name

        return str(get_service_name())
    except Exception:
        return "hermes-gateway.service" if profile == "default" else f"hermes-gateway-{profile}.service"
    finally:
        if old_home is None:
            __import__("os").environ.pop("HERMES_HOME", None)
        else:
            __import__("os").environ["HERMES_HOME"] = old_home


def _skill_count(profile_home: Path) -> int:
    skills_root = profile_home / "skills"
    if not skills_root.is_dir():
        return 0
    try:
        return sum(1 for path in skills_root.rglob("SKILL.md") if path.is_file())
    except OSError:
        return 0


def _base_data(profile: str, profile_home: Path) -> dict[str, Any]:
    running = False
    try:
        from hermes_cli.profiles import _check_gateway_running

        running = bool(_check_gateway_running(profile_home))
    except Exception:
        pass
    return {
        "profile": {
            "name": profile,
            "path": str(profile_home),
            "isDefault": profile == "default",
            "gatewayRunning": running,
            "serviceName": _service_name(profile, profile_home),
            "skillCount": _skill_count(profile_home),
        },
        "effectiveImmediately": False,
        "restartRequired": False,
    }


def _error(command_text: str, code: str, message: str, data: dict[str, Any] | None = None):
    from gateway.aops_commands import LocalCommandResult, _single_response

    payload = dict(data or {})
    payload.setdefault("effectiveImmediately", False)
    payload.setdefault("restartRequired", False)
    return LocalCommandResult(
        text=_single_response(
            type_="profile.delete.preview",
            command=command_text,
            data=payload,
            ok=False,
            error={"code": code, "message": message},
        ),
        metadata={},
    )


def preview(command_text: str, event: Any):
    from gateway.aops_commands import LocalCommandResult, _single_response

    profile, profile_home = _profile_context()
    data = _base_data(profile, profile_home)
    if profile == "default":
        return _error(command_text, "PROFILE_DEFAULT_PROTECTED", "The default profile cannot be deleted.", data)
    if not profile_home.is_dir():
        return _error(command_text, "PROFILE_NOT_FOUND", f"Profile `{profile}` does not exist.", data)

    token = secrets.token_urlsafe(24)
    expires_at_ms = int((time.time() + _CONFIRMATION_TTL_SECONDS) * 1000)
    operation_id = uuid.uuid4().hex
    scope = _event_scope(event, profile)
    with _PENDING_LOCK:
        _cleanup_expired()
        if profile in _IN_PROGRESS:
            return _error(command_text, "PROFILE_DELETE_IN_PROGRESS", f"Profile `{profile}` deletion is already in progress.", data)
        for old_token, item in list(_PENDING.items()):
            if tuple(item.get("scope", ())) == scope:
                _PENDING.pop(old_token, None)
        _PENDING[token] = {
            "scope": scope,
            "profile": profile,
            "profileHome": str(profile_home),
            "expiresAtMs": expires_at_ms,
            "operationId": operation_id,
        }

    data["deletion"] = {
        "confirmationToken": token,
        "expiresAtMs": expires_at_ms,
        "irreversible": True,
        "localDataDeleted": [
            "config",
            "env",
            "memories",
            "sessions",
            "skills",
            "cron",
            "hindsight-config",
            "gateway-state",
        ],
        "remoteHindsightRetained": True,
    }
    return LocalCommandResult(
        text=_single_response(
            type_="profile.delete.preview",
            command=command_text,
            data=data,
            ok=True,
            error=None,
        ),
        metadata={},
    )


def confirm(command_text: str, event: Any, token: str):
    from gateway.aops_commands import LocalCommandResult, _single_response

    profile, profile_home = _profile_context()
    data = _base_data(profile, profile_home)
    token = str(token or "").strip()
    if not token:
        return _error(command_text, "PROFILE_DELETE_CONFIRMATION_REQUIRED", "A confirmation token is required.", data)
    now_ms = int(time.time() * 1000)
    with _PENDING_LOCK:
        _cleanup_expired(now_ms / 1000)
        item = _PENDING.get(token)
        if item is None:
            return _error(command_text, "PROFILE_DELETE_CONFIRMATION_INVALID", "The confirmation token is invalid or expired.", data)
        if int(item.get("expiresAtMs", 0)) <= now_ms:
            _PENDING.pop(token, None)
            return _error(command_text, "PROFILE_DELETE_CONFIRMATION_EXPIRED", "The confirmation token has expired.", data)
        if tuple(item.get("scope", ())) != _event_scope(event, profile):
            return _error(command_text, "PROFILE_DELETE_CONFIRMATION_INVALID", "The confirmation token is bound to another AOPS session.", data)
        _PENDING.pop(token, None)
        _IN_PROGRESS[profile] = time.time()

    operation_id = str(item.get("operationId") or uuid.uuid4().hex)
    data["profile"] = {"name": profile}
    data["deletion"] = {
        "status": "scheduled",
        "operationId": operation_id,
        "irreversible": True,
        "remoteHindsightRetained": True,
    }
    data["effectiveImmediately"] = True
    return LocalCommandResult(
        text=_single_response(
            type_="profile.delete.accepted",
            command=command_text,
            data=data,
            ok=True,
            error=None,
        ),
        metadata={
            "effects": {
                "deleteProfile": {
                    "profile": profile,
                    "profileHome": str(profile_home),
                    "operationId": operation_id,
                }
            }
        },
    )


def is_delete_command(event: Any) -> bool:
    try:
        return (
            str(event.get_command() or "").strip().lower() == "profile"
            and (str(event.get_command_args() or "").strip().lower().split()[:1] == ["delete"])
        )
    except Exception:
        return False


def handle(command_text: str, event: Any, args: list[str]):
    if len(args) == 1:
        return preview(command_text, event)
    if len(args) == 3 and args[1].lower() == "confirm":
        return confirm(command_text, event, args[2])
    return _error(
        command_text,
        "PROFILE_DELETE_CONFIRMATION_REQUIRED",
        "Usage: /profile delete, then /profile delete confirm <token>.",
        _base_data(*_profile_context()),
    )


__all__ = ["handle", "is_delete_command"]
