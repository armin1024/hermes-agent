"""AOPS gateway adapter and native streaming bridge."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urljoin, urlparse, urlunparse

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    proxy_kwargs_for_aiohttp,
    resolve_proxy_url,
)
from hermes_constants import get_default_hermes_root, get_hermes_home

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_AOPS_LOG_RETENTION_DAYS_DEFAULT = 7
_AOPS_LOG_LOCK = threading.Lock()
_DONE = object()
_SEGMENT_BREAK = object()
_COMMENTARY = object()
_TOOL = object()
_FINAL = object()
_ERROR = object()
_AOPS_CLIENT_ID_CACHE: dict[str, str] = {}
_AOPS_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_AOPS_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0


def check_aops_requirements() -> bool:
    """Return True when the AOPS adapter can run."""
    return AIOHTTP_AVAILABLE


def _coerce_str_list(value: Any, *, default: Optional[list[str]] = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else list(default or [])


def _coerce_float(value: Any, *, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("[Aops] Ignoring invalid %s=%r", name, value)
        return default
    return max(0.0, parsed)


def _current_system_user() -> str:
    try:
        username = getpass.getuser().strip()
        if username:
            return username
    except Exception:
        pass
    try:
        return str(os.getuid())
    except Exception:
        return "unknown-user"


def _is_reportable_ipv4(value: Any) -> bool:
    try:
        ip = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return False
    return ip.version == 4 and not ip.is_loopback


def _dedupe_sorted_ipv4(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen or not _is_reportable_ipv4(text):
            continue
        seen.add(text)
        out.append(text)
    return sorted(out, key=lambda item: tuple(int(part) for part in item.split(".")))


def _collect_ipv4_from_ip_addr() -> list[str]:
    try:
        proc = subprocess.run(
            ["ip", "-j", "-4", "addr"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return []
    addresses: list[str] = []
    if isinstance(payload, list):
        for link in payload:
            if not isinstance(link, dict):
                continue
            if str(link.get("ifname") or "").strip().lower() == "lo":
                continue
            for addr in link.get("addr_info") or []:
                if not isinstance(addr, dict):
                    continue
                if str(addr.get("family") or "").lower() != "inet":
                    continue
                addresses.append(str(addr.get("local") or ""))
    return _dedupe_sorted_ipv4(addresses)


def _collect_ipv4_from_socket() -> list[str]:
    names = {socket.gethostname(), socket.getfqdn()}
    addresses: list[str] = []
    for name in names:
        if not name:
            continue
        try:
            infos = socket.getaddrinfo(name, None, socket.AF_INET)
        except Exception:
            continue
        for info in infos:
            sockaddr = info[4] if len(info) > 4 else None
            if isinstance(sockaddr, tuple) and sockaddr:
                addresses.append(str(sockaddr[0]))
    return _dedupe_sorted_ipv4(addresses)


def _collect_host_ipv4s() -> list[str]:
    return _collect_ipv4_from_ip_addr() or _collect_ipv4_from_socket()


def _resolve_aops_profile_name() -> str:
    raw_profile = os.getenv("HERMES_PROFILE", "").strip()
    if raw_profile:
        return raw_profile
    home = get_hermes_home()
    try:
        if home.parent.name == "profiles":
            return home.name or "default"
    except Exception:
        pass
    return "default"


def _runtime_secret_from_model_config(model_cfg: dict[str, Any]) -> tuple[str, str]:
    api_key = str(model_cfg.get("api_key") or "").strip()
    if api_key:
        return api_key, "config.model.api_key"
    api_key_env = str(model_cfg.get("api_key_env") or "").strip()
    if api_key_env:
        try:
            from hermes_cli.config import get_env_value

            env_value = (get_env_value(api_key_env) or os.getenv(api_key_env, "")).strip()
        except Exception:
            env_value = os.getenv(api_key_env, "").strip()
        if env_value:
            return env_value, f"env:{api_key_env}"
    return "", ""


def _runtime_secret_from_provider(provider: str, model_name: str, base_url: str) -> tuple[str, str, str]:
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=provider or None,
            explicit_base_url=base_url or None,
            target_model=model_name or None,
        )
    except Exception:
        return "", "", ""
    api_key = str(runtime.get("api_key") or "").strip()
    source = str(runtime.get("source") or "").strip()
    resolved_base_url = str(runtime.get("base_url") or "").strip()
    return api_key, source, resolved_base_url


def _build_aops_model_runtime_report() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    raw_model = cfg.get("model") if isinstance(cfg, dict) else {}
    model_cfg = raw_model if isinstance(raw_model, dict) else {}
    if isinstance(raw_model, str):
        model_cfg = {"default": raw_model}

    provider = str(model_cfg.get("provider") or "").strip()
    model = str(model_cfg.get("model") or model_cfg.get("default") or model_cfg.get("name") or "").strip()
    default_model = str(model_cfg.get("default") or "").strip()
    base_url = str(model_cfg.get("base_url") or "").strip()
    api_mode = str(model_cfg.get("api_mode") or "").strip()
    api_key_env = str(model_cfg.get("api_key_env") or "").strip()

    api_key, _source = _runtime_secret_from_model_config(model_cfg)
    if not api_key:
        api_key, _source, resolved_base_url = _runtime_secret_from_provider(provider, model, base_url)
        if resolved_base_url and not base_url:
            base_url = resolved_base_url

    report: dict[str, Any] = {}
    if provider:
        report["provider"] = provider
    if model:
        report["model"] = model
    if default_model:
        report["default"] = default_model
    if base_url:
        report["baseUrl"] = base_url
    if api_mode:
        report["apiMode"] = api_mode
    if api_key_env:
        report["apiKeyEnv"] = api_key_env
    if api_key:
        report["apiKey"] = api_key
    return report


def _build_aops_runtime_report() -> dict[str, Any]:
    try:
        hostname = socket.gethostname().strip()
    except Exception:
        hostname = ""
    hermes_home = get_hermes_home()
    runtime: dict[str, Any] = {
        "schema": "aops-runtime-report.v1",
        "host": {
            "ips": _collect_host_ipv4s(),
        },
        "user": {
            "systemUser": _current_system_user(),
        },
        "hermes": {
            "home": str(hermes_home),
            "profile": _resolve_aops_profile_name(),
        },
        "model": _build_aops_model_runtime_report(),
    }
    if hostname:
        runtime["host"]["hostname"] = hostname
    return runtime


def _redact_aops_agent_report_for_log(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    redacted = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    try:
        model = redacted["body"]["runtime"]["model"]
        if isinstance(model, dict) and model.get("apiKey"):
            model["apiKey"] = "[REDACTED]"
    except Exception:
        pass
    return redacted


def _aops_client_user_key() -> str:
    return hashlib.sha256(_current_system_user().encode("utf-8")).hexdigest()[:16]


def _legacy_aops_client_id_path() -> Path:
    return get_hermes_home() / "aops" / f"client-id-{_aops_client_user_key()}"


def _aops_client_id_path(config: PlatformConfig | None = None) -> Path:
    del config
    return get_default_hermes_root() / "aops" / f"client-id-v2-{_aops_client_user_key()}"


def _aops_allow_legacy_client_id_migration() -> bool:
    raw = os.getenv("AOPS_MIGRATE_LEGACY_CLIENT_ID", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _normalize_aops_client_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw.strip()))
    except Exception:
        return ""


def _resolve_aops_client_id(config: PlatformConfig) -> str:
    client_id_path = _aops_client_id_path(config)
    cache_key = str(client_id_path)
    cached = _AOPS_CLIENT_ID_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        existing = _normalize_aops_client_id(client_id_path.read_text(encoding="utf-8"))
        if existing:
            _AOPS_CLIENT_ID_CACHE[cache_key] = existing
            return existing
    except Exception:
        pass

    legacy_path = _legacy_aops_client_id_path()
    if _aops_allow_legacy_client_id_migration() and legacy_path != client_id_path:
        try:
            existing = _normalize_aops_client_id(legacy_path.read_text(encoding="utf-8"))
            if existing:
                try:
                    client_id_path.parent.mkdir(parents=True, exist_ok=True)
                    client_id_path.write_text(existing + "\n", encoding="utf-8")
                except Exception:
                    pass
                _AOPS_CLIENT_ID_CACHE[cache_key] = existing
                return existing
        except Exception:
            pass
    elif legacy_path.exists():
        logger.info(
            "Ignoring legacy AOPS client id at %s because legacy migration is not enabled",
            legacy_path,
        )

    new_id = str(uuid.uuid4())
    try:
        client_id_path.parent.mkdir(parents=True, exist_ok=True)
        client_id_path.write_text(new_id + "\n", encoding="utf-8")
    except Exception:
        pass
    _AOPS_CLIENT_ID_CACHE[cache_key] = new_id
    return new_id


def _iso_to_datetime(raw: str | None) -> datetime:
    if not raw:
        return datetime.now()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.now()


def _entry_matches(entries: list[str], value: str | None) -> bool:
    if not value:
        return False
    normalized = str(value).strip()
    if not normalized:
        return False
    for entry in entries:
        if entry == "*" or entry == normalized:
            return True
    return False


def _build_ws_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = parsed.scheme.lower()
    if scheme == "https":
        ws_scheme = "wss"
    elif scheme == "http":
        ws_scheme = "ws"
    elif scheme in ("ws", "wss"):
        ws_scheme = scheme
    else:
        ws_scheme = "wss"
    return urlunparse(parsed._replace(scheme=ws_scheme, path="/api/v1/ws", params="", query="", fragment=""))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _aops_message_type(*, metadata: dict[str, Any], inherited_silent: bool | None) -> str:
    explicit = str(metadata.get("message_type") or "").strip().lower()
    if explicit == "cron":
        return "cron"
    if inherited_silent is True:
        return "silent"
    return "common"


def _aops_outbound_extra_fields(metadata: dict[str, Any], *, message_type: str) -> dict[str, Any]:
    """Return protocol metadata fields safe to expose beside messageType."""
    outbound: dict[str, Any] = {}
    source = str(metadata.get("source") or "").strip()
    if source:
        outbound["source"] = source
    bot_reply_extra = metadata.get("botReplyExtra")
    if isinstance(bot_reply_extra, dict):
        nested = bot_reply_extra.get("botReplyExtra")
        if isinstance(nested, dict):
            bot_reply_extra = nested
        for key in ("messageType", "id", "name"):
            value = bot_reply_extra.get(key)
            if value is not None:
                outbound[key] = value
    elif message_type:
        outbound["messageType"] = message_type
    return outbound


def _aops_normalize_outbound_protocol_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten legacy AOPS metadata/botReplyExtra shapes at the final send boundary."""
    normalized = dict(data)
    message_type = str(normalized.get("messageType") or "").strip().lower() or "common"
    metadata = normalized.pop("metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    extra = _aops_outbound_extra_fields(metadata, message_type=message_type)
    for key, value in extra.items():
        normalized.setdefault(key, value)
    bot_reply_extra = normalized.pop("botReplyExtra", None)
    if isinstance(bot_reply_extra, dict):
        nested = bot_reply_extra.get("botReplyExtra")
        if isinstance(nested, dict):
            bot_reply_extra = nested
        for key in ("messageType", "id", "name"):
            value = bot_reply_extra.get(key)
            if value is not None:
                normalized[key] = value
    normalized.setdefault("messageType", message_type)
    return normalized


def _normalize_aops_attachment_type(attachment: dict[str, Any]) -> str:
    raw_type = str(attachment.get("fileType") or attachment.get("file_type") or "").strip().lower()
    mime_type = str(attachment.get("mimeType") or attachment.get("mime_type") or "").strip().lower()
    file_name = str(attachment.get("fileName") or attachment.get("file_name") or "").strip()
    guessed_mime = mimetypes.guess_type(file_name)[0] or ""
    effective_mime = mime_type or guessed_mime.lower()

    if raw_type in {"image", "audio", "video", "pdf", "document", "spreadsheet", "presentation", "text", "archive"}:
        return raw_type
    if effective_mime.startswith("image/"):
        return "image"
    if effective_mime.startswith("audio/"):
        return "audio"
    if effective_mime.startswith("video/"):
        return "video"
    if effective_mime == "application/pdf":
        return "pdf"
    if effective_mime.startswith("text/"):
        return "text"
    ext = Path(file_name).suffix.lower()
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}:
        return "archive"
    if ext in {".doc", ".docx"}:
        return "document"
    if ext in {".xls", ".xlsx", ".csv"}:
        return "spreadsheet"
    if ext in {".ppt", ".pptx"}:
        return "presentation"
    return "unknown"


