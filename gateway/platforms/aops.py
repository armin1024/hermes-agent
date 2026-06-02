"""AOPS gateway adapter and native streaming bridge."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import mimetypes
import os
import queue
import threading
import time
import uuid
from collections import defaultdict
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
_AOPS_WIRE_LOG_RETENTION_DAYS = 7
_AOPS_WIRE_LOG_LOCK = threading.Lock()
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


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_local(dt: datetime) -> str:
    local_dt = dt.astimezone()
    tz_label = os.getenv("HERMES_TIMEZONE", "").strip() or local_dt.tzname() or local_dt.strftime("%z")
    return f"{local_dt.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}".strip()


def _extract_aops_wire_fields(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    data_record = data if isinstance(data, dict) else {}
    return {
        "event": payload.get("event"),
        "messageId": data_record.get("messageId") or data_record.get("id"),
        "seq": data_record.get("seq"),
        "phase": data_record.get("phase"),
        "kind": data_record.get("kind"),
        "channelId": data_record.get("channelId") or data_record.get("conversationId"),
        "replyToId": data_record.get("replyToId"),
        "runId": data_record.get("runId"),
        "userId": data_record.get("userId") or data_record.get("ownerUserId"),
        "agentKey": data_record.get("agentKey") or data_record.get("agentId"),
    }


def _cleanup_aops_wire_logs(log_dir: Path, *, now: datetime) -> None:
    cutoff = now.astimezone(timezone.utc).date() - timedelta(days=_AOPS_WIRE_LOG_RETENTION_DAYS - 1)
    for path in log_dir.glob("aops-wire-*.log"):
        raw_date = path.stem.removeprefix("aops-wire-")
        try:
            log_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if log_date < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _write_aops_wire_log(
    *,
    level: str,
    direction: str,
    action: str,
    payload: Any = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    log_dir = get_hermes_home() / "logs" / "aops"
    record = {
        "ts": _format_utc(now),
        "localTime": _format_local(now),
        "level": level,
        "direction": direction,
        "action": action,
        **_extract_aops_wire_fields(payload),
        "payload": payload,
    }
    if error:
        record["error"] = error
    try:
        with _AOPS_WIRE_LOG_LOCK:
            log_dir.mkdir(parents=True, exist_ok=True)
            _cleanup_aops_wire_logs(log_dir, now=now)
            log_path = log_dir / f"aops-wire-{now.date().isoformat()}.log"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("AOPS wire log write failed: %s", exc)


@dataclass
class _ReplyContext:
    channel_id: str
    reply_to_id: Optional[str]
    run_id: Optional[str]


class AopsLiveReplyBridge:
    """Thread-safe bridge for AOPS native streaming replies."""

    def __init__(self, adapter: "AopsAdapter", *, chat_id: str, reply_to_id: Optional[str], run_id: Optional[str] = None):
        self.adapter = adapter
        self.context = _ReplyContext(channel_id=str(chat_id), reply_to_id=reply_to_id, run_id=run_id)
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
            self.adapter._log_wire(
                "info",
                direction="drop",
                action="tool_progress.dropped",
                payload=payload,
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
        self._connected_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._channel_send_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._chat_cache: dict[str, dict[str, Any]] = {}
        self._seen_message_ids: set[str] = set()
        self._reply_flags_by_message_id: dict[str, dict[str, Any]] = {}
        self._bot_id: Optional[str] = None
        self._bot_name: Optional[str] = None

    @property
    def push_tool_calls(self) -> bool:
        return self._push_tool_calls

    @property
    def dm_policy(self) -> str:
        return self._dm_policy

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
        _write_aops_wire_log(
            level=level,
            direction=direction,
            action=action,
            payload=payload,
            error=error,
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
        self._connected_event.clear()
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._cleanup()
        self._mark_disconnected()

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
        self._connected_event.set()
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
        async with self._session.get(url, headers=self._headers(), **request_kwargs) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"/bot/me failed ({resp.status}): {body[:200]}")
            return await resp.json()

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
        }

    async def _report_agents(self, *, request_kwargs: dict[str, Any]) -> None:
        if not self._session or not self._bot_id:
            return
        url = urljoin(f"{self._base_url}/", "api/v1/bot/agents/report")
        payload = self._build_agent_report_payload()
        try:
            async with self._session.post(url, headers={**self._headers(), "Content-Type": "application/json"}, json=payload, **request_kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[%s] agent report failed (%s): %s", self.name, resp.status, body[:200])
        except Exception as exc:
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
                self._connected_event.clear()
                logger.warning("[%s] AOPS socket error: %s", self.name, exc)
                self._log_wire(
                    "warning",
                    direction="state",
                    action="ws.socket_error",
                    error=str(exc),
                )
                delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                try:
                    await self._open_connection()
                    attempt = 0
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)
                    self._log_wire(
                        "warning",
                        direction="state",
                        action="ws.reconnect_failed",
                        error=str(reconnect_exc),
                    )

    async def _read_events(self) -> None:
        if not self._ws:
            raise RuntimeError("AOPS websocket not connected")
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._log_wire("info", direction="in", action="ws.receive", payload=payload)
                    if await self._handle_ws_control_event(payload):
                        continue
                    await self._dispatch_payload(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                self._log_wire(
                    "warning",
                    direction="in",
                    action="ws.closed",
                    payload={
                        "event": "closed",
                        "type": str(msg.type),
                        "data": getattr(msg, "data", None),
                    },
                )
                raise RuntimeError("AOPS websocket closed")

    async def _handle_ws_control_event(self, payload: dict[str, Any]) -> bool:
        event = str(payload.get("event") or "").strip().lower()
        if event != "ping":
            return False
        pong = {
            "event": "pong",
            "data": {
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        }
        async with self._send_lock:
            if not self._ws or self._ws.closed:
                raise RuntimeError("AOPS websocket not connected")
            await self._ws.send_json(pong)
        self._log_wire("info", direction="out", action="ws.send", payload=pong)
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
        await self._attach_inbound_attachments(event)
        await self.handle_message(event)

    def _normalize_inbound_message(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        if payload.get("event") == "message_posted":
            data = payload.get("data")
            return data if isinstance(data, dict) else None

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
        if isinstance(data.get("silent"), bool):
            self._reply_flags_by_message_id[message_id] = {"silent": bool(data["silent"])}
        text, text_was_messages, system_prompt = _extract_aops_text(data.get("text"))
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
        if raw.get("silent") is True or str(raw.get("messageType") or "").strip().lower() == "silent":
            self._log_wire(
                "info",
                direction="drop",
                action="attachment.download.skipped",
                payload={"messageId": getattr(event, "message_id", None), "reason": "silent"},
            )
            return
        attachments = raw.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return

        media_paths: list[str] = []
        media_types: list[str] = []
        failures: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                self._log_wire(
                    "warning",
                    direction="drop",
                    action="attachment.download.skipped",
                    payload={"messageId": getattr(event, "message_id", None), "reason": "invalid_attachment"},
                )
                continue
            file_id = str(attachment.get("fileId") or attachment.get("file_id") or "").strip()
            if not file_id:
                self._log_wire(
                    "warning",
                    direction="drop",
                    action="attachment.download.skipped",
                    payload={
                        "messageId": getattr(event, "message_id", None),
                        "reason": "missing_file_id",
                        "fileName": attachment.get("fileName") or attachment.get("file_name"),
                    },
                )
                continue
            attachment_log = self._attachment_log_payload(event, attachment)
            self._log_wire("info", direction="in", action="attachment.download.start", payload=attachment_log)
            try:
                cached_path, media_type = await self._download_and_cache_attachment(attachment)
            except Exception as exc:
                logger.warning("[%s] Failed to download AOPS attachment %s: %s", self.name, file_id, exc)
                failures.append(str(attachment.get("fileName") or attachment.get("file_name") or file_id))
                self._log_wire(
                    "warning",
                    direction="in",
                    action="attachment.download.failed",
                    payload=attachment_log,
                    error=str(exc),
                )
                continue
            if cached_path:
                media_paths.append(cached_path)
                media_types.append(media_type)
                self._log_wire(
                    "info",
                    direction="in",
                    action="attachment.download.success",
                    payload={**attachment_log, "mimeType": media_type, "cachedPath": cached_path},
                )

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
        async with self._session.get(url, headers=self._headers(), **request_kwargs) as resp:
            if resp.status >= 400:
                body = await resp.text()
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

    async def _send_payload(self, payload: dict[str, Any], *, channel_id: str) -> SendResult:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="AOPS websocket is not connected", retryable=True)
        lock = self._channel_send_locks[str(channel_id)]
        async with lock:
            async with self._send_lock:
                if not self._ws or self._ws.closed:
                    return SendResult(success=False, error="AOPS websocket is not connected", retryable=True)
                await self._ws.send_json(payload)
        return SendResult(success=True, message_id=((payload.get("data") or {}).get("messageId")))

    async def send_reply_event(self, data: dict[str, Any]) -> SendResult:
        reply_to_id = str(data.get("replyToId") or "").strip()
        if "silent" not in data and reply_to_id:
            reply_flags = self._reply_flags_by_message_id.get(reply_to_id) or {}
            if isinstance(reply_flags.get("silent"), bool):
                data = {**data, "silent": reply_flags["silent"]}
        if "messageType" not in data:
            data = {
                **data,
                "messageType": (
                    "cron"
                    if str(data.get("messageType") or "").strip().lower() == "cron"
                    else ("silent" if data.get("silent") is True else "common")
                ),
            }
        payload = {"event": "message_reply", "data": data}
        self._log_wire("info", direction="out", action="ws.send", payload=payload)
        return await self._send_payload(payload, channel_id=str(data.get("channelId") or ""))

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
        start_payload = {
            "messageId": message_id,
            "seq": 1,
            "phase": "start",
            "kind": kind,
            "channelId": chat_id,
            "conversationEnded": False,
            "ts": _now_ms(),
            "messageType": _aops_message_type(metadata=metadata, inherited_silent=inherited_silent),
        }
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
            "messageType": _aops_message_type(metadata=metadata, inherited_silent=inherited_silent),
        }
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
        text = f"Dangerous command requires approval: {description or command}"
        return await self.send(
            chat_id=chat_id,
            content=text,
            reply_to=metadata.get("reply_to"),
            metadata={
                **metadata,
                "content": [{
                    "type": "approval",
                    "id": approval_id,
                    "approvalKind": "exec",
                    "allowedActions": ["allow-once", "allow-always", "deny"],
                    "expiresAtMs": expires_at_ms,
                    "sessionKey": session_key,
                    "command": command,
                    "description": description,
                }],
            },
        )

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
