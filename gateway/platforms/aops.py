"""AOPS gateway adapter and native streaming bridge."""

from __future__ import annotations

import asyncio
import json
import logging
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
from urllib.parse import urljoin, urlparse, urlunparse

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
    proxy_kwargs_for_aiohttp,
    resolve_proxy_url,
)
from hermes_constants import get_hermes_home

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
_MACHINE_ID_CACHE: Optional[str] = None


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


def _machine_id() -> str:
    global _MACHINE_ID_CACHE
    if _MACHINE_ID_CACHE is not None:
        return _MACHINE_ID_CACHE
    try:
        _MACHINE_ID_CACHE = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except Exception:
        _MACHINE_ID_CACHE = ""
    return _MACHINE_ID_CACHE


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
        self._base_url = str(extra.get("base_url") or os.getenv("AOPS_BASE_URL", "")).rstrip("/")
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
            "tec-client-ip": _machine_id(),
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
                "AOPS startup failed: AOPS_BOT_TOKEN and AOPS_BASE_URL are required",
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
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
            **session_kwargs,
        )
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
        text = str(message_data.get("content") or "").strip()
        if not user_id or not channel_id or not text:
            return None

        agent_key = str(
            message_data.get("agentId")
            or metadata.get("agentId")
            or ""
        ).strip()
        if agent_key == "main":
            agent_key = ""

        return {
            "id": message_id,
            "userId": user_id,
            "userName": str(metadata.get("userName") or "").strip() or None,
            "text": text,
            "channelId": channel_id,
            "channelType": "direct",
            "timestamp": message_data.get("createdAt") or message_data.get("updatedAt"),
            "agentKey": agent_key or None,
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
        return MessageEvent(
            text=str(data.get("text") or ""),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=message_id,
            timestamp=_iso_to_datetime(data.get("timestamp")),
            channel_prompt=channel_prompt,
            route_overrides=route_overrides,
        )

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
        }
        if reply_to:
            start_payload["replyToId"] = reply_to
        if run_id:
            start_payload["runId"] = run_id
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
        }
        if reply_to:
            end_payload["replyToId"] = reply_to
        if run_id:
            end_payload["runId"] = run_id
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