def _message_type_for_aops_media(media_types: list[str]) -> MessageType:
    normalized = [str(item or "").lower() for item in media_types]
    if any(item.startswith("image/") for item in normalized):
        return MessageType.PHOTO
    if any(item.startswith("audio/") for item in normalized):
        return MessageType.AUDIO
    if any(item.startswith("video/") for item in normalized):
        return MessageType.VIDEO
    if normalized:
        return MessageType.DOCUMENT
    return MessageType.TEXT


def _extract_aops_text(raw_text: Any) -> tuple[str, bool, str | None]:
    text = str(raw_text or "")
    stripped = text.strip()
    if not stripped.startswith("["):
        return text, False, None
    try:
        messages = json.loads(stripped)
    except Exception:
        return text, False, None
    if not isinstance(messages, list):
        return text, False, None
    system_parts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != "system":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            system_parts.append(content.strip())
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            system_prompt = "\n\n".join(system_parts).strip() or None
            return content, True, system_prompt
    return text, False, None


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _extract_aops_conversation_title(data: dict[str, Any]) -> str:
    metadata = _metadata_dict(data.get("metadata"))
    candidates = (
        data.get("title"),
        data.get("conversationTitle"),
        data.get("conversation_title"),
        data.get("chatTitle"),
        data.get("channelName"),
        metadata.get("title"),
        metadata.get("conversationTitle"),
        metadata.get("conversation_title"),
        metadata.get("chatTitle"),
        metadata.get("channelName"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _iter_aops_content_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return _iter_aops_content_items(parsed)
    return []


def _extract_aops_approval_action(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (slash-command, approval-id) for AOPS approval button callbacks."""
    metadata = _metadata_dict(data.get("metadata"))
    content_candidates = [
        *_iter_aops_content_items(data.get("content")),
        *_iter_aops_content_items(metadata.get("content")),
    ]
    candidates: list[dict[str, Any]] = [*content_candidates, metadata, data]

    action = ""
    approval_id = ""
    approval_kind = ""
    for item in candidates:
        raw_action = (
            item.get("action")
            or item.get("approvalAction")
            or item.get("approval_action")
            or item.get("value")
        )
        raw_id = item.get("approvalId") or item.get("approval_id")
        raw_kind = item.get("approvalKind") or item.get("approval_kind") or item.get("kind")
        if raw_id is None and item in content_candidates:
            raw_id = item.get("id")
        if not action and raw_action is not None:
            action = str(raw_action).strip().lower().replace("_", "-")
        if not approval_id and raw_id is not None:
            approval_id = str(raw_id).strip()
        if not approval_kind and raw_kind is not None:
            approval_kind = str(raw_kind).strip().lower().replace("_", "-")

    if approval_kind in {"slash", "slash-confirm", "confirm"}:
        command_by_action = {
            "/approve": "/approve",
            "allow-once": "/approve",
            "approve-once": "/approve",
            "once": "/approve",
            "/always": "/always",
            "/approve-always": "/always",
            "/approve always": "/always",
            "allow-always": "/always",
            "approve-always": "/always",
            "always": "/always",
            "/cancel": "/cancel",
            "/deny": "/cancel",
            "deny": "/cancel",
            "reject": "/cancel",
            "cancel": "/cancel",
        }
        return command_by_action.get(action), approval_id or None

    command_by_action = {
        "/approve": "/approve",
        "allow-once": "/approve",
        "approve-once": "/approve",
        "once": "/approve",
        "/approve-session": "/approve session",
        "/approve session": "/approve session",
        "allow-session": "/approve session",
        "approve-session": "/approve session",
        "session": "/approve session",
        "/approve-always": "/approve always",
        "/approve always": "/approve always",
        "allow-always": "/approve always",
        "approve-always": "/approve always",
        "always": "/approve always",
        "/deny": "/deny",
        "/cancel": "/deny",
        "deny": "/deny",
        "reject": "/deny",
        "cancel": "/deny",
    }
    return command_by_action.get(action), approval_id or None


def _extension_for_aops_attachment(filename: str, mime_type: str, file_type: str) -> str:
    ext = Path(filename).suffix
    if ext:
        return ext
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed:
        return guessed
    if file_type == "audio":
        return ".ogg"
    if file_type == "video":
        return ".mp4"
    if file_type == "image":
        return ".jpg"
    return ".bin"


def _format_local(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="milliseconds")


def _compact_log_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _aops_log_retention_days(config: PlatformConfig | None = None) -> int:
    value: Any = None
    if config is not None:
        extra = getattr(config, "extra", None)
        if isinstance(extra, dict):
            value = extra.get("log_retention_days")
    if value is None:
        value = os.getenv("AOPS_LOG_RETENTION_DAYS", "").strip()
    if value in (None, ""):
        return _AOPS_LOG_RETENTION_DAYS_DEFAULT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.debug("Ignoring invalid AOPS log retention days: %r", value)
        return _AOPS_LOG_RETENTION_DAYS_DEFAULT
    if parsed < 1:
        logger.debug("Ignoring invalid AOPS log retention days: %r", value)
        return _AOPS_LOG_RETENTION_DAYS_DEFAULT
    return parsed


def _aops_local_command_send_timeout() -> float:
    raw = os.getenv("AOPS_LOCAL_COMMAND_SEND_TIMEOUT", "2").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 2.0
    return value if value > 0 else 2.0


def _aops_local_command_exec_timeout() -> float:
    raw = os.getenv("AOPS_LOCAL_COMMAND_EXEC_TIMEOUT", "3").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 3.0
    return value if value > 0 else 3.0


def _quote_aops_log_value(value: Any, *, limit: int = 500) -> str:
    text = _compact_log_text(value, limit=limit)
    return json.dumps(text or "-", ensure_ascii=False)


def _aops_log_raw_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _aops_action_commands(actions: Any) -> list[str]:
    commands: list[str] = []
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                command = str(action.get("command") or action.get("value") or "").strip()
            else:
                command = str(action or "").strip()
            if command:
                commands.append(command)
    return commands


def _aops_log_event(action: str, raw_payload: Any) -> str:
    if isinstance(raw_payload, dict):
        event = str(raw_payload.get("event") or raw_payload.get("action") or "").strip()
        if event:
            return event
    return action or "-"


def _aops_log_message_type(data: dict[str, Any], *, action: str, filtered: bool = False) -> str:
    if filtered:
        return "filtered"
    phase = str(data.get("phase") or "").strip().lower()
    kind = str(data.get("kind") or "").strip().lower()
    if kind == "approval" or phase == "actions":
        return "approval"
    if kind == "tool" or phase == "tool":
        return "tool"
    message_type = str(data.get("messageType") or "").strip()
    if message_type:
        return message_type
    if data.get("messageId") or data.get("replyToId") or data.get("channelId") or data.get("text"):
        return "-"
    if action.startswith("ws.") or action in {"ping", "pong", "auth"}:
        return "ws"
    if action.startswith("http."):
        return "http"
    if action.startswith("attachment."):
        return "attachment"
    return "-"


def _aops_log_text(data: dict[str, Any], *, action: str, error: str | None = None, filtered: bool = False) -> str:
    if filtered:
        return "filtered reason=internal_thinking tool=_thinking"
    if error:
        base = action or "error"
        return f"{base} error={_compact_log_text(error, limit=200)}"
    text = data.get("text") or data.get("delta")
    if text is None and isinstance(data.get("content"), str):
        text = data.get("content")
    if text:
        return _compact_log_text(text)
    phase = str(data.get("phase") or "").strip().lower()
    kind = str(data.get("kind") or "").strip().lower()
    if kind == "approval" or phase == "actions":
        content = data.get("content")
        approval = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
        commands = _aops_action_commands(approval.get("allowedActions"))
        return (
            f"approval kind={approval.get('approvalKind') or '-'} "
            f"actions={','.join(commands) or '-'} "
            f"command={_compact_log_text(approval.get('command'), limit=200) or '-'}"
        )
    if kind == "tool" or phase == "tool":
        tool = data.get("tool") if isinstance(data.get("tool"), dict) else data
        result = tool.get("result") if isinstance(tool.get("result"), dict) else {}
        tool_text = data.get("text") or tool.get("preview") or result.get("text")
        tool_name = tool.get("name") or tool.get("tool_name") or "-"
        if str(tool_name) == "cronjob":
            cron_bits: list[str] = []
            args = tool.get("args") if isinstance(tool.get("args"), dict) else data.get("args")
            if isinstance(args, dict):
                for key in ("action", "job_id", "schedule", "repeat"):
                    if args.get(key) is not None:
                        cron_bits.append(f"{key}={_compact_log_text(args.get(key), limit=80)}")
            parsed_result = result.get("json") if isinstance(result.get("json"), dict) else None
            if not parsed_result and isinstance(result.get("text"), str):
                try:
                    loaded = json.loads(result["text"])
                    if isinstance(loaded, dict):
                        parsed_result = loaded
                except Exception:
                    parsed_result = None
            if parsed_result:
                job = parsed_result.get("job") if isinstance(parsed_result.get("job"), dict) else {}
                for key, value in (
                    ("job_id", parsed_result.get("job_id") or job.get("job_id")),
                    ("schedule", parsed_result.get("schedule") or job.get("schedule")),
                    ("repeat", parsed_result.get("repeat") or job.get("repeat")),
                    ("enabled", job.get("enabled")),
                    ("state", job.get("state")),
                ):
                    if value is not None:
                        cron_bits.append(f"{key}={_compact_log_text(value, limit=80)}")
            if cron_bits:
                tool_text = " ".join(cron_bits)
        return (
            f"tool phase={tool.get('phase') or data.get('phase') or '-'} "
            f"name={tool_name} "
            f"text={_compact_log_text(tool_text, limit=200) or '-'}"
        )
    if action.startswith("attachment."):
        summary = f"{action}"
        file_name = data.get("fileName") or data.get("file_name")
        if file_name:
            summary += f" file={file_name}"
        if error:
            summary += f" error={_compact_log_text(error, limit=200)}"
        elif data.get("reason"):
            summary += f" reason={data.get('reason')}"
        return summary
    if action.startswith("http."):
        summary = action
        status = data.get("status")
        method = data.get("method")
        url = data.get("url")
        if method:
            summary += f" method={method}"
        if status is not None:
            summary += f" status={status}"
        if url:
            summary += f" url={url}"
        if error:
            summary += f" error={_compact_log_text(error, limit=200)}"
        return summary
    if action == "ws.closed":
        close_code = data.get("closeCode") or data.get("data")
        reason_hint = f"server_closed_code_{close_code}" if close_code else "-"
        return f"closeCode={close_code or '-'} reasonHint={reason_hint}"
    return action or "-"


def _aops_log_data_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    payload_data = payload.get("data")
    if isinstance(payload_data, dict):
        return payload_data
    return payload


def _aops_is_internal_thinking_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    stack = [value]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        for key in ("tool_name", "name"):
            if str(item.get(key) or "").strip() == "_thinking":
                return True
        tool = item.get("tool")
        if isinstance(tool, dict):
            for key in ("tool_name", "name"):
                if str(tool.get(key) or "").strip() == "_thinking":
                    return True
            stack.append(tool)
        kind = str(item.get("kind") or "").strip().lower()
        phase = str(item.get("phase") or "").strip().lower()
        event_type = str(item.get("event_type") or item.get("eventType") or "").strip().lower()
        if kind == "thinking" or phase == "thinking" or event_type in {"_thinking", "reasoning.available"}:
            return True
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            marker = str(metadata.get("type") or metadata.get("kind") or metadata.get("reasoning") or "").strip().lower()
            if marker in {"thinking", "reasoning", "internal_thinking", "true"}:
                return True
            stack.append(metadata)
        content = item.get("content")
        if isinstance(content, list):
            stack.extend(child for child in content if isinstance(child, dict))
        elif isinstance(content, dict):
            stack.append(content)
    return False


def _cleanup_aops_logs(log_dir: Path, *, now: datetime, retention_days: int) -> None:
    cutoff = now.astimezone(timezone.utc).date() - timedelta(days=retention_days - 1)
    patterns = ("aops-*.log", "aops-wire-*.log", "aops-messages-*.log")
    for pattern in patterns:
        paths = list(log_dir.glob(pattern))
        for path in paths:
            raw_date = path.stem
            for prefix in ("aops-wire-", "aops-messages-", "aops-"):
                raw_date = raw_date.removeprefix(prefix)
            try:
                log_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if log_date < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass


def _write_aops_log_line(
    *,
    direction: str,
    action: str,
    payload: Any = None,
    data: dict[str, Any] | None = None,
    raw: Any = None,
    error: str | None = None,
    session_key: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    elapsed_ms: int | None = None,
    send_elapsed_ms: int | None = None,
    filtered: bool = False,
    config: PlatformConfig | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    log_dir = get_hermes_home() / "logs" / "aops"
    record_data = data if isinstance(data, dict) else {}
    raw_payload = raw if raw is not None else payload
    if not record_data:
        record_data = _aops_log_data_from_payload(raw_payload)
    metadata = record_data.get("metadata") if isinstance(record_data.get("metadata"), dict) else {}
    io = "recv" if direction in {"in", "recv"} else "send"
    event = _aops_log_event(action, raw_payload)
    message_type = _aops_log_message_type(record_data, action=action, filtered=filtered)
    silent = bool(record_data.get("silent") is True or str(record_data.get("messageType") or "").strip().lower() == "silent")
    text = _aops_log_text(record_data, action=action, error=error, filtered=filtered)
    status_value = status or ("failed" if error else ("skipped" if filtered else "ok"))
    line_parts = [
        _format_local(now),
        f"io={io}",
        f"event={event or '-'}",
        f"messageType={message_type or '-'}",
        f"silent={'true' if silent else 'false'}",
        f"channel={record_data.get('channelId') or record_data.get('conversationId') or '-'}",
        f"msg={record_data.get('messageId') or record_data.get('id') or '-'}",
        f"replyTo={record_data.get('replyToId') or '-'}",
        f"title={_quote_aops_log_value(record_data.get('title') or record_data.get('conversationTitle') or metadata.get('title') or metadata.get('conversationTitle'))}",
        f"text={_quote_aops_log_value(text)}",
        f"status={status_value}",
        f"elapsedMs={elapsed_ms if elapsed_ms is not None else '-'}",
        f"sendElapsedMs={send_elapsed_ms if send_elapsed_ms is not None else '-'}",
        f"raw={_aops_log_raw_json(raw_payload if raw_payload is not None else record_data)}",
    ]
    try:
        with _AOPS_LOG_LOCK:
            log_dir.mkdir(parents=True, exist_ok=True)
            _cleanup_aops_logs(log_dir, now=now, retention_days=_aops_log_retention_days(config))
            log_path = log_dir / f"aops-{now.date().isoformat()}.log"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(" ".join(line_parts) + "\n")
    except Exception as exc:
        logger.debug("AOPS log write failed: %s", exc)


@dataclass
class _ReplyContext:
    channel_id: str
    reply_to_id: Optional[str]
    run_id: Optional[str]
    title: Optional[str] = None


class AopsLiveReplyBridge:
    """Thread-safe bridge for AOPS native streaming replies."""

    def __init__(
        self,
        adapter: "AopsAdapter",
        *,
        chat_id: str,
        reply_to_id: Optional[str],
        run_id: Optional[str] = None,
        title: Optional[str] = None,
    ):
        self.adapter = adapter
        self.context = _ReplyContext(channel_id=str(chat_id), reply_to_id=reply_to_id, run_id=run_id, title=title)
        self._queue: queue.Queue = queue.Queue()
        self._message_id: Optional[str] = None
        self._seq = 0
        self._started = False
        self._terminal = False
        self._text = ""
        self._already_sent = False
        self._final_response_sent = False

    @property
    def already_sent(self) -> bool:
        return self._already_sent

    @property
    def final_response_sent(self) -> bool:
        return self._final_response_sent

    def bind_run_id(self, run_id: str | None) -> None:
        self.context.run_id = run_id

    def update_title(self, title: str | None) -> None:
        normalized = str(title or "").strip()
        if normalized:
            self.context.title = normalized

    def _reset_segment(self) -> None:
        self._message_id = None
        self._seq = 0
        self._started = False
        self._terminal = False
        self._text = ""

    def on_delta(self, text: Optional[str]) -> None:
        if text is None:
            self._queue.put(_SEGMENT_BREAK)
            return
        if text:
            self._queue.put(text)

    def on_segment_break(self) -> None:
        self._queue.put(_SEGMENT_BREAK)

    def on_commentary(self, text: str, *, already_streamed: bool = False) -> None:
        if not str(text or "").strip():
            return
        if already_streamed:
            self._queue.put(_SEGMENT_BREAK)
        self._queue.put((_COMMENTARY, text))

    def on_tool_progress(
        self,
        event_type: str,
        tool_name: str | None = None,
        preview: str | None = None,
        args: dict | None = None,
        **kwargs,
    ) -> None:
        payload = {
            "event_type": event_type,
            "tool_name": tool_name,
            "preview": preview,
            "args": args,
            **kwargs,
        }
        self._queue.put((_TOOL, payload))

    def send_final(self, text: str, *, conversation_ended: bool = True, content: Optional[list[dict[str, Any]]] = None) -> None:
        self._queue.put((_FINAL, {"text": text or "", "conversation_ended": conversation_ended, "content": content or []}))

    def send_error(self, message: str, *, conversation_ended: bool = True) -> None:
        self._queue.put((_ERROR, {"message": message or "Unknown error", "conversation_ended": conversation_ended}))

    def finish(self) -> None:
        self._queue.put(_DONE)

    async def _send_event(
        self,
        *,
        phase: str,
        kind: str,
        conversation_ended: bool,
        text: Optional[str] = None,
        delta: Optional[str] = None,
        tool: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
        content: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        if not self._message_id:
            self._message_id = self.adapter.create_message_id()
        self._seq += 1
        data = {
            "messageId": self._message_id,
            "seq": self._seq,
            "phase": phase,
            "kind": kind,
            "channelId": self.context.channel_id,
            "conversationEnded": conversation_ended,
            "ts": _now_ms(),
        }
        if self.context.reply_to_id:
            data["replyToId"] = self.context.reply_to_id
        if self.context.run_id:
            data["runId"] = self.context.run_id
        if self.context.title:
            data["title"] = self.context.title
        if delta is not None:
            data["delta"] = delta
        if text is not None:
            data["text"] = text
        if tool:
            data["tool"] = tool
        if error:
            data["error"] = error
        if content:
            data["content"] = content
        await self.adapter.send_reply_event(data)
        self._already_sent = True

    async def _ensure_started(self) -> None:
        if self._started or self._terminal:
            return
        await self._send_event(phase="start", kind="final", conversation_ended=False)
        self._started = True

    async def _close_segment(self, *, conversation_ended: bool) -> None:
        if not self._started or self._terminal:
            return
        await self._send_event(
            phase="end",
            kind="final",
            text=self._text,
            conversation_ended=conversation_ended,
        )
        self._terminal = True
        if conversation_ended:
            self._final_response_sent = True
        self._reset_segment()

    async def _emit_commentary(self, text: str) -> None:
        if self._started and not self._terminal:
            await self._close_segment(conversation_ended=False)
        await self._ensure_started()
        self._text = text
        await self._send_event(phase="delta", kind="final", delta=text, text=text, conversation_ended=False)
        await self._close_segment(conversation_ended=False)

    async def _emit_tool(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type") or "").strip()
        if event_type in ("reasoning.available", "_thinking"):
            raw = {"event": "message_reply", "data": payload}
            _write_aops_log_line(
                direction="out",
                action="message_reply",
                data=payload,
                raw=raw,
                filtered=True,
                status="skipped",
                config=self.adapter.config,
            )
            return
        tool_name = str(payload.get("tool_name") or "").strip() or None
        preview = str(payload.get("preview") or "").strip() or None
        is_error = bool(payload.get("is_error"))
        phase = "start"
        if event_type == "tool.completed":
            phase = "result"
        elif event_type == "tool.started":
            phase = "start"
        tool_text = preview
        if not tool_text:
            if phase == "start" and tool_name:
                tool_text = f"calling tool: {tool_name}"
        if not tool_name and not tool_text:
            return
        await self._ensure_started()
        tool_payload: dict[str, Any] = {"phase": phase}
        if tool_name:
            tool_payload["name"] = tool_name
        if tool_text:
            tool_payload["result"] = {"text": tool_text}
        if is_error:
            tool_payload["isError"] = True
        await self._send_event(
            phase="tool",
            kind="tool",
            text=tool_text,
            conversation_ended=False,
            tool=tool_payload,
        )

    async def run(self) -> None:
        while True:
            item = await asyncio.to_thread(self._queue.get)
            if item is _DONE:
                return
            if item is _SEGMENT_BREAK:
                if self._started and not self._terminal:
                    await self._close_segment(conversation_ended=False)
                continue
            if isinstance(item, tuple) and item and item[0] is _COMMENTARY:
                await self._emit_commentary(item[1])
                continue
            if isinstance(item, tuple) and item and item[0] is _TOOL:
                await self._emit_tool(item[1])
                continue
            if isinstance(item, tuple) and item and item[0] is _FINAL:
                payload = item[1]
                final_text = payload.get("text", "")
                await self._ensure_started()
                self._text = final_text
                await self._send_event(
                    phase="end",
                    kind="final",
                    text=final_text,
                    conversation_ended=bool(payload.get("conversation_ended", True)),
                    content=payload.get("content") or None,
                )
                self._terminal = True
                self._final_response_sent = True
                self._reset_segment()
                continue
            if isinstance(item, tuple) and item and item[0] is _ERROR:
                payload = item[1]
                await self._ensure_started()
                await self._send_event(
                    phase="error",
                    kind="final",
                    conversation_ended=bool(payload.get("conversation_ended", True)),
                    error={"message": payload.get("message", "Unknown error")},
                )
                self._terminal = True
                self._reset_segment()
                continue
            if isinstance(item, str):
                await self._ensure_started()
                previous = self._text
                self._text = previous + item
                await self._send_event(
                    phase="delta",
                    kind="final",
                    delta=item,
                    text=self._text,
                    conversation_ended=False,
                )


class AopsAdapter(BasePlatformAdapter):
    """Hermes AOPS adapter."""

    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = 12000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.AOPS)
        extra = config.extra or {}
        self._base_url = str(
            extra.get("base_url")
            or os.getenv("AOPS_BOT_URL", "").strip()
        ).rstrip("/")
        self._connect_timeout = _coerce_float(
            extra.get("connect_timeout")
            if extra.get("connect_timeout") is not None
            else os.getenv("AOPS_CONNECT_TIMEOUT"),
            default=_AOPS_CONNECT_TIMEOUT_SECS_DEFAULT,
            name="AOPS_CONNECT_TIMEOUT",
        )
        self._proxy_url = str(extra.get("proxy") or os.getenv("AOPS_PROXY", "")).strip() or resolve_proxy_url("AOPS_PROXY")
        self._push_tool_calls = bool(extra.get("push_tool_calls", True))
        self._dm_policy = str(extra.get("dm_policy") or os.getenv("AOPS_DM_POLICY", "open")).strip().lower() or "open"
        self._allow_from = _coerce_str_list(extra.get("allow_from") or os.getenv("AOPS_ALLOW_FROM"))
        self._trusted_agent_key_from = _coerce_str_list(
            extra.get("trusted_agent_key_from") or os.getenv("AOPS_TRUSTED_AGENT_KEY_FROM"),
            default=["*"],
        )
        self._agent_routes = extra.get("agent_routes") if isinstance(extra.get("agent_routes"), dict) else {}
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._dispatch_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._dispatch_worker_task: asyncio.Task | None = None
        self._connected = False
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None
        self._connected_event: asyncio.Event | None = None
        self._send_lock: asyncio.Lock | None = None
        self._channel_send_locks: dict[str, asyncio.Lock] = {}
        self._chat_cache: dict[str, dict[str, Any]] = {}
        self._seen_message_ids: set[str] = set()
        self._reply_flags_by_message_id: dict[str, dict[str, Any]] = {}
        self._conversation_titles: dict[str, str] = {}
        self._title_resolver = None
        self._bot_id: Optional[str] = None
        self._bot_name: Optional[str] = None
        try:
            self._ensure_loop_primitives()
        except RuntimeError:
            pass

    def _ensure_loop_primitives(self) -> None:
        """Keep asyncio primitives bound to the currently running loop.

        AOPS adapters can outlive a single event loop in tests and in some
        supervisor/reconnect paths. asyncio locks/events remember the loop
        once they have waiters; reusing them from another loop can crash ping
        pong or silent replies with "bound to a different event loop".
        """
        loop = asyncio.get_running_loop()
        if self._asyncio_loop is loop and self._connected_event is not None and self._send_lock is not None:
            return
        was_connected = self._connected
        self._asyncio_loop = loop
        self._connected_event = asyncio.Event()
        if was_connected:
            self._connected_event.set()
        self._send_lock = asyncio.Lock()
        self._channel_send_locks = {}

    def _connected_signal(self) -> asyncio.Event:
        self._ensure_loop_primitives()
        assert self._connected_event is not None
        return self._connected_event

    def _send_guard(self) -> asyncio.Lock:
        self._ensure_loop_primitives()
        assert self._send_lock is not None
        return self._send_lock

    def _channel_send_guard(self, channel_id: str) -> asyncio.Lock:
        self._ensure_loop_primitives()
        key = str(channel_id)
        lock = self._channel_send_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_send_locks[key] = lock
        return lock

    def _set_connected_signal(self, connected: bool) -> None:
        self._connected = connected
        try:
            event = self._connected_signal()
        except RuntimeError:
            return
        if connected:
            event.set()
        else:
            event.clear()

    @property
    def push_tool_calls(self) -> bool:
        return self._push_tool_calls

    @property
    def dm_policy(self) -> str:
        return self._dm_policy

    def set_title_resolver(self, resolver) -> None:
        self._title_resolver = resolver

    def _resolve_outbound_title(self, data: dict[str, Any]) -> str:
        title = str(data.get("title") or "").strip()
        if title:
            return title
        channel_id = str(data.get("channelId") or "").strip()
        cached_title = self._conversation_titles.get(channel_id, "")
        if cached_title:
            return cached_title
        resolver = self._title_resolver
        if callable(resolver):
            try:
                resolved = resolver(data)
                if resolved:
                    return str(resolved).strip()
            except Exception as exc:
                logger.debug("[%s] AOPS title resolver failed: %s", self.name, exc)
        return ""

    @property
    def allow_from(self) -> list[str]:
        return list(self._allow_from)

    @property
    def trusted_agent_key_from(self) -> list[str]:
        return list(self._trusted_agent_key_from)

    @property
    def agent_routes(self) -> dict[str, Any]:
        return dict(self._agent_routes)

    def create_message_id(self) -> str:
        return f"botmsg-{uuid.uuid4().hex}"

    def _headers(self) -> dict[str, str]:
        token = str(self.config.token or "").strip()
        return {
            "Authorization": f"Bearer {token}",
            "tec-client-ip": _resolve_aops_client_id(self.config),
        }

    def _request_kwargs(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return proxy_kwargs_for_aiohttp(self._proxy_url)

    def _log_wire(
        self,
        level: str,
        *,
        direction: str,
        action: str,
        payload: Any = None,
        error: str | None = None,
    ) -> None:
        _write_aops_log_line(
            direction=direction,
            action=action,
            payload=payload,
            error=error,
            config=self.config,
        )

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("aops_missing_dependency", "AOPS startup failed: aiohttp not installed", retryable=True)
            return False
        if not self.config.token or not self._base_url:
            self._set_fatal_error(
                "aops_missing_config",
                "AOPS startup failed: AOPS_BOT_TOKEN and AOPS_BOT_URL are required",
                retryable=True,
            )
            return False
        try:
            await self._open_connection()
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            return True
        except Exception as exc:
            self._set_fatal_error("aops_connect_error", f"AOPS startup failed: {exc}", retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._set_connected_signal(False)
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._cancel_dispatch_tasks()
        await self._stop_dispatch_worker()
        await self._cleanup()
        self._mark_disconnected()

    async def _cancel_dispatch_tasks(self) -> None:
        tasks = [task for task in self._dispatch_tasks if not task.done()]
        if not tasks:
            self._dispatch_tasks.clear()
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_tasks.difference_update(tasks)

    async def _cleanup(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _open_connection(self) -> None:
        await self._cleanup()
        session_kwargs, request_kwargs = self._request_kwargs()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._connect_timeout or None),
            trust_env=True,
            **session_kwargs,
        )
        timeout_label = f"{self._connect_timeout:g}"
        logger.info("[%s] Opening AOPS connection (timeout=%ss, base_url=%s)", self.name, timeout_label, self._base_url)
        bot_info = await self._fetch_bot_me(request_kwargs=request_kwargs)
        self._bot_id = str(bot_info.get("id") or "").strip() or None
        self._bot_name = str(bot_info.get("name") or "").strip() or None
        ws = await self._session.ws_connect(
            _build_ws_url(self._base_url),
            headers=self._headers(),
            heartbeat=30,
            **request_kwargs,
        )
        self._ws = ws
        await self._ws.send_json({"action": "auth", "token": self.config.token})
        self._log_wire(
            "info",
            direction="out",
            action="ws.auth_sent",
            payload={"event": "auth", "authSent": True},
        )
        self._set_connected_signal(True)
        await self._report_agents(request_kwargs=request_kwargs)
        logger.info("[%s] Connected to %s as %s", self.name, self._base_url, self._bot_id or "unknown")
        self._log_wire(
            "info",
            direction="state",
            action="ws.connected",
            payload={"event": "connected", "baseUrl": self._base_url, "botId": self._bot_id or "unknown"},
        )

    async def _fetch_bot_me(self, *, request_kwargs: dict[str, Any]) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("AOPS session not initialized")
        url = urljoin(f"{self._base_url}/", "api/v1/bot/me")
        self._log_wire("info", direction="out", action="http.bot_me.request", payload={"method": "GET", "url": url})
        async with self._session.get(url, headers=self._headers(), **request_kwargs) as resp:
            if resp.status >= 400:
                body = await resp.text()
                self._log_wire(
                    "warning",
                    direction="in",
                    action="http.bot_me.response",
                    payload={"method": "GET", "url": url, "status": resp.status, "body": body[:500]},
                    error=f"status={resp.status}",
                )
                raise RuntimeError(f"/bot/me failed ({resp.status}): {body[:200]}")
            payload = await resp.json()
            self._log_wire("info", direction="in", action="http.bot_me.response", payload={"method": "GET", "url": url, "status": resp.status, "body": payload})
            return payload

    def _build_agent_report_payload(self) -> dict[str, Any]:
        agents = []
        default_agent_id = "main"
        routes = self._agent_routes if isinstance(self._agent_routes, dict) else {}
        if routes:
            for route_id, route in routes.items():
                route_cfg = route if isinstance(route, dict) else {}
                agent = {
                    "id": str(route_id),
                    "enabled": bool(route_cfg.get("enabled", True)),
                    "default": bool(route_cfg.get("default", False)),
                    "workspace": str(route_cfg.get("workspace") or "~/.hermes"),
                }
                if agent["default"]:
                    default_agent_id = agent["id"]
                agents.append(agent)
        if not agents:
            agents = [{
                "id": "main",
                "enabled": True,
                "default": True,
                "workspace": "~/.hermes",
            }]
        if not any(agent.get("default") for agent in agents):
            agents[0]["default"] = True
            default_agent_id = agents[0]["id"]
        return {
            "botId": self._bot_id,
            "reportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "hermes",
            "agents": agents,
            "defaultAgentId": default_agent_id,
            "runtime": _build_aops_runtime_report(),
        }

    async def _report_agents(self, *, request_kwargs: dict[str, Any]) -> None:
        if not self._session or not self._bot_id:
            return
        url = urljoin(f"{self._base_url}/", "api/v1/bot/agents/report")
        payload = self._build_agent_report_payload()
        try:
            log_payload = _redact_aops_agent_report_for_log({"method": "POST", "url": url, "body": payload})
            self._log_wire("info", direction="out", action="http.agent_report.request", payload=log_payload)
            async with self._session.post(url, headers={**self._headers(), "Content-Type": "application/json"}, json=payload, **request_kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    self._log_wire(
                        "warning",
                        direction="in",
                        action="http.agent_report.response",
                        payload={"method": "POST", "url": url, "status": resp.status, "body": body[:500]},
                        error=f"status={resp.status}",
                    )
                    logger.warning("[%s] agent report failed (%s): %s", self.name, resp.status, body[:200])
                    return
                body = None
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
                self._log_wire("info", direction="in", action="http.agent_report.response", payload={"method": "POST", "url": url, "status": resp.status, "body": body[:500]})
        except Exception as exc:
            self._log_wire("warning", direction="in", action="http.agent_report.response", payload={"method": "POST", "url": url}, error=str(exc))
            logger.warning("[%s] agent report failed: %s", self.name, exc)

    async def _listen_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._read_events()
                attempt = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                self._set_connected_signal(False)
                logger.warning("[%s] AOPS socket error: %s", self.name, exc)
                delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                try:
                    await self._open_connection()
                    attempt = 0
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)

    async def _read_events(self) -> None:
        if not self._ws:
            raise RuntimeError("AOPS websocket not connected")
        self._ensure_dispatch_worker()
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    if payload.get("event") != "message_posted":
                        self._log_wire("info", direction="in", action="ws.receive", payload=payload)
                    if await self._handle_ws_control_event(payload):
                        continue
                    if payload.get("event") == "message_posted":
                        self._schedule_dispatch_payload(payload)
                    else:
                        await self._dispatch_payload(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                close_code = getattr(self._ws, "close_code", None)
                close_error = None
                try:
                    close_error = self._ws.exception() if self._ws else None
                except Exception:
                    close_error = None
                self._log_wire(
                    "warning",
                    direction="in",
                    action="ws.closed",
                    payload={
                        "event": "ws.closed",
                        "type": str(msg.type),
                        "data": getattr(msg, "data", None),
                        "extra": getattr(msg, "extra", None),
                        "closeCode": close_code or getattr(msg, "data", None),
                        "closeError": str(close_error or ""),
                    },
                )
                raise RuntimeError("AOPS websocket closed")

    def _schedule_dispatch_payload(self, payload: dict[str, Any]) -> None:
        self._ensure_dispatch_worker()
        assert self._dispatch_queue is not None
        self._dispatch_queue.put_nowait(payload)

    def _ensure_dispatch_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._dispatch_worker_task and not self._dispatch_worker_task.done():
            try:
                if self._dispatch_worker_task.get_loop() is loop:
                    return
            except Exception:
                pass
            self._dispatch_worker_task.cancel()
            self._dispatch_tasks.discard(self._dispatch_worker_task)
            self._dispatch_worker_task = None
            self._dispatch_queue = None
        if self._dispatch_worker_task and not self._dispatch_worker_task.done():
            return
        self._dispatch_queue = asyncio.Queue()
        self._dispatch_worker_task = asyncio.create_task(self._dispatch_worker())
        self._dispatch_tasks.add(self._dispatch_worker_task)

        def _done(done_task: asyncio.Task) -> None:
            self._dispatch_tasks.discard(done_task)
            if self._dispatch_worker_task is done_task:
                self._dispatch_worker_task = None
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[%s] AOPS message dispatch worker failed: %s", self.name, exc, exc_info=True)

        self._dispatch_worker_task.add_done_callback(_done)

    async def _dispatch_worker(self) -> None:
        assert self._dispatch_queue is not None
        while True:
            payload = await self._dispatch_queue.get()
            if payload is None:
                self._dispatch_queue.task_done()
                return
            try:
                await self._dispatch_payload(payload)
            except asyncio.CancelledError:
                self._dispatch_queue.task_done()
                raise
            except Exception as exc:
                logger.warning("[%s] AOPS message dispatch failed: %s", self.name, exc, exc_info=True)
            finally:
                if payload is not None:
                    self._dispatch_queue.task_done()

    async def _stop_dispatch_worker(self) -> None:
        task = self._dispatch_worker_task
        queue = self._dispatch_queue
        if not task:
            return
        if queue is not None:
            queue.put_nowait(None)
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._dispatch_worker_task = None
            self._dispatch_queue = None

    async def _handle_ws_control_event(self, payload: dict[str, Any]) -> bool:
        event = str(payload.get("event") or "").strip().lower()
        action = str(payload.get("action") or "").strip().lower()
        if event != "ping" and action != "ping":
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        pong = {
            "action" if action == "ping" and event != "ping" else "event": "pong",
            "data": {
                **data,
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        }
        async with self._send_guard():
            if not self._ws or self._ws.closed:
                raise RuntimeError("AOPS websocket not connected")
            await self._ws.send_json(pong)
        self._log_wire("info", direction="out", action="pong", payload=pong)
        return True

    def _parse_json(self, raw: str) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("[%s] Ignoring non-JSON frame", self.name)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async def _dispatch_payload(self, payload: dict[str, Any]) -> None:
        data = self._normalize_inbound_message(payload)
        if data is None:
            return
        event = self._build_message_event(data)
        if event is None:
            return
        log_data = dict(data)
        log_data["text"] = event.text
        _write_aops_log_line(
            direction="in",
            action="in.command" if str(event.text or "").strip().startswith("/") else "in.message",
            data=log_data,
            raw=payload,
            session_key=self._session_key_for_event(event),
            config=self.config,
        )
        if self._inbound_is_silent(data):
            await self._dispatch_silent_event(event)
            return
        await self._attach_inbound_attachments(event)
        await self.handle_message(event)

    async def _dispatch_silent_event(self, event: MessageEvent) -> None:
        started = time.monotonic()
        response: Any = None
        try:
            from gateway import aops_commands as _aops_commands

            command = (event.get_command() or "").strip().lower().replace("_", "-")
            if command == "help":
                response = _aops_commands.help_tree_response(self.config, event.text.strip() or "/help")
            elif command == "commands":
                lines = ["🧰 **AOPS Local Commands**", *_aops_commands.aops_text_command_lines(), ""]
                skill_entries = _aops_commands.aops_skill_command_lines(self.config)
                if skill_entries:
                    lines.extend(["⚡ **Skill Commands**:", *skill_entries, ""])
                response = "\n".join(lines).strip()
            else:
                response = await asyncio.wait_for(
                    asyncio.to_thread(_aops_commands.maybe_local_command, event),
                    timeout=_aops_local_command_exec_timeout(),
                )

        except asyncio.TimeoutError:
            response = json.dumps(
                {
                    "schemaVersion": "local-command-list.v1",
                    "type": "silent.error",
                    "ok": False,
                    "command": event.text,
                    "error": {"code": "SILENT_COMMAND_TIMEOUT", "message": "Silent local command timed out."},
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            response = json.dumps(
                {
                    "schemaVersion": "local-command-list.v1",
                    "type": "silent.error",
                    "ok": False,
                    "command": event.text,
                    "error": {"code": "SILENT_COMMAND_FAILED", "message": str(exc)},
                },
                ensure_ascii=False,
                indent=2,
            )
        if response is None:
            response = json.dumps(
                {
                    "schemaVersion": "local-command-list.v1",
                    "type": "silent.error",
                    "ok": False,
                    "command": event.text,
                    "error": {"code": "SILENT_COMMAND_UNSUPPORTED", "message": "Unsupported silent command."},
                },
                ensure_ascii=False,
                indent=2,
            )
        content = None
        metadata = None
        if hasattr(response, "text") and hasattr(response, "content"):
            content = getattr(response, "content", None)
            metadata = getattr(response, "metadata", None)
            response = getattr(response, "text", "") or ""
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        message_id = self.create_message_id()
        data = {
            "messageId": message_id,
            "seq": 1,
            "phase": "end",
            "kind": "final",
            "channelId": event.source.chat_id if event.source else "",
            "replyToId": event.message_id,
            "text": str(response or ""),
            "conversationEnded": True,
            "ts": _now_ms(),
            "messageType": "silent",
            "silent": True,
            "title": str(raw.get("title") or raw.get("conversationTitle") or ""),
            "botReplyExtra": {"messageType": "silent"},
        }
        if isinstance(metadata, dict):
            data["contentMetadata"] = metadata
        if content:
            data["content"] = content
        await self.send_reply_event(data, send_timeout=_aops_local_command_send_timeout(), elapsed_ms=int((time.monotonic() - started) * 1000))

    def _session_key_for_event(self, event: MessageEvent) -> str | None:
        try:
            from gateway.session import build_session_key

            return build_session_key(event.source) if event.source else None
        except Exception:
            return None

    @staticmethod
    def _metadata_from_message(data: dict[str, Any]) -> dict[str, Any]:
        metadata = data.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        return metadata if isinstance(metadata, dict) else {}

    @classmethod
    def _normalize_wire_message_data(cls, data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data)
        metadata = cls._metadata_from_message(normalized)
        if metadata is not data.get("metadata"):
            normalized["metadata"] = metadata
        message_type = normalized.get("messageType") or metadata.get("messageType")
        if message_type is not None:
            normalized["messageType"] = message_type
        if isinstance(metadata.get("silent"), bool):
            normalized["silent"] = bool(metadata["silent"])
        elif isinstance(normalized.get("silent"), bool):
            normalized["silent"] = bool(normalized["silent"])
        return normalized

    @classmethod
    def _inbound_is_silent(cls, data: dict[str, Any]) -> bool:
        normalized = cls._normalize_wire_message_data(data)
        return normalized.get("silent") is True or str(normalized.get("messageType") or "").strip().lower() == "silent"

    def _normalize_inbound_message(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        if payload.get("event") == "message_posted":
            data = payload.get("data")
            return self._normalize_wire_message_data(data) if isinstance(data, dict) else None

        envelope_type = str(payload.get("type") or "").strip().lower()
        envelope_event = str(payload.get("event") or "").strip().lower()
        raw_data = payload.get("data")
        if not isinstance(raw_data, dict):
            return None

        message_data: Optional[dict[str, Any]] = None
        if envelope_type == "user_message":
            message_data = raw_data
        elif envelope_event == "message_created":
            nested = raw_data.get("message")
            if isinstance(nested, dict):
                message_data = nested

        if not isinstance(message_data, dict):
            return None
        if str(message_data.get("role") or "").strip().lower() != "user":
            return None

        metadata = message_data.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

        bot_id = str(message_data.get("botId") or metadata.get("botId") or "").strip()
        if self._bot_id and bot_id and bot_id != self._bot_id:
            return None

        message_id = str(message_data.get("id") or "").strip()
        if not message_id:
            return None
        if message_id in self._seen_message_ids:
            return None
        self._seen_message_ids.add(message_id)

        user_id = (
            str(metadata.get("userId") or "").strip()
            or str(message_data.get("ownerUserId") or "").strip()
        )
        channel_id = str(message_data.get("conversationId") or "").strip()
        raw_text = str(message_data.get("content") or "").strip()
        if not user_id or not channel_id or not raw_text:
            return None

        agent_key = str(
            message_data.get("agentId")
            or metadata.get("agentId")
            or ""
        ).strip()
        if agent_key == "main":
            agent_key = ""

        text, text_was_messages, system_prompt = _extract_aops_text(message_data.get("content"))
        if text_was_messages:
            metadata = {**metadata, "aopsRawTextWasMessages": True}
            if system_prompt:
                metadata = {**metadata, "aopsMessagesSystemPrompt": system_prompt}
        return {
            "id": message_id,
            "userId": user_id,
            "userName": str(metadata.get("userName") or "").strip() or None,
            "text": text,
            "channelId": channel_id,
            "channelType": "direct",
            "timestamp": message_data.get("createdAt") or message_data.get("updatedAt"),
            "agentKey": agent_key or None,
            "model": message_data.get("model"),
            "metadata": metadata,
            "attachments": message_data.get("attachments"),
            "messageType": message_data.get("messageType") or metadata.get("messageType"),
            "title": message_data.get("title") or message_data.get("conversationTitle") or metadata.get("title") or metadata.get("conversationTitle"),
            "silent": (
                bool(metadata["silent"])
                if isinstance(metadata.get("silent"), bool)
                else bool(message_data["silent"])
                if isinstance(message_data.get("silent"), bool)
                else None
            ),
        }

    def _build_message_event(self, data: dict[str, Any]) -> Optional[MessageEvent]:
        message_id = str(data.get("id") or "").strip()
        user_id = str(data.get("userId") or "").strip()
        channel_id = str(data.get("channelId") or "").strip()
        if not message_id or not user_id or not channel_id:
            return None
        if self._bot_id and user_id == self._bot_id:
            return None
        channel_type = str(data.get("channelType") or "direct").strip().lower()
        chat_type = "dm" if channel_type == "direct" else "group"
        user_name = str(data.get("userName") or "").strip() or None
        chat_name = user_name if chat_type == "dm" else channel_id
        source = self.build_source(
            chat_id=channel_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        route_overrides = None
        channel_prompt = None
        agent_key = str(data.get("agentKey") or "").strip()
        if agent_key and _entry_matches(self._trusted_agent_key_from, user_id):
            route = self._agent_routes.get(agent_key)
            if isinstance(route, dict):
                allowed_fields = ("model", "provider", "api_mode", "command", "args", "credential_pool")
                route_overrides = {key: route[key] for key in allowed_fields if key in route}
                prompt = str(route.get("prompt") or "").strip()
                if prompt:
                    channel_prompt = prompt
            else:
                logger.debug("[%s] Unknown AOPS agentKey ignored: %s", self.name, agent_key)
        self._chat_cache[channel_id] = {
            "id": channel_id,
            "name": chat_name or channel_id,
            "type": chat_type,
        }
        conversation_title = _extract_aops_conversation_title(data)
        if conversation_title:
            self._conversation_titles[channel_id] = conversation_title
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                metadata = _metadata_dict(metadata)
            data = {**data, "metadata": {**metadata, "conversationTitle": conversation_title}}
        if isinstance(data.get("silent"), bool):
            self._reply_flags_by_message_id[message_id] = {"silent": bool(data["silent"])}
        approval_command, approval_id = _extract_aops_approval_action(data)
        text, text_was_messages, system_prompt = _extract_aops_text(data.get("text"))
        if approval_command:
            text = approval_command
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                metadata = _metadata_dict(metadata)
            metadata = {**metadata, "aopsApprovalAction": True}
            if approval_id:
                metadata["aopsApprovalId"] = approval_id
            data = {**data, "metadata": metadata}
            text_was_messages = False
        if text_was_messages:
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata = {**metadata, "aopsRawTextWasMessages": True}
            if system_prompt:
                metadata = {**metadata, "aopsMessagesSystemPrompt": system_prompt}
                channel_prompt = (channel_prompt + "\n\n" + system_prompt).strip() if channel_prompt else system_prompt
            data = {**data, "metadata": metadata}
        elif isinstance((data.get("metadata") or {}), dict):
            system_prompt = str((data.get("metadata") or {}).get("aopsMessagesSystemPrompt") or "").strip()
            if system_prompt:
                channel_prompt = (channel_prompt + "\n\n" + system_prompt).strip() if channel_prompt else system_prompt
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=message_id,
            timestamp=_iso_to_datetime(data.get("timestamp")),
            channel_prompt=channel_prompt,
            route_overrides=route_overrides,
        )

    async def _attach_inbound_attachments(self, event: MessageEvent) -> None:
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        if self._inbound_is_silent(raw):
            return
        attachments = raw.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return

        media_paths: list[str] = []
        media_types: list[str] = []
        failures: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            file_id = str(attachment.get("fileId") or attachment.get("file_id") or "").strip()
            if not file_id:
                continue
            attachment_log = self._attachment_log_payload(event, attachment)
            try:
                cached_path, media_type = await self._download_and_cache_attachment(attachment)
            except Exception as exc:
                logger.warning("[%s] Failed to download AOPS attachment %s: %s", self.name, file_id, exc)
                failures.append(str(attachment.get("fileName") or attachment.get("file_name") or file_id))
                continue
            if cached_path:
                media_paths.append(cached_path)
                media_types.append(media_type)

        if media_paths:
            event.media_urls.extend(media_paths)
            event.media_types.extend(media_types)
            event.message_type = _message_type_for_aops_media(media_types)
        if failures:
            failed = "、".join(failures[:3])
            suffix = "等" if len(failures) > 3 else ""
            notice = f"[系统提示：收到附件，但 {failed}{suffix} 下载失败或内容不可用，无法识别对应图片/文件。]"
            event.text = f"{event.text.rstrip()}\n\n{notice}" if event.text.strip() else notice

    def _attachment_log_payload(self, event: MessageEvent, attachment: dict[str, Any]) -> dict[str, Any]:
        return {
            "messageId": getattr(event, "message_id", None),
            "fileId": attachment.get("fileId") or attachment.get("file_id"),
            "fileName": attachment.get("fileName") or attachment.get("file_name"),
            "mimeType": attachment.get("mimeType") or attachment.get("mime_type"),
            "fileType": attachment.get("fileType") or attachment.get("file_type"),
            "downloadUrl": self._attachment_download_url(attachment),
        }

    def _attachment_download_url(self, attachment: dict[str, Any]) -> str:
        raw_url = str(attachment.get("downloadUrl") or attachment.get("download_url") or "").strip()
        if raw_url:
            return urljoin(f"{self._base_url}/", raw_url.lstrip("/"))
        file_id = str(attachment.get("fileId") or attachment.get("file_id") or "").strip()
        return urljoin(f"{self._base_url}/", f"api/v1/attachments/{quote(file_id, safe='')}/download")

    async def _download_attachment_bytes(self, attachment: dict[str, Any]) -> tuple[bytes, str]:
        if not self._session:
            raise RuntimeError("AOPS session not initialized")
        session_kwargs, request_kwargs = self._request_kwargs()
        del session_kwargs
        url = self._attachment_download_url(attachment)
        self._log_wire("info", direction="out", action="http.attachment.request", payload={"method": "GET", "url": url, "fileId": attachment.get("fileId") or attachment.get("file_id")})
        async with self._session.get(url, headers=self._headers(), **request_kwargs) as resp:
            if resp.status >= 400:
                body = await resp.text()
                self._log_wire(
                    "warning",
                    direction="in",
                    action="http.attachment.response",
                    payload={"method": "GET", "url": url, "status": resp.status, "body": body[:500]},
                    error=f"status={resp.status}",
                )
                raise RuntimeError(f"attachment download failed ({resp.status}): {body[:200]}")
            raw_size = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            try:
                if raw_size and int(raw_size) > _AOPS_MAX_ATTACHMENT_BYTES:
                    raise RuntimeError(f"attachment too large: {raw_size} bytes")
            except ValueError:
                pass
            data = await resp.read()
            if len(data) > _AOPS_MAX_ATTACHMENT_BYTES:
                raise RuntimeError(f"attachment too large: {len(data)} bytes")
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip() if hasattr(resp, "headers") else ""
            self._log_wire(
                "info",
                direction="in",
                action="http.attachment.response",
                payload={"method": "GET", "url": url, "status": resp.status, "contentType": content_type, "bytes": len(data)},
            )
            return data, content_type

    async def _download_and_cache_attachment(self, attachment: dict[str, Any]) -> tuple[str, str]:
        data, response_mime = await self._download_attachment_bytes(attachment)
        file_name = str(attachment.get("fileName") or attachment.get("file_name") or attachment.get("fileId") or "aops_attachment")
        declared_mime = str(attachment.get("mimeType") or attachment.get("mime_type") or "").strip()
        media_type = response_mime or declared_mime or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        file_type = _normalize_aops_attachment_type({**attachment, "mimeType": media_type})
        ext = _extension_for_aops_attachment(file_name, media_type, file_type)

        if file_type == "image":
            return cache_image_from_bytes(data, ext), media_type
        if file_type == "audio":
            return cache_audio_from_bytes(data, ext), media_type
        if file_type == "video":
            return cache_video_from_bytes(data, ext), media_type
        return cache_document_from_bytes(data, file_name), media_type

    async def _send_payload(self, payload: dict[str, Any], *, channel_id: str, timeout: float = 15.0) -> SendResult:
        try:
            await asyncio.wait_for(self._connected_signal().wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="AOPS websocket is not connected", retryable=True)
        async with self._channel_send_guard(str(channel_id)):
            async with self._send_guard():
                if not self._ws or self._ws.closed:
                    return SendResult(success=False, error="AOPS websocket is not connected", retryable=True)
                await self._ws.send_json(payload)
        return SendResult(success=True, message_id=((payload.get("data") or {}).get("messageId")))

    async def send_reply_event(
        self,
        data: dict[str, Any],
        *,
        send_timeout: float = 15.0,
        elapsed_ms: int | None = None,
    ) -> SendResult:
        send_started = time.monotonic()
        reply_to_id = str(data.get("replyToId") or "").strip()
        if "silent" not in data and reply_to_id:
            reply_flags = self._reply_flags_by_message_id.get(reply_to_id) or {}
            if isinstance(reply_flags.get("silent"), bool):
                data = {**data, "silent": reply_flags["silent"]}
        title = self._resolve_outbound_title(data)
        data = {**data, "title": title}
        if "messageType" not in data:
            data = {
                **data,
                "messageType": (
                    "cron"
                    if str(data.get("messageType") or "").strip().lower() == "cron"
                    else ("silent" if data.get("silent") is True else "common")
                ),
            }
        data = _aops_normalize_outbound_protocol_fields(data)
        payload = {"event": "message_reply", "data": data}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if _aops_is_internal_thinking_payload(data):
            _write_aops_log_line(
                direction="out",
                action="message_reply",
                data=data,
                raw=payload,
                session_key=metadata.get("sessionKey"),
                session_id=data.get("runId") or metadata.get("sessionId"),
                filtered=True,
                status="skipped",
                config=self.config,
            )
            return SendResult(success=True, message_id=str(data.get("messageId") or ""))
        result = await self._send_payload(payload, channel_id=str(data.get("channelId") or ""), timeout=send_timeout)
        send_elapsed_ms = int((time.monotonic() - send_started) * 1000)
        _write_aops_log_line(
            direction="out",
            action="message_reply",
            data=data,
            raw=payload,
            session_key=metadata.get("sessionKey"),
            session_id=data.get("runId") or metadata.get("sessionId"),
            status="ok" if result.success else "failed",
            error=None if result.success else result.error,
            elapsed_ms=elapsed_ms,
            send_elapsed_ms=send_elapsed_ms,
            config=self.config,
        )
        return result

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        reply_to = reply_to or metadata.get("reply_to")
        inherited_silent = None
        if reply_to:
            reply_flags = self._reply_flags_by_message_id.get(str(reply_to)) or {}
            if isinstance(reply_flags.get("silent"), bool):
                inherited_silent = reply_flags["silent"]
        message_id = str(metadata.get("message_id") or self.create_message_id())
        run_id = metadata.get("run_id")
        kind = str(metadata.get("kind") or "final")
        message_type = _aops_message_type(metadata=metadata, inherited_silent=inherited_silent)
        outbound_extra = _aops_outbound_extra_fields(metadata, message_type=message_type)
        title = self._resolve_outbound_title(
            {
                "title": metadata.get("title"),
                "channelId": chat_id,
                "runId": run_id,
                "sessionId": metadata.get("sessionId"),
                "sessionKey": metadata.get("sessionKey"),
            }
        )
        start_payload = {
            "messageId": message_id,
            "seq": 1,
            "phase": "start",
            "kind": kind,
            "channelId": chat_id,
            "conversationEnded": False,
            "ts": _now_ms(),
            "messageType": message_type,
            "title": title,
        }
        if outbound_extra:
            start_payload.update(outbound_extra)
        if reply_to:
            start_payload["replyToId"] = reply_to
        if run_id:
            start_payload["runId"] = run_id
        if inherited_silent is not None:
            start_payload["silent"] = inherited_silent
        start = await self.send_reply_event(start_payload)
        if not start.success:
            return start
        end_payload = {
            "messageId": message_id,
            "seq": 2,
            "phase": "end",
            "kind": kind,
            "channelId": chat_id,
            "text": content,
            "conversationEnded": bool(metadata.get("conversation_ended", True)),
            "ts": _now_ms(),
            "messageType": message_type,
            "title": title,
        }
        if outbound_extra:
            end_payload.update(outbound_extra)
        if reply_to:
            end_payload["replyToId"] = reply_to
        if run_id:
            end_payload["runId"] = run_id
        if inherited_silent is not None:
            end_payload["silent"] = inherited_silent
        if metadata.get("content"):
            end_payload["content"] = metadata["content"]
        result = await self.send_reply_event(end_payload)
        if result.success:
            self._chat_cache.setdefault(str(chat_id), {"id": str(chat_id), "name": str(chat_id), "type": "dm"})
            result.message_id = message_id
        return result

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        approval_id = f"exec-approval-{uuid.uuid4().hex[:12]}"
        expires_at_ms = _now_ms() + self._approval_timeout_ms()
        message_id = str(metadata.get("message_id") or self.create_message_id())
        allow_permanent = bool(metadata.get("allow_permanent", True))
        allowed_actions = (
            [
                {"command": "/approve", "display": "仅本次允许"},
                {"command": "/approve always", "display": "始终允许"},
                {"command": "/deny", "display": "拒绝"},
            ]
            if allow_permanent
            else [
                {"command": "/approve", "display": "仅本次允许"},
                {"command": "/approve session", "display": "本会话允许"},
                {"command": "/deny", "display": "拒绝"},
            ]
        )
        inherited_silent = None
        reply_to = metadata.get("reply_to")
        if reply_to:
            reply_flags = self._reply_flags_by_message_id.get(str(reply_to)) or {}
            if isinstance(reply_flags.get("silent"), bool):
                inherited_silent = reply_flags["silent"]
        command_preview = command[:500] + "..." if len(command) > 500 else command
        action_hint = "仅本次允许、始终允许同类操作，或拒绝执行" if allow_permanent else "仅本次允许、本会话允许，或拒绝执行"
        message = (
            "检测到需要审批的高风险操作。\n\n"
            f"风险说明：{description or '危险命令'}\n\n"
            f"待执行命令：\n```sh\n{command_preview}\n```\n\n"
            f"请选择：{action_hint}。"
        )
        payload = {
            "messageId": message_id,
            "seq": 1,
            "phase": "actions",
            "kind": "approval",
            "channelId": chat_id,
            "text": "",
            "conversationEnded": True,
            "ts": _now_ms(),
            "messageType": _aops_message_type(metadata=metadata, inherited_silent=inherited_silent),
            "content": [{
                "type": "approval",
                "id": approval_id,
                "approvalKind": "exec",
                "allowedActions": allowed_actions,
                "expiresAtMs": expires_at_ms,
                "message": message,
                "replyContent": message,
                "sessionKey": session_key,
                "command": command,
                "description": description,
                "allowPermanent": allow_permanent,
            }],
        }
        if reply_to:
            payload["replyToId"] = reply_to
        if metadata.get("run_id"):
            payload["runId"] = metadata["run_id"]
        if inherited_silent is not None:
            payload["silent"] = inherited_silent
        result = await self.send_reply_event(payload)
        if result.success:
            result.message_id = message_id
        return result

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        message_id = str(metadata.get("message_id") or self.create_message_id())
        inherited_silent = None
        reply_to = metadata.get("reply_to")
        if reply_to:
            reply_flags = self._reply_flags_by_message_id.get(str(reply_to)) or {}
            if isinstance(reply_flags.get("silent"), bool):
                inherited_silent = reply_flags["silent"]
        payload = {
            "messageId": message_id,
            "seq": 1,
            "phase": "actions",
            "kind": "approval",
            "channelId": chat_id,
            "text": "",
            "conversationEnded": True,
            "ts": _now_ms(),
            "messageType": _aops_message_type(metadata=metadata, inherited_silent=inherited_silent),
            "content": [{
                "type": "approval",
                "id": confirm_id,
                "approvalKind": "slash",
                "allowedActions": [
                    {"command": "/approve", "display": "执行本次"},
                    {"command": "/always", "display": "始终执行"},
                    {"command": "/cancel", "display": "取消"},
                ],
                "message": message,
                "replyContent": message,
                "sessionKey": session_key,
                "command": title,
                "description": "slash command confirmation",
            }],
        }
        if reply_to:
            payload["replyToId"] = reply_to
        if metadata.get("run_id"):
            payload["runId"] = metadata["run_id"]
        if inherited_silent is not None:
            payload["silent"] = inherited_silent
        result = await self.send_reply_event(payload)
        if result.success:
            result.message_id = message_id
        return result

    def _approval_timeout_ms(self) -> int:
        try:
            from tools.approval import _get_approval_config
            timeout_seconds = int(_get_approval_config().get("gateway_timeout", 300))
        except Exception:
            timeout_seconds = 300
        return max(timeout_seconds, 1) * 1000

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return SendResult(success=False, error="AOPS does not support native image delivery")

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return SendResult(success=False, error="AOPS does not support native document delivery")

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return SendResult(success=False, error="AOPS does not support native voice delivery")

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return SendResult(success=False, error="AOPS does not support native video delivery")

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        return SendResult(success=False, error="AOPS does not support native image delivery")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return dict(self._chat_cache.get(str(chat_id), {"id": str(chat_id), "name": str(chat_id), "type": "dm"}))
