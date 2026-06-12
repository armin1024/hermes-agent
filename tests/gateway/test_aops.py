import asyncio
import importlib
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from agent.prompt_builder import PLATFORM_HINTS
from gateway.config import GatewayConfig, Platform, PlatformConfig, _apply_env_overrides
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.aops import AopsAdapter, AopsLiveReplyBridge, SendResult
import gateway.platforms.aops as aops_mod
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools.send_message_tool import (
    _parse_target_ref,
    _send_to_platform,
)
import toolsets


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _approval_commands(actions):
    return [action["command"] for action in actions]


def _approval_displays(actions):
    return [action["display"] for action in actions]


class _RunningAgent:
    def __init__(self):
        self.interrupts = []

    def interrupt(self, message):
        self.interrupts.append(message)


class _FakeResponse:
    def __init__(self, *, status=200, payload=None, text="", body=b"", headers=None):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def read(self):
        return self._body


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.messages = []
        self.on_receive = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        if self.on_receive:
            self.on_receive()
        return self.messages.pop(0)

    async def close(self):
        self.closed = True


class _FakeClientSession:
    def __init__(self, ws):
        self.ws = ws
        self.closed = False
        self.kwargs = {}
        self.get_calls = []
        self.post_calls = []
        self.ws_calls = []
        self.get_responses = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.get_responses:
            return self.get_responses.pop(0)
        return _FakeResponse(payload={"id": "bot-001", "name": "AOPS Bot"})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(payload={"ok": True})

    async def ws_connect(self, url, **kwargs):
        self.ws_calls.append((url, kwargs))
        return self.ws

    async def close(self):
        self.closed = True


def _make_runner(platform: Platform = Platform.AOPS, extra=None):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="token", extra=extra or {})}
    )
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner.delivery_router = SimpleNamespace(adapters={})
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.emit_collect = AsyncMock(return_value=[])
    runner.hooks.loaded_hooks = []
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def _make_aops_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.AOPS,
            user_id="user-001",
            chat_id="user-001",
            user_name="AOPS User",
            chat_type="dm",
        ),
        message_id="msg-1",
    )


def _make_aops_event_for_channel(text: str, *, channel_id: str = "conv-001", agent_key: str = "main") -> MessageEvent:
    event = _make_aops_event(text)
    event.source = SessionSource(
        platform=Platform.AOPS,
        user_id="user-001",
        chat_id=channel_id,
        user_name="AOPS User",
        chat_type="dm",
    )
    event.raw_message = {"agentKey": agent_key}
    return event


def _make_silent_aops_event(text: str, *, metadata: dict | None = None) -> MessageEvent:
    event = _make_aops_event(text)
    event.raw_message = {
        "silent": True,
        "model": "openclaw",
        "metadata": {
            "silent": True,
            "id": 123456,
            "botId": "bot-001",
            "agentId": "main",
            **(metadata or {}),
        },
    }
    return event


def _make_message_type_silent_aops_event(text: str) -> MessageEvent:
    event = _make_aops_event(text)
    event.raw_message = {
        "messageType": "silent",
        "model": "openclaw",
        "metadata": {
            "id": 123456,
            "botId": "bot-001",
            "agentId": "main",
        },
    }
    return event


def _make_top_level_context_silent_aops_event(text: str) -> MessageEvent:
    event = _make_aops_event(text)
    event.raw_message = {
        "id": "msg-top-1",
        "botId": "bot-top",
        "agentId": "agent-top",
        "model": "openclaw",
        "messageType": "silent",
        "metadata": {},
    }
    event.message_id = "msg-top-1"
    return event


def _make_wire_silent_aops_payload(text: str, *, message_id: str = "wire-msg-1") -> dict:
    return {
        "event": "message_posted",
        "data": {
            "id": message_id,
            "userId": "user-001",
            "userName": "AOPS User",
            "agentKey": "main",
            "text": text,
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-05-22T03:20:30Z",
            "silent": True,
        },
    }


def _aops_messages_text(system_text: str, user_text: str) -> str:
    return json.dumps(
        [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        ensure_ascii=False,
    )


def _read_aops_log_lines(hermes_home):
    files = sorted((hermes_home / "logs" / "aops").glob("aops-*.log"))
    lines = []
    for path in files:
        lines.extend(line for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return lines


def _aops_log_raw(line: str):
    return json.loads(line.split(" raw=", 1)[1])


async def _drain_aops_dispatch_tasks(adapter: AopsAdapter):
    queue = getattr(adapter, "_dispatch_queue", None)
    if queue is not None:
        await asyncio.wait_for(queue.join(), timeout=2.0)
    task = getattr(adapter, "_dispatch_worker_task", None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        adapter._dispatch_worker_task = None
    tasks = [task for task in getattr(adapter, "_dispatch_tasks", set()) if not task.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _aops_log_field(line: str, field: str) -> str:
    prefix = f" {field}="
    if field == line.split("=", 1)[0]:
        return line.split("=", 1)[1].split(" ", 1)[0]
    return line.split(prefix, 1)[1].split(" ", 1)[0]


def _aops_log_events(hermes_home):
    return [_aops_log_field(line, "event") for line in _read_aops_log_lines(hermes_home)]


def test_platform_aops_registered():
    assert Platform.AOPS.value == "aops"


def test_aops_gateway_setup_platform_registered():
    from hermes_cli.gateway import _all_platforms
    from hermes_cli.platforms import PLATFORMS

    platforms = {platform["key"]: platform for platform in _all_platforms()}

    assert "aops" in platforms
    assert platforms["aops"]["token_var"] == "AOPS_BOT_TOKEN"
    assert [item["name"] for item in platforms["aops"]["vars"]] == [
        "AOPS_BOT_TOKEN",
        "AOPS_BOT_URL",
        "AOPS_HOME_CHANNEL",
    ]
    assert PLATFORMS["aops"].default_toolset == "hermes-aops"


def test_get_connected_platforms_recognizes_aops():
    config = GatewayConfig(
        platforms={Platform.AOPS: PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})}
    )
    assert config.get_connected_platforms() == [Platform.AOPS]


def test_apply_env_overrides_reads_aops(monkeypatch):
    monkeypatch.setenv("AOPS_BOT_TOKEN", "tok")
    monkeypatch.setenv("AOPS_BOT_URL", "https://aops.example.com")
    monkeypatch.setenv("AOPS_HOME_CHANNEL", "user-001")
    monkeypatch.setenv("AOPS_PUSH_TOOL_CALLS", "false")
    monkeypatch.setenv("AOPS_DM_POLICY", "allowlist")
    monkeypatch.setenv("AOPS_ALLOW_FROM", "user-001,user-002")
    monkeypatch.setenv("AOPS_TRUSTED_AGENT_KEY_FROM", "user-001")
    monkeypatch.setenv("AOPS_PROXY", "http://proxy.internal:8080")

    config = GatewayConfig()
    _apply_env_overrides(config)

    aops = config.platforms[Platform.AOPS]
    assert aops.token == "tok"
    assert aops.extra["base_url"] == "https://aops.example.com"
    assert aops.home_channel.chat_id == "user-001"
    assert aops.extra["push_tool_calls"] is False
    assert aops.extra["dm_policy"] == "allowlist"
    assert aops.extra["allow_from"] == ["user-001", "user-002"]
    assert aops.extra["trusted_agent_key_from"] == ["user-001"]
    assert aops.extra["proxy"] == "http://proxy.internal:8080"


@pytest.mark.asyncio
async def test_create_adapter_returns_aops_adapter(monkeypatch):
    monkeypatch.setattr("gateway.platforms.aops.AIOHTTP_AVAILABLE", True)
    runner = _make_runner()
    adapter = runner._create_adapter(
        Platform.AOPS,
        PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}),
    )
    assert isinstance(adapter, AopsAdapter)


@pytest.mark.asyncio
async def test_aops_connect_calls_bot_me_and_ws_auth(monkeypatch):
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    fake_aiohttp = types.SimpleNamespace(
        ClientSession=lambda **kwargs: fake_session,
        ClientTimeout=lambda total: total,
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )

    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setattr("gateway.platforms.aops.AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr("gateway.platforms.aops._resolve_aops_client_id", lambda config: "client-123")

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._listen_loop = AsyncMock(return_value=None)

    assert await adapter.connect() is True
    assert fake_session.get_calls
    get_url, get_kwargs = fake_session.get_calls[0]
    assert get_url.endswith("/api/v1/bot/me")
    assert get_kwargs["headers"]["Authorization"] == "Bearer tok"
    assert get_kwargs["headers"]["tec-client-ip"] == "client-123"
    assert fake_session.ws_calls


@pytest.mark.asyncio
async def test_aops_connect_timeout_reads_env(monkeypatch):
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)

    def _client_session(**kwargs):
        fake_session.kwargs = kwargs
        return fake_session

    fake_aiohttp = types.SimpleNamespace(
        ClientSession=_client_session,
        ClientTimeout=lambda total: {"total": total},
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setenv("AOPS_CONNECT_TIMEOUT", "90")
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setattr("gateway.platforms.aops.AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr("gateway.platforms.aops._resolve_aops_client_id", lambda config: "client-123")

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._listen_loop = AsyncMock(return_value=None)

    assert await adapter.connect() is True
    assert fake_session.kwargs["timeout"] == {"total": 90.0}
    ws_url, ws_kwargs = fake_session.ws_calls[0]
    assert ws_url == "wss://aops.example.com/api/v1/ws"
    assert ws_kwargs["headers"]["Authorization"] == "Bearer tok"
    assert fake_ws.sent == [{"action": "auth", "token": "tok"}]
    assert fake_session.post_calls


@pytest.mark.asyncio
async def test_aops_send_reply_event_inherits_silent_from_inbound_message():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    fake_ws = _FakeWebSocket()
    adapter._ws = fake_ws
    adapter._reply_flags_by_message_id["msg-1"] = {"silent": True}

    result = await adapter.send_reply_event(
        {
            "messageId": "botmsg-1",
            "seq": 1,
            "phase": "start",
            "kind": "final",
            "channelId": "user-001",
            "replyToId": "msg-1",
            "conversationEnded": False,
            "ts": 1,
        }
    )

    assert result.success is True
    assert fake_ws.sent[0]["data"]["title"] == ""
    assert fake_ws.sent[0]["data"]["silent"] is True
    assert fake_ws.sent[0]["data"]["messageType"] == "silent"


@pytest.mark.asyncio
async def test_aops_send_emits_start_then_end_with_inherited_silent():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    adapter._reply_flags_by_message_id["msg-1"] = {"silent": True}

    result = await adapter.send("user-001", "hello", reply_to="msg-1")

    assert result.success is True
    assert adapter.send_reply_event.await_count == 2
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["phase"] == "start"
    assert end["phase"] == "end"
    assert end["text"] == "hello"
    assert end["replyToId"] == "msg-1"
    assert start["silent"] is True
    assert end["silent"] is True
    assert start["messageType"] == "silent"
    assert end["messageType"] == "silent"


@pytest.mark.asyncio
async def test_aops_send_defaults_message_type_to_common():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send("user-001", "hello")

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["messageType"] == "common"
    assert end["messageType"] == "common"
    assert start["title"] == ""
    assert end["title"] == ""


@pytest.mark.asyncio
async def test_aops_send_includes_title_from_metadata():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send("user-001", "hello", metadata={"title": "CPU 告警排查结果"})

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["title"] == "CPU 告警排查结果"
    assert end["title"] == "CPU 告警排查结果"


@pytest.mark.asyncio
async def test_aops_send_reply_event_uses_inbound_conversation_title():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    fake_ws = _FakeWebSocket()
    adapter._ws = fake_ws

    event = adapter._build_message_event({
        "id": "msg-1",
        "userId": "user-001",
        "userName": "Alice",
        "text": "hello",
        "channelId": "conv-1",
        "channelType": "direct",
        "title": "CPU 告警排查结果",
    })
    assert event is not None

    result = await adapter.send_reply_event({
        "messageId": "botmsg-1",
        "seq": 1,
        "phase": "end",
        "kind": "final",
        "channelId": "conv-1",
        "replyToId": "msg-1",
        "conversationEnded": True,
        "ts": 1,
    })

    assert result.success is True
    assert fake_ws.sent[0]["data"]["title"] == "CPU 告警排查结果"


@pytest.mark.asyncio
async def test_aops_send_reply_event_writes_readable_message_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    fake_ws = _FakeWebSocket()
    adapter._ws = fake_ws

    result = await adapter.send_reply_event(
        {
            "messageId": "botmsg-1",
            "seq": 2,
            "phase": "end",
            "kind": "final",
            "channelId": "conv-1",
            "replyToId": "msg-1",
            "runId": "sess-1",
            "text": "已完成",
            "title": "运维排查",
            "conversationEnded": True,
            "ts": 1,
            "metadata": {"sessionKey": "aops:conv-1", "sessionId": "sess-1"},
        }
    )

    assert result.success is True
    lines = _read_aops_log_lines(tmp_path)
    assert len(lines) == 1
    assert " io=send " in lines[0]
    assert "event=message_reply" in lines[0]
    assert "messageType=common" in lines[0]
    assert "channel=conv-1" in lines[0]
    assert 'title="运维排查"' in lines[0]
    assert 'text="已完成"' in lines[0]
    assert "status=ok" in lines[0]
    assert _aops_log_raw(lines[0])["event"] == "message_reply"


@pytest.mark.asyncio
async def test_aops_send_reply_event_writes_approval_summary_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()

    await adapter.send_reply_event(
        {
            "messageId": "approval-1",
            "seq": 1,
            "phase": "actions",
            "kind": "approval",
            "channelId": "conv-1",
            "replyToId": "msg-1",
            "conversationEnded": True,
            "ts": 1,
            "content": [
                {
                    "type": "approval",
                    "approvalKind": "exec",
                    "allowedActions": [
                        {"command": "/approve", "display": "仅本次允许"},
                        {"command": "/approve always", "display": "始终允许"},
                        {"command": "/deny", "display": "拒绝"},
                    ],
                    "command": "rm -rf /tmp/foo",
                }
            ],
        }
    )

    line = _read_aops_log_lines(tmp_path)[0]
    assert " io=send " in line
    assert "event=message_reply" in line
    assert "messageType=approval" in line
    assert 'text="approval kind=exec actions=/approve,/approve always,/deny command=rm -rf /tmp/foo"' in line
    assert _aops_log_raw(line)["data"]["content"][0]["approvalKind"] == "exec"


@pytest.mark.asyncio
async def test_aops_send_reply_event_writes_silent_log_type(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()

    await adapter.send_reply_event(
        {
            "messageId": "silent-1",
            "seq": 1,
            "phase": "end",
            "kind": "final",
            "channelId": "conv-1",
            "text": "/cron result",
            "silent": True,
            "conversationEnded": True,
            "ts": 1,
        }
    )

    line = _read_aops_log_lines(tmp_path)[0]
    assert " io=send " in line
    assert "event=message_reply" in line
    assert "messageType=silent" in line
    assert "silent=true" in line
    assert 'text="/cron result"' in line
    assert _aops_log_raw(line)["data"]["silent"] is True


@pytest.mark.asyncio
async def test_aops_send_uses_cron_message_type_from_metadata():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send("user-001", "hello", metadata={"message_type": "cron"})

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["messageType"] == "cron"
    assert end["messageType"] == "cron"


@pytest.mark.asyncio
async def test_aops_send_includes_cron_bot_reply_extra_metadata():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send(
        "user-001",
        "hello",
        metadata={
            "source": "claw",
            "message_type": "cron",
            "botReplyExtra": {
                "messageType": "cron",
                "id": "0798d2788a2d",
                "name": "提醒查看数据库数据",
            },
        },
    )

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert "metadata" not in start
    assert "metadata" not in end
    assert "botReplyExtra" not in start
    assert "botReplyExtra" not in end
    assert start["source"] == "claw"
    assert end["source"] == "claw"
    assert start["messageType"] == "cron"
    assert end["messageType"] == "cron"
    assert start["id"] == "0798d2788a2d"
    assert end["id"] == "0798d2788a2d"
    assert start["name"] == "提醒查看数据库数据"
    assert end["name"] == "提醒查看数据库数据"


@pytest.mark.asyncio
async def test_aops_send_uses_resolved_session_title_when_metadata_title_empty():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    adapter.set_title_resolver(lambda payload: "CPU 告警排查" if payload.get("runId") == "sess-1" else "")

    result = await adapter.send(
        "user-001",
        "hello",
        metadata={"run_id": "sess-1", "message_type": "cron"},
    )

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["title"] == "CPU 告警排查"
    assert end["title"] == "CPU 告警排查"


@pytest.mark.asyncio
async def test_aops_send_reply_event_uses_resolved_session_title(monkeypatch):
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()
    adapter.set_title_resolver(lambda payload: "磁盘空间巡检" if payload.get("runId") == "sess-2" else "")

    result = await adapter.send_reply_event(
        {
            "messageId": "reply-1",
            "seq": 1,
            "phase": "end",
            "kind": "final",
            "channelId": "conv-1",
            "text": "ok",
            "conversationEnded": True,
            "runId": "sess-2",
        }
    )

    assert result.success is True
    sent = adapter._ws.sent[0]["data"]
    assert sent["title"] == "磁盘空间巡检"


@pytest.mark.asyncio
async def test_aops_outbound_title_resolves_from_channel_session(tmp_path):
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    runner = _make_runner(extra={"dm_policy": "open"})
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
    runner._session_db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.AOPS, chat_id="conv-1", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    runner._session_db.create_session(entry.session_id, source="aops")
    runner._session_db.set_session_title(entry.session_id, "CPU 告警排查")

    title = runner._aops_outbound_title_for_payload({"channelId": "conv-1"})

    assert title == "CPU 告警排查"


@pytest.mark.asyncio
async def test_aops_title_command_result_metadata_uses_set_session_title(tmp_path):
    from gateway.aops_commands import LocalCommandResult
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    runner = _make_runner(extra={"dm_policy": "open"})
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
    runner._session_db = SessionDB(db_path=tmp_path / "state.db")

    result = await runner._handle_message(_make_aops_event_for_channel("/title 我的标题", channel_id="conv-title"))

    assert isinstance(result, LocalCommandResult)
    assert result.metadata["title"] == "我的标题"
    assert result.metadata["sessionId"]


@pytest.mark.asyncio
async def test_aops_title_command_result_metadata_uses_current_session_title(tmp_path):
    from gateway.aops_commands import LocalCommandResult
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    runner = _make_runner(extra={"dm_policy": "open"})
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
    runner._session_db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.AOPS, chat_id="conv-title", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    runner._session_db.create_session(entry.session_id, source="aops")
    runner._session_db.set_session_title(entry.session_id, "已有中文标题")

    result = await runner._handle_message(_make_aops_event_for_channel("/title", channel_id="conv-title"))

    assert isinstance(result, LocalCommandResult)
    assert result.metadata["title"] == "已有中文标题"
    assert "已有中文标题" in result.text


@pytest.mark.asyncio
async def test_aops_send_uses_title_command_metadata_in_payload(tmp_path):
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    runner = _make_runner(extra={"dm_policy": "open"})
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
    runner._session_db = SessionDB(db_path=tmp_path / "state.db")
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await runner._handle_message(_make_aops_event_for_channel("/title 我的标题", channel_id="conv-title"))
    await adapter.send("conv-title", result.text, metadata=result.metadata)

    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["title"] == "我的标题"
    assert end["title"] == "我的标题"


@pytest.mark.asyncio
async def test_aops_send_reply_event_flattens_nested_cron_bot_reply_extra():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()

    result = await adapter.send_reply_event(
        {
            "messageId": "cron-1",
            "seq": 1,
            "phase": "start",
            "kind": "final",
            "channelId": "conv-1",
            "conversationEnded": False,
            "messageType": "cron",
            "source": "claw",
            "botReplyExtra": {
                "messageType": "cron",
                "title": "",
                "source": "claw",
                "botReplyExtra": {
                    "messageType": "cron",
                    "id": "940bdb32d5af",
                    "name": "双分钟祝福",
                },
            },
        }
    )

    assert result.success is True
    sent = adapter._ws.sent[0]["data"]
    assert "metadata" not in sent
    assert "botReplyExtra" not in sent
    assert sent["source"] == "claw"
    assert sent["messageType"] == "cron"
    assert sent["id"] == "940bdb32d5af"
    assert sent["name"] == "双分钟祝福"


@pytest.mark.asyncio
async def test_aops_send_flattens_nested_cron_bot_reply_extra():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send(
        "user-001",
        "hello",
        metadata={
            "source": "claw",
            "message_type": "cron",
            "botReplyExtra": {
                "messageType": "cron",
                "title": "",
                "source": "claw",
                "botReplyExtra": {
                    "messageType": "cron",
                    "id": "940bdb32d5af",
                    "name": "双分钟祝福",
                },
            },
        },
    )

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert "botReplyExtra" not in start
    assert "botReplyExtra" not in end
    assert start["source"] == "claw"
    assert end["source"] == "claw"
    assert start["messageType"] == "cron"
    assert end["messageType"] == "cron"
    assert start["id"] == "940bdb32d5af"
    assert end["id"] == "940bdb32d5af"
    assert start["name"] == "双分钟祝福"
    assert end["name"] == "双分钟祝福"


@pytest.mark.asyncio
async def test_aops_send_cron_message_type_overrides_inherited_silent():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    adapter._reply_flags_by_message_id["msg-1"] = {"silent": True}

    result = await adapter.send(
        "user-001",
        "hello",
        reply_to="msg-1",
        metadata={"message_type": "cron"},
    )

    assert result.success is True
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["silent"] is True
    assert end["silent"] is True
    assert start["messageType"] == "cron"
    assert end["messageType"] == "cron"


@pytest.mark.asyncio
async def test_aops_send_emits_structured_content_in_end_payload():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send(
        "user-001",
        '{"ok":true}',
        reply_to="msg-1",
        metadata={
            "content": [
                {
                    "type": "commandResult",
                    "command": "clawhub explore --json",
                    "items": [{"slug": "knowledge-query"}],
                }
            ]
        },
    )

    assert result.success is True
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert end["phase"] == "end"
    assert end["content"][0]["type"] == "commandResult"
    assert end["content"][0]["items"][0]["slug"] == "knowledge-query"


def test_aops_silent_reasoning_status_reads_current_config(tmp_path, monkeypatch):
    import yaml
    from gateway import aops_commands

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {"reasoning_effort": "high"},
                "display": {"platforms": {"aops": {"show_reasoning": True}}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = aops_commands.maybe_local_command(_make_aops_event("/reasoning"))
    payload = json.loads(result)

    assert payload["type"] == "reasoning.status"
    assert payload["ok"] is True
    assert payload["level"] == "high"
    assert payload["enabled"] is True
    assert payload["showReasoning"] is True


def test_aops_silent_reasoning_status_expands_default_level(tmp_path, monkeypatch):
    import yaml
    from gateway import aops_commands

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = aops_commands.maybe_local_command(_make_aops_event("/reasoning"))
    payload = json.loads(result)

    assert payload["type"] == "reasoning.status"
    assert payload["ok"] is True
    assert payload["level"] == "medium"
    assert payload["enabled"] is True
    assert payload["source"] == "default"
    assert payload["isDefault"] is True
    assert payload["defaultLevel"] == "medium"


def test_aops_silent_reasoning_set_persists_global_config(tmp_path, monkeypatch):
    import yaml
    from gateway import aops_commands

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"reasoning_effort": "low"}}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = aops_commands.maybe_local_command(_make_aops_event("/reasoning xhigh"))
    payload = json.loads(result)
    saved = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))

    assert payload["type"] == "reasoning.updated"
    assert payload["ok"] is True
    assert payload["level"] == "xhigh"
    assert payload["enabled"] is True
    assert payload["persisted"] is True
    assert saved["agent"]["reasoning_effort"] == "xhigh"


def test_resolve_aops_client_id_reads_existing_file(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "coder")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    client_id_path = aops._aops_client_id_path(config)
    client_id_path.parent.mkdir(parents=True, exist_ok=True)
    client_id_path.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")

    assert aops._resolve_aops_client_id(config) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert aops._resolve_aops_client_id(config) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_resolve_aops_client_id_generates_and_persists_once(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "coder")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    generated = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: generated)

    first = aops._resolve_aops_client_id(config)
    second = aops._resolve_aops_client_id(config)
    client_id_path = aops._aops_client_id_path(config)

    assert first == generated
    assert second == generated
    assert client_id_path.read_text(encoding="utf-8").strip() == generated


def test_resolve_aops_client_id_shared_across_profiles_for_same_system_user(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "coder")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    profile_home = tmp_path / ".hermes" / "profiles" / "coder"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    values = iter([
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "11111111-2222-3333-4444-555555555555",
    ])
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: next(values))

    first = aops._resolve_aops_client_id(config)
    assert first == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert aops._aops_client_id_path(config).read_text(encoding="utf-8").strip() == first

    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "assistant"))
    second = aops._resolve_aops_client_id(config)
    assert second == first
    assert len(list((tmp_path / ".hermes" / "aops").glob("client-id-v2-*"))) == 1


def test_resolve_aops_client_id_isolated_by_system_user(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})

    values = iter(
        [
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "11111111-2222-3333-4444-555555555555",
        ]
    )
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: next(values))
    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    monkeypatch.setattr(aops.getpass, "getuser", lambda: "coder")
    first = aops._resolve_aops_client_id(config)

    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "assistant")
    second = aops._resolve_aops_client_id(config)

    assert first != second
    assert len(list((tmp_path / ".hermes" / "aops").glob("client-id-v2-*"))) == 2


def test_resolve_aops_client_id_shared_by_aops_config_for_same_system_user(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "hermes")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    values = iter([
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "11111111-2222-3333-4444-555555555555",
    ])
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: next(values))

    config_a = PlatformConfig(enabled=True, token="token-a", extra={"base_url": "https://aops.example.com"})
    config_b = PlatformConfig(enabled=True, token="token-b", extra={"base_url": "https://aops.example.com"})

    assert aops._resolve_aops_client_id(config_a) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert aops._resolve_aops_client_id(config_b) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert len(list((tmp_path / ".hermes" / "aops").glob("client-id-v2-*"))) == 1


def test_resolve_aops_client_id_migrates_legacy_file_in_current_home(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    current_home = tmp_path / "current"
    other_home = tmp_path / "other"
    monkeypatch.setenv("HERMES_HOME", str(current_home))
    monkeypatch.setenv("AOPS_MIGRATE_LEGACY_CLIENT_ID", "1")
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "hermes")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})

    legacy_name = f"client-id-{aops._aops_client_user_key()}"
    (current_home / "aops").mkdir(parents=True)
    (other_home / "aops").mkdir(parents=True)
    (current_home / "aops" / legacy_name).write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")
    (other_home / "aops" / legacy_name).write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")

    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    assert aops._resolve_aops_client_id(config) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert aops._aops_client_id_path(config).read_text(encoding="utf-8").strip() == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_resolve_aops_client_id_ignores_legacy_file_for_new_install(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("AOPS_MIGRATE_LEGACY_CLIENT_ID", raising=False)
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "oma")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: "11111111-2222-3333-4444-555555555555")

    legacy_name = f"client-id-{aops._aops_client_user_key()}"
    (hermes_home / "aops").mkdir(parents=True)
    (hermes_home / "aops" / legacy_name).write_text("copied-from-another-user\n", encoding="utf-8")

    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    assert aops._resolve_aops_client_id(config) == "11111111-2222-3333-4444-555555555555"
    assert aops._aops_client_id_path(config).read_text(encoding="utf-8").strip() == "11111111-2222-3333-4444-555555555555"


def test_resolve_aops_client_id_ignores_invalid_existing_file(monkeypatch, tmp_path):
    import gateway.platforms.aops as aops

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(aops.getpass, "getuser", lambda: "coder")
    monkeypatch.setattr(aops, "_AOPS_CLIENT_ID_CACHE", {})
    monkeypatch.setattr(aops.uuid, "uuid4", lambda: "11111111-2222-3333-4444-555555555555")
    config = PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})

    client_id_path = aops._aops_client_id_path(config)
    client_id_path.parent.mkdir(parents=True, exist_ok=True)
    client_id_path.write_text("not-a-uuid\n", encoding="utf-8")

    assert aops._resolve_aops_client_id(config) == "11111111-2222-3333-4444-555555555555"
    assert client_id_path.read_text(encoding="utf-8").strip() == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_aops_read_events_replies_to_server_ping(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    fake_ws.messages = [
        SimpleNamespace(
            type="TEXT",
            data=json.dumps({
                "event": "ping",
                "message": "heartbeat",
                "data": {"ts": "2026-04-28T07:58:00Z", "timeoutMs": 30000},
            }),
        )
    ]
    fake_ws.on_receive = lambda: setattr(adapter, "_running", False)
    adapter._ws = fake_ws
    adapter._running = True
    adapter._dispatch_payload = AsyncMock()

    await adapter._read_events()

    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["event"] == "pong"
    assert fake_ws.sent[0]["data"]["ts"].endswith("Z")
    adapter._dispatch_payload.assert_not_awaited()
    lines = _read_aops_log_lines(tmp_path)
    assert [_aops_log_field(line, "io") for line in lines] == ["recv", "send"]
    assert [_aops_log_field(line, "event") for line in lines] == ["ping", "pong"]
    assert [_aops_log_field(line, "messageType") for line in lines] == ["ws", "ws"]
    assert _aops_log_raw(lines[0])["event"] == "ping"
    assert _aops_log_raw(lines[0])["message"] == "heartbeat"
    assert _aops_log_raw(lines[1])["event"] == "pong"
    assert lines[1].startswith(datetime.now().astimezone().strftime("%Y-%m-%dT"))


def test_aops_send_primitives_recover_across_event_loops():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    adapter._ws = fake_ws
    adapter._connected = True

    async def bind_old_loop_lock():
        lock = adapter._send_guard()
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        assert getattr(lock, "_loop", None) is asyncio.get_running_loop()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        lock.release()
        return lock

    old_loop = asyncio.new_event_loop()
    try:
        old_lock = old_loop.run_until_complete(bind_old_loop_lock())
    finally:
        old_loop.close()

    async def send_on_new_loop():
        pong_handled = await adapter._handle_ws_control_event({"event": "ping", "data": {"timeoutMs": 30000}})
        reply = await adapter._send_payload(
            {"event": "message_reply", "data": {"messageId": "reply-1"}},
            channel_id="main",
        )
        return pong_handled, reply, adapter._send_lock

    new_loop = asyncio.new_event_loop()
    try:
        pong_handled, reply, new_lock = new_loop.run_until_complete(send_on_new_loop())
    finally:
        new_loop.close()

    assert pong_handled is True
    assert reply.success is True
    assert new_lock is not old_lock
    assert fake_ws.sent[0]["event"] == "pong"
    assert fake_ws.sent[1]["event"] == "message_reply"


@pytest.mark.asyncio
async def test_aops_read_events_dispatches_message_posted_after_ping_support(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    payload = {
        "event": "message_posted",
        "data": {
            "id": "msg-1",
            "userId": "user-001",
            "userName": "Alice",
            "text": "check cpu",
            "channelId": "user-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
        },
    }
    fake_ws.messages = [SimpleNamespace(type="TEXT", data=json.dumps(payload))]
    fake_ws.on_receive = lambda: setattr(adapter, "_running", False)
    adapter._ws = fake_ws
    adapter._running = True
    adapter.handle_message = AsyncMock()

    await adapter._read_events()
    await _drain_aops_dispatch_tasks(adapter)

    assert fake_ws.sent == []
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_id == "msg-1"
    assert event.text == "check cpu"


@pytest.mark.asyncio
async def test_aops_read_events_logs_ws_close_code(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    fake_ws.messages = [SimpleNamespace(type="CLOSE", data=4004, extra="policy")]
    fake_ws.close_code = 4004
    fake_ws.exception = lambda: RuntimeError("server closed")
    adapter._ws = fake_ws
    adapter._running = True

    with pytest.raises(RuntimeError):
        await adapter._read_events()

    line = _read_aops_log_lines(tmp_path)[0]
    assert " io=recv " in line
    assert "event=ws.closed" in line
    assert "messageType=ws" in line
    assert 'text="closeCode=4004 reasonHint=server_closed_code_4004"' in line
    assert _aops_log_raw(line)["closeCode"] == 4004


@pytest.mark.asyncio
async def test_aops_command_message_is_written_to_unified_log(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    payload = {
        "event": "message_posted",
        "data": {
            "id": "msg-cmd-1",
            "userId": "user-001",
            "userName": "Alice",
            "text": "/cron",
            "channelId": "conv-1",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "title": "运维排查",
        },
    }
    fake_ws.messages = [SimpleNamespace(type="TEXT", data=json.dumps(payload))]
    fake_ws.on_receive = lambda: setattr(adapter, "_running", False)
    adapter._ws = fake_ws
    adapter._running = True
    adapter.handle_message = AsyncMock()

    await adapter._read_events()
    await _drain_aops_dispatch_tasks(adapter)

    lines = _read_aops_log_lines(tmp_path)
    assert len(lines) == 1
    assert " io=recv " in lines[0]
    assert "event=message_posted" in lines[0]
    assert "messageType=-" in lines[0]
    assert "msg=msg-cmd-1" in lines[0]
    assert 'title="运维排查"' in lines[0]
    assert 'text="/cron"' in lines[0]
    assert _aops_log_raw(lines[0])["event"] == "message_posted"


@pytest.mark.asyncio
async def test_aops_read_events_keeps_ping_pong_while_message_dispatch_is_slow(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    fake_ws.messages = [
        SimpleNamespace(
            type="TEXT",
            data=json.dumps({
                "event": "message_posted",
                "data": {
                    "id": "msg-slow-1",
                    "userId": "user-001",
                    "userName": "Alice",
                    "text": "/model status",
                    "channelId": "conv-1",
                    "channelType": "direct",
                    "timestamp": "2026-04-09T08:00:00.000Z",
                    "silent": True,
                },
            }),
        ),
        SimpleNamespace(
            type="TEXT",
            data=json.dumps({
                "event": "ping",
                "message": "heartbeat",
                "data": {"ts": "2026-04-09T08:00:01Z", "timeoutMs": 30000},
            }),
        ),
    ]
    receive_count = 0

    def on_receive():
        nonlocal receive_count
        receive_count += 1
        if receive_count >= 2:
            adapter._running = False

    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def slow_dispatch(payload):
        dispatch_started.set()
        await release_dispatch.wait()

    fake_ws.on_receive = on_receive
    adapter._ws = fake_ws
    adapter._running = True
    adapter._dispatch_payload = slow_dispatch

    await adapter._read_events()
    await asyncio.wait_for(dispatch_started.wait(), timeout=0.5)

    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["event"] == "pong"
    release_dispatch.set()
    await _drain_aops_dispatch_tasks(adapter)


@pytest.mark.asyncio
async def test_aops_silent_command_bypasses_normal_message_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()
    adapter.handle_message = AsyncMock()
    payload = _make_wire_silent_aops_payload("/model status", message_id="silent-msg-1")

    await adapter._dispatch_payload(payload)

    adapter.handle_message.assert_not_awaited()
    sent = [item for item in adapter._ws.sent if item.get("event") == "message_reply"]
    assert len(sent) == 1
    data = sent[0]["data"]
    assert data["phase"] == "end"
    assert data["messageType"] == "silent"
    assert data["silent"] is True
    assert data["replyToId"] == "silent-msg-1"
    assert json.loads(data["text"])["type"] == "model.status"
    lines = _read_aops_log_lines(tmp_path)
    assert any(" io=recv " in line and "event=message_posted" in line and "silent=true" in line for line in lines)
    assert any(" io=send " in line and "event=message_reply" in line and "messageType=silent" in line for line in lines)


@pytest.mark.asyncio
async def test_aops_common_message_with_metadata_silent_uses_silent_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()
    adapter.handle_message = AsyncMock()
    payload = {
        "event": "message_posted",
        "data": {
            "id": "common-silent-1",
            "userId": "user-001",
            "userName": "AOPS User",
            "text": "/model status",
            "channelId": "conv-001",
            "channelType": "direct",
            "messageType": "common",
            "metadata": {"silent": True, "id": "silent-correlation-1"},
        },
    }

    await adapter._dispatch_payload(payload)

    adapter.handle_message.assert_not_awaited()
    sent = [item["data"] for item in adapter._ws.sent if item.get("event") == "message_reply"]
    assert len(sent) == 1
    assert sent[0]["messageType"] == "silent"
    assert sent[0]["silent"] is True
    assert sent[0]["replyToId"] == "common-silent-1"
    assert json.loads(sent[0]["text"])["type"] == "model.status"


@pytest.mark.asyncio
async def test_aops_common_silent_messages_are_dispatched_in_receive_order(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    fake_ws.messages = [
        SimpleNamespace(
            type="TEXT",
            data=json.dumps({
                "event": "message_posted",
                "data": {
                    "id": "common-silent-help",
                    "userId": "user-001",
                    "userName": "AOPS User",
                    "text": "/help",
                    "channelId": "conv-001",
                    "channelType": "direct",
                    "messageType": "common",
                    "metadata": {"silent": True},
                },
            }),
        ),
        SimpleNamespace(
            type="TEXT",
            data=json.dumps({
                "event": "message_posted",
                "data": {
                    "id": "common-silent-model",
                    "userId": "user-001",
                    "userName": "AOPS User",
                    "text": "/model status",
                    "channelId": "conv-001",
                    "channelType": "direct",
                    "messageType": "common",
                    "metadata": {"silent": True},
                },
            }),
        ),
    ]
    receive_count = 0

    def on_receive():
        nonlocal receive_count
        receive_count += 1
        if receive_count >= 2:
            adapter._running = False

    fake_ws.on_receive = on_receive
    adapter._connected_event.set()
    adapter._ws = fake_ws
    adapter._running = True
    adapter.handle_message = AsyncMock()

    await adapter._read_events()
    await _drain_aops_dispatch_tasks(adapter)

    adapter.handle_message.assert_not_awaited()
    replies = [item["data"] for item in fake_ws.sent if item.get("event") == "message_reply"]
    assert [reply["replyToId"] for reply in replies] == ["common-silent-help", "common-silent-model"]
    assert all(reply["messageType"] == "silent" for reply in replies)
    assert json.loads(replies[0]["text"])["type"] == "command.tree"
    assert json.loads(replies[1]["text"])["type"] == "model.status"


@pytest.mark.asyncio
async def test_aops_silent_help_returns_command_tree(monkeypatch):
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/help"))

    payload = json.loads(result)
    assert payload["schemaVersion"] == "local-command-tree.v2"
    assert payload["type"] == "command.tree"
    assert payload["ok"] is True
    assert payload["command"] == "/help"
    assert any(item["fullCommand"] == "/model" for item in payload["items"])


@pytest.mark.asyncio
async def test_aops_read_events_ignores_unknown_event_without_pong(monkeypatch, tmp_path):
    fake_aiohttp = types.SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="TEXT", CLOSE="CLOSE", CLOSED="CLOSED", ERROR="ERROR"),
    )
    monkeypatch.setattr("gateway.platforms.aops.aiohttp", fake_aiohttp)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    fake_ws = _FakeWebSocket()
    fake_ws.messages = [SimpleNamespace(type="TEXT", data=json.dumps({"event": "presence_updated"}))]
    fake_ws.on_receive = lambda: setattr(adapter, "_running", False)
    adapter._ws = fake_ws
    adapter._running = True
    adapter.handle_message = AsyncMock()

    await adapter._read_events()

    assert fake_ws.sent == []
    adapter.handle_message.assert_not_awaited()


def test_aops_log_keeps_recent_seven_days(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    log_dir = tmp_path / "logs" / "aops"
    log_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    old_log = log_dir / f"aops-{(today - timedelta(days=8)).isoformat()}.log"
    recent_log = log_dir / f"aops-{(today - timedelta(days=6)).isoformat()}.log"
    old_wire_log = log_dir / f"aops-wire-{(today - timedelta(days=8)).isoformat()}.log"
    old_message_log = log_dir / f"aops-messages-{(today - timedelta(days=8)).isoformat()}.log"
    old_log.write_text('{"old": true}\n', encoding="utf-8")
    recent_log.write_text('{"recent": true}\n', encoding="utf-8")
    old_wire_log.write_text('{"old": true}\n', encoding="utf-8")
    old_message_log.write_text('{"old": true}\n', encoding="utf-8")

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._log_wire(
        "info",
        direction="out",
        action="ws.send",
        payload={
            "event": "message_reply",
            "data": {
                "messageId": "botmsg-1",
                "seq": 2,
                "phase": "end",
                "kind": "final",
                "channelId": "conv-1",
                "replyToId": "msg-1",
                "runId": "run-1",
                "text": "完整正文",
            },
        },
    )

    assert not old_log.exists()
    assert not old_wire_log.exists()
    assert not old_message_log.exists()
    assert recent_log.exists()
    current = next(line for line in _read_aops_log_lines(tmp_path) if "msg=botmsg-1" in line)
    assert current.startswith(datetime.now().astimezone().strftime("%Y-%m-%dT"))
    assert " io=send " in current
    assert "event=message_reply" in current
    assert "messageType=-" in current
    assert "channel=conv-1" in current
    assert "replyTo=msg-1" in current
    assert _aops_log_raw(current)["data"]["text"] == "完整正文"


def test_aops_log_retention_days_can_be_overridden_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AOPS_LOG_RETENTION_DAYS", "7")
    log_dir = tmp_path / "logs" / "aops"
    log_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    old_log = log_dir / f"aops-{(today - timedelta(days=4)).isoformat()}.log"
    old_log.write_text("old\n", encoding="utf-8")

    adapter = AopsAdapter(
        PlatformConfig(
            enabled=True,
            token="tok",
            extra={"base_url": "https://aops.example.com", "log_retention_days": 3},
        )
    )

    adapter._log_wire("info", direction="out", action="ws.send", payload={"event": "pong"})

    assert not old_log.exists()


def test_aops_log_retention_days_can_be_overridden_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AOPS_LOG_RETENTION_DAYS", "3")
    log_dir = tmp_path / "logs" / "aops"
    log_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    old_log = log_dir / f"aops-{(today - timedelta(days=4)).isoformat()}.log"
    old_log.write_text("old\n", encoding="utf-8")

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    adapter._log_wire("info", direction="out", action="ws.send", payload={"event": "pong"})

    assert not old_log.exists()


@pytest.mark.asyncio
async def test_aops_agent_registration_request_and_response_are_written_to_unified_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    fake_session.get_responses.append(_FakeResponse(payload={"id": "bot-001", "name": "AOPS Bot"}))
    adapter = AopsAdapter(
        PlatformConfig(
            enabled=True,
            token="tok",
            extra={
                "base_url": "https://aops.example.com",
                "agent_routes": {"devops": {"workspace": "~/.hermes/devops"}},
            },
        )
    )
    adapter._session = fake_session

    bot_info = await adapter._fetch_bot_me(request_kwargs={})
    adapter._bot_id = bot_info["id"]
    await adapter._report_agents(request_kwargs={})

    lines = _read_aops_log_lines(tmp_path)
    events = _aops_log_events(tmp_path)
    assert events == [
        "http.bot_me.request",
        "http.bot_me.response",
        "http.agent_report.request",
        "http.agent_report.response",
    ]
    agent_request_raw = _aops_log_raw(lines[2])
    assert agent_request_raw["method"] == "POST"
    assert agent_request_raw["body"]["botId"] == "bot-001"
    assert agent_request_raw["body"]["agents"][0]["id"] == "devops"
    assert _aops_log_raw(lines[3])["status"] == 200


def test_message_posted_maps_to_message_event_and_agent_route():
    adapter = AopsAdapter(
        PlatformConfig(
            enabled=True,
            token="tok",
            extra={
                "base_url": "https://aops.example.com",
                "trusted_agent_key_from": ["user-001"],
                "agent_routes": {
                    "devops": {
                        "model": "openrouter/anthropic/claude-sonnet-4",
                        "provider": "openrouter",
                        "prompt": "You are the DevOps-focused Hermes route.",
                    }
                },
            },
        )
    )
    event = adapter._build_message_event(
        {
            "id": "msg-1",
            "userId": "user-001",
            "userName": "Alice",
            "text": "check cpu",
            "channelId": "user-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "agentKey": "devops",
        }
    )
    assert event is not None
    assert event.source.chat_type == "dm"
    assert event.source.user_id == "user-001"
    assert event.route_overrides == {
        "model": "openrouter/anthropic/claude-sonnet-4",
        "provider": "openrouter",
    }
    assert event.channel_prompt == "You are the DevOps-focused Hermes route."


def test_group_channel_type_maps_to_group_and_bot_messages_are_ignored():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    group_event = adapter._build_message_event(
        {
            "id": "msg-2",
            "userId": "user-002",
            "text": "hello",
            "channelId": "group-123",
            "channelType": "group",
        }
    )
    assert group_event is not None
    assert group_event.source.chat_type == "group"

    adapter._bot_id = "bot-001"
    assert adapter._build_message_event(
        {
            "id": "msg-3",
            "userId": "bot-001",
            "text": "loop",
            "channelId": "user-001",
            "channelType": "direct",
        }
    ) is None


@pytest.mark.asyncio
async def test_aops_inbound_attachment_downloads_to_media_event(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    fake_session.get_responses.append(
        _FakeResponse(
            body=b"\x89PNG\r\n\x1a\nfake-png",
            headers={"Content-Type": "image/png", "Content-Length": "16"},
        )
    )
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._session = fake_session

    event = adapter._build_message_event(
        {
            "id": "msg-attach-1",
            "userId": "user-001",
            "text": "请分析附件",
            "channelId": "user-001",
            "channelType": "direct",
            "attachments": [
                {
                    "fileId": "cms_file_001",
                    "fileName": "告警截图.png",
                    "mimeType": "image/png",
                    "fileType": "image",
                    "size": 16,
                    "downloadUrl": "/api/v1/attachments/cms_file_001/download",
                }
            ],
        }
    )

    await adapter._attach_inbound_attachments(event)

    assert event.message_type == MessageType.PHOTO
    assert event.media_types == ["image/png"]
    assert len(event.media_urls) == 1
    assert Path(event.media_urls[0]).exists()
    assert fake_session.get_calls[0][0] == "https://aops.example.com/api/v1/attachments/cms_file_001/download"
    assert fake_session.get_calls[0][1]["headers"]["Authorization"] == "Bearer tok"
    assert "tec-client-ip" in fake_session.get_calls[0][1]["headers"]
    actions = _aops_log_events(tmp_path)
    assert actions == ["http.attachment.request", "http.attachment.response"]


@pytest.mark.asyncio
async def test_aops_inbound_attachment_uses_file_id_download_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    fake_session.get_responses.append(
        _FakeResponse(
            body=b"hello",
            headers={"Content-Type": "text/plain", "Content-Length": "5"},
        )
    )
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._session = fake_session

    event = adapter._build_message_event(
        {
            "id": "msg-attach-2",
            "userId": "user-001",
            "text": "看附件",
            "channelId": "user-001",
            "attachments": [{"fileId": "file with space", "fileName": "note.txt"}],
        }
    )

    await adapter._attach_inbound_attachments(event)

    assert event.message_type == MessageType.DOCUMENT
    assert event.media_types == ["text/plain"]
    assert fake_session.get_calls[0][0] == "https://aops.example.com/api/v1/attachments/file%20with%20space/download"


@pytest.mark.asyncio
async def test_aops_silent_inbound_attachment_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._session = fake_session

    event = adapter._build_message_event(
        {
            "id": "msg-silent-attach",
            "userId": "user-001",
            "text": "/bash clawhub explore --json",
            "channelId": "user-001",
            "silent": True,
            "attachments": [{"fileId": "cms_file_001", "fileName": "a.png"}],
        }
    )

    await adapter._attach_inbound_attachments(event)

    assert event.media_urls == []
    assert fake_session.get_calls == []
    assert _read_aops_log_lines(tmp_path) == []


@pytest.mark.asyncio
async def test_aops_inbound_attachment_failure_keeps_text_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_ws = _FakeWebSocket()
    fake_session = _FakeClientSession(fake_ws)
    fake_session.get_responses.append(_FakeResponse(status=403, text='{"status":403}'))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._session = fake_session

    event = adapter._build_message_event(
        {
            "id": "msg-attach-fail",
            "userId": "user-001",
            "text": "附件失败也要处理文本",
            "channelId": "user-001",
            "attachments": [{"fileId": "cms_file_403", "fileName": "secret.pdf"}],
        }
    )

    await adapter._attach_inbound_attachments(event)

    assert "附件失败也要处理文本" in event.text
    assert "下载失败" in event.text
    assert "无法识别" in event.text
    assert event.message_type == MessageType.TEXT
    assert event.media_urls == []
    assert _aops_log_events(tmp_path) == ["http.attachment.request", "http.attachment.response"]


def test_aops_extracts_user_content_from_messages_json():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-json-text",
            "userId": "user-001",
            "text": _aops_messages_text("系统提示", "识别图片问题"),
            "channelId": "user-001",
        }
    )

    assert event is not None
    assert event.text == "识别图片问题"
    assert event.channel_prompt == "系统提示"
    assert event.raw_message["metadata"]["aopsRawTextWasMessages"] is True
    assert event.raw_message["metadata"]["aopsMessagesSystemPrompt"] == "系统提示"


def test_aops_messages_json_preserves_route_prompt_and_system_context():
    adapter = AopsAdapter(
        PlatformConfig(
            enabled=True,
            token="tok",
            extra={
                "base_url": "https://aops.example.com",
                "trusted_agent_key_from": ["user-001"],
                "agent_routes": {
                    "oma": {
                        "prompt": "路由提示",
                    },
                },
            },
        )
    )

    event = adapter._build_message_event(
        {
            "id": "msg-json-route-text",
            "userId": "user-001",
            "agentKey": "oma",
            "text": _aops_messages_text("系统提示里包含报错详情", "请帮我分析提交的1条报错信息。"),
            "channelId": "user-001",
        }
    )

    assert event is not None
    assert event.text == "请帮我分析提交的1条报错信息。"
    assert event.channel_prompt == "路由提示\n\n系统提示里包含报错详情"


def test_aops_plain_text_is_not_marked_as_messages_json():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-plain-text",
            "userId": "user-001",
            "text": "普通问题",
            "channelId": "user-001",
        }
    )

    assert event is not None
    assert event.text == "普通问题"
    assert "aopsRawTextWasMessages" not in event.raw_message.get("metadata", {})


@pytest.mark.asyncio
async def test_aops_send_emits_start_then_end():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send("user-001", "hello", reply_to="msg-1")

    assert result.success is True
    assert adapter.send_reply_event.await_count == 2
    start = adapter.send_reply_event.await_args_list[0].args[0]
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert start["phase"] == "start"
    assert end["phase"] == "end"
    assert end["text"] == "hello"
    assert end["replyToId"] == "msg-1"


@pytest.mark.asyncio
async def test_aops_bridge_emits_segmented_stream_events():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    adapter.create_message_id = MagicMock(side_effect=["botmsg-1", "botmsg-2"])
    bridge = AopsLiveReplyBridge(adapter, chat_id="user-001", reply_to_id="msg-1", run_id="run-1")

    task = asyncio.create_task(bridge.run())
    bridge.on_delta("hello")
    bridge.on_tool_progress("tool.started", "exec", "pwd", {})
    bridge.on_delta(None)
    bridge.on_delta("next")
    bridge.send_final("next", conversation_ended=True)
    bridge.finish()
    await task

    events = [call.args[0] for call in adapter.send_reply_event.await_args_list]
    phases = [event["phase"] for event in events]
    assert phases == ["start", "delta", "tool", "end", "start", "delta", "end"]
    assert events[0]["messageId"] == "botmsg-1"
    assert events[3]["messageId"] == "botmsg-1"
    assert events[4]["messageId"] == "botmsg-2"
    assert events[6]["messageId"] == "botmsg-2"
    assert events[0]["seq"] == 1
    assert events[3]["seq"] == 4
    assert events[4]["seq"] == 1
    assert events[6]["conversationEnded"] is True


@pytest.mark.asyncio
async def test_aops_bridge_final_uses_latest_title():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    bridge = AopsLiveReplyBridge(adapter, chat_id="user-001", reply_to_id="msg-1", run_id="run-1")

    task = asyncio.create_task(bridge.run())
    bridge.update_title("CPU 告警排查")
    bridge.send_final("done", conversation_ended=True)
    bridge.finish()
    await task

    events = [call.args[0] for call in adapter.send_reply_event.await_args_list]
    assert events[-1]["phase"] == "end"
    assert events[-1]["title"] == "CPU 告警排查"


@pytest.mark.asyncio
async def test_aops_bridge_drops_internal_thinking_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    bridge = AopsLiveReplyBridge(adapter, chat_id="user-001", reply_to_id="msg-1", run_id="run-1")

    await bridge._emit_tool({
        "event_type": "reasoning.available",
        "tool_name": "_thinking",
        "preview": "测试收到。我是 数智运维专家。",
        "args": None,
    })
    await bridge._emit_tool({
        "event_type": "_thinking",
        "tool_name": None,
        "preview": "internal state",
        "args": {},
    })

    adapter.send_reply_event.assert_not_awaited()
    lines = _read_aops_log_lines(tmp_path)
    assert [_aops_log_field(line, "io") for line in lines] == ["send", "send"]
    assert [_aops_log_field(line, "messageType") for line in lines] == ["filtered", "filtered"]
    assert [_aops_log_field(line, "status") for line in lines] == ["skipped", "skipped"]
    assert _aops_log_raw(lines[0])["data"]["event_type"] == "reasoning.available"
    assert _aops_log_raw(lines[0])["data"]["preview"] == "测试收到。我是 数智运维专家。"
    assert 'text="filtered reason=internal_thinking tool=_thinking"' in lines[0]


@pytest.mark.asyncio
async def test_aops_bridge_still_emits_real_tool_progress():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    bridge = AopsLiveReplyBridge(adapter, chat_id="user-001", reply_to_id="msg-1", run_id="run-1")

    await bridge._emit_tool({
        "event_type": "tool.started",
        "tool_name": "exec",
        "preview": "pwd",
        "args": {},
    })

    adapter.send_reply_event.assert_awaited()
    events = [call.args[0] for call in adapter.send_reply_event.await_args_list]
    assert [event["phase"] for event in events] == ["start", "tool"]
    assert events[1]["kind"] == "tool"
    assert events[1]["tool"]["name"] == "exec"
    assert events[1]["text"] == "pwd"


@pytest.mark.asyncio
async def test_send_exec_approval_puts_approval_in_end_content(monkeypatch):
    monkeypatch.setattr("gateway.platforms.aops.time.time", lambda: 1760000000)
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    await adapter.send_exec_approval(
        chat_id="user-001",
        command="rm -rf /tmp/foo",
        session_key="session-1",
        description="dangerous command",
        metadata={"reply_to": "msg-1"},
    )
    action_payload = adapter.send_reply_event.await_args.args[0]
    approval = action_payload["content"][0]
    assert action_payload["phase"] == "actions"
    assert action_payload["kind"] == "approval"
    assert action_payload["text"] == ""
    assert action_payload["replyToId"] == "msg-1"
    assert approval["type"] == "approval"
    assert approval["approvalKind"] == "exec"
    assert _approval_commands(approval["allowedActions"]) == ["/approve", "/approve always", "/deny"]
    assert _approval_displays(approval["allowedActions"]) == ["仅本次允许", "始终允许", "拒绝"]
    assert approval["expiresAtMs"] > 1760000000000
    assert "检测到需要审批" in approval["message"]
    assert approval["replyContent"] == approval["message"]
    assert "rm -rf /tmp/foo" in approval["message"]


@pytest.mark.asyncio
async def test_send_exec_approval_without_permanent_uses_session_action():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    await adapter.send_exec_approval(
        chat_id="user-001",
        command="apply_patch",
        session_key="session-1",
        description="Codex requests to apply a patch",
        metadata={"allow_permanent": False},
    )

    action_payload = adapter.send_reply_event.await_args.args[0]
    approval = action_payload["content"][0]
    assert action_payload["phase"] == "actions"
    assert action_payload["kind"] == "approval"
    assert _approval_commands(approval["allowedActions"]) == ["/approve", "/approve session", "/deny"]
    assert _approval_displays(approval["allowedActions"]) == ["仅本次允许", "本会话允许", "拒绝"]
    assert approval["allowPermanent"] is False
    assert approval["replyContent"] == approval["message"]
    assert "本会话允许" in approval["message"]
    assert "始终允许" not in approval["message"]


@pytest.mark.asyncio
async def test_send_slash_confirm_puts_approval_actions_in_content():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send_slash_confirm(
        chat_id="conv-001",
        title="/new",
        message="Confirm /new",
        session_key="session-1",
        confirm_id="confirm-1",
        metadata={"reply_to": "msg-1"},
    )

    assert result.success is True
    action_payload = adapter.send_reply_event.await_args.args[0]
    approval = action_payload["content"][0]
    assert action_payload["phase"] == "actions"
    assert action_payload["kind"] == "approval"
    assert action_payload["text"] == ""
    assert action_payload["replyToId"] == "msg-1"
    assert approval["type"] == "approval"
    assert approval["approvalKind"] == "slash"
    assert _approval_commands(approval["allowedActions"]) == ["/approve", "/always", "/cancel"]
    assert _approval_displays(approval["allowedActions"]) == ["执行本次", "始终执行", "取消"]
    assert approval["id"] == "confirm-1"
    assert approval["message"] == "Confirm /new"
    assert approval["replyContent"] == "Confirm /new"


def test_aops_approval_action_payload_maps_to_approve_command():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-approval-1",
            "userId": "user-001",
            "userName": "Alice",
            "text": "",
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "content": {"type": "approval", "id": "approval-1", "action": "allow-always"},
        }
    )

    assert event is not None
    assert event.text == "/approve always"
    assert event.raw_message["metadata"]["aopsApprovalAction"] is True
    assert event.raw_message["metadata"]["aopsApprovalId"] == "approval-1"


def test_aops_approval_action_payload_accepts_direct_command_value():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-approval-direct",
            "userId": "user-001",
            "userName": "Alice",
            "text": "",
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "content": {"type": "approval", "id": "approval-1", "action": "/approve always"},
        }
    )

    assert event is not None
    assert event.text == "/approve always"
    assert event.raw_message["metadata"]["aopsApprovalAction"] is True
    assert event.raw_message["metadata"]["aopsApprovalId"] == "approval-1"


def test_aops_slash_approval_action_maps_to_slash_confirm_command():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-approval-slash",
            "userId": "user-001",
            "userName": "Alice",
            "text": "",
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "content": {
                "type": "approval",
                "approvalKind": "slash",
                "id": "confirm-1",
                "action": "allow-always",
            },
        }
    )

    assert event is not None
    assert event.text == "/always"
    assert event.raw_message["metadata"]["aopsApprovalAction"] is True
    assert event.raw_message["metadata"]["aopsApprovalId"] == "confirm-1"


def test_aops_slash_approval_action_accepts_direct_command_value():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-approval-slash-direct",
            "userId": "user-001",
            "userName": "Alice",
            "text": "",
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "content": {
                "type": "approval",
                "approvalKind": "slash",
                "id": "confirm-1",
                "action": "/always",
            },
        }
    )

    assert event is not None
    assert event.text == "/always"
    assert event.raw_message["metadata"]["aopsApprovalAction"] is True
    assert event.raw_message["metadata"]["aopsApprovalId"] == "confirm-1"


def test_aops_approval_action_metadata_maps_to_deny_command():
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))

    event = adapter._build_message_event(
        {
            "id": "msg-approval-2",
            "userId": "user-001",
            "userName": "Alice",
            "text": "",
            "channelId": "conv-001",
            "channelType": "direct",
            "timestamp": "2026-04-09T08:00:00.000Z",
            "metadata": {"approvalId": "approval-2", "approvalAction": "deny"},
        }
    )

    assert event is not None
    assert event.text == "/deny"
    assert event.raw_message["metadata"]["aopsApprovalId"] == "approval-2"


def test_agent_report_payload_uses_agent_routes(monkeypatch):
    monkeypatch.setattr(
        aops_mod,
        "_build_aops_runtime_report",
        lambda: {
            "schema": "aops-runtime-report.v1",
            "host": {"ips": ["10.0.0.8"]},
            "user": {"systemUser": "hermes"},
            "hermes": {"home": "/home/hermes/.hermes", "profile": "default"},
            "model": {},
        },
    )
    adapter = AopsAdapter(
        PlatformConfig(
            enabled=True,
            token="tok",
            extra={
                "base_url": "https://aops.example.com",
                "agent_routes": {
                    "main": {"default": True, "workspace": "~/.hermes"},
                    "devops": {"workspace": "~/.hermes/devops"},
                },
            },
        )
    )
    adapter._bot_id = "bot-001"
    payload = adapter._build_agent_report_payload()
    assert payload["source"] == "hermes"
    assert payload["defaultAgentId"] == "main"
    assert payload["agents"] == [
        {"id": "main", "enabled": True, "default": True, "workspace": "~/.hermes"},
        {"id": "devops", "enabled": True, "default": False, "workspace": "~/.hermes/devops"},
    ]
    assert payload["runtime"]["schema"] == "aops-runtime-report.v1"
    assert payload["runtime"]["host"]["ips"] == ["10.0.0.8"]


def test_aops_host_ipv4_report_uses_ip_addr_and_filters_loopback(monkeypatch):
    payload = [
        {
            "ifname": "lo",
            "addr_info": [{"family": "inet", "local": "127.0.0.1"}],
        },
        {
            "ifname": "eth0",
            "addr_info": [
                {"family": "inet", "local": "192.168.10.23"},
                {"family": "inet6", "local": "fe80::1"},
            ],
        },
        {
            "ifname": "eth1",
            "addr_info": [
                {"family": "inet", "local": "10.10.0.18"},
                {"family": "inet", "local": "192.168.10.23"},
            ],
        },
    ]

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(aops_mod.subprocess, "run", fake_run)

    assert aops_mod._collect_host_ipv4s() == ["10.10.0.18", "192.168.10.23"]


def test_aops_host_ipv4_report_falls_back_to_socket(monkeypatch):
    monkeypatch.setattr(aops_mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(aops_mod.socket, "gethostname", lambda: "vm-aops-01")
    monkeypatch.setattr(aops_mod.socket, "getfqdn", lambda: "vm-aops-01.example.internal")

    def fake_getaddrinfo(name, *_args, **_kwargs):
        if name == "vm-aops-01":
            return [(None, None, None, None, ("127.0.0.1", 0)), (None, None, None, None, ("172.16.0.9", 0))]
        return [(None, None, None, None, ("10.0.0.5", 0))]

    monkeypatch.setattr(aops_mod.socket, "getaddrinfo", fake_getaddrinfo)

    assert aops_mod._collect_host_ipv4s() == ["10.0.0.5", "172.16.0.9"]


def test_aops_runtime_model_report_reads_config_api_key(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {
                "provider": "custom",
                "model": "qwen3-32b",
                "default": "qwen3-32b",
                "base_url": "http://llm-gateway.internal/v1",
                "api_mode": "openai",
                "api_key_env": "LLM_GATEWAY_TOKEN",
                "api_key": "sk-config",
            }
        },
    )

    report = aops_mod._build_aops_model_runtime_report()

    assert report == {
        "provider": "custom",
        "model": "qwen3-32b",
        "default": "qwen3-32b",
        "baseUrl": "http://llm-gateway.internal/v1",
        "apiMode": "openai",
        "apiKeyEnv": "LLM_GATEWAY_TOKEN",
        "apiKey": "sk-config",
    }


def test_aops_runtime_model_report_reads_config_api_key_env(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {
                "provider": "custom",
                "model": "qwen3-32b",
                "base_url": "http://llm-gateway.internal/v1",
                "api_key_env": "LLM_GATEWAY_TOKEN",
            }
        },
    )
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda key: "sk-env" if key == "LLM_GATEWAY_TOKEN" else "")

    report = aops_mod._build_aops_model_runtime_report()

    assert report["apiKeyEnv"] == "LLM_GATEWAY_TOKEN"
    assert report["apiKey"] == "sk-env"


def test_aops_runtime_model_report_falls_back_to_provider_credentials(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"provider": "openrouter", "default": "openai/gpt-4.1"}},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-provider",
            "source": "OPENROUTER_API_KEY",
        },
    )

    report = aops_mod._build_aops_model_runtime_report()

    assert report["provider"] == "openrouter"
    assert report["model"] == "openai/gpt-4.1"
    assert report["baseUrl"] == "https://openrouter.ai/api/v1"
    assert report["apiKey"] == "sk-provider"


def test_aops_agent_report_log_redacts_model_api_key():
    payload = {
        "method": "POST",
        "url": "https://aops.example.com/api/v1/bot/agents/report",
        "body": {
            "runtime": {
                "model": {
                    "apiKey": "sk-secret",
                    "apiKeyEnv": "LLM_GATEWAY_TOKEN",
                }
            }
        },
    }

    redacted = aops_mod._redact_aops_agent_report_for_log(payload)

    assert redacted["body"]["runtime"]["model"]["apiKey"] == "[REDACTED]"
    assert payload["body"]["runtime"]["model"]["apiKey"] == "sk-secret"


def test_aops_dm_policy_auth_open_allowlist_pairing_disabled(monkeypatch):
    monkeypatch.delenv("AOPS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("AOPS_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    source = SessionSource(platform=Platform.AOPS, user_id="user-001", chat_id="user-001", chat_type="dm")

    open_runner = _make_runner(extra={"dm_policy": "open"})
    assert open_runner._is_user_authorized(source) is True

    allow_runner = _make_runner(extra={"dm_policy": "allowlist", "allow_from": ["user-001"]})
    assert allow_runner._is_user_authorized(source) is True
    denied_runner = _make_runner(extra={"dm_policy": "allowlist", "allow_from": ["user-002"]})
    assert denied_runner._is_user_authorized(source) is False

    pairing_runner = _make_runner(extra={"dm_policy": "pairing"})
    pairing_runner.pairing_store.is_approved.return_value = False
    assert pairing_runner._is_user_authorized(source) is False
    pairing_runner.pairing_store.is_approved.return_value = True
    assert pairing_runner._is_user_authorized(source) is True

    disabled_runner = _make_runner(extra={"dm_policy": "disabled"})
    assert disabled_runner._is_user_authorized(source) is False


@pytest.mark.asyncio
async def test_aops_skills_local_command_returns_list_json(monkeypatch, tmp_path):
    import agent.skill_commands as skill_commands
    import tools.skills_tool as skills_tool

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "ops" / "restart-service"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Restart Service\n"
        "description: Restart a service safely.\n"
        "homepage: https://example.com/restart-service\n"
        "---\n"
        "# Restart Service\n",
        encoding="utf-8",
    )
    other_dir = skills_root / "ops" / "draft-notes"
    other_dir.mkdir(parents=True)
    (other_dir / "SKILL.md").write_text(
        "---\n"
        "name: Draft Notes\n"
        "description: Draft notes quickly.\n"
        "---\n"
        "# Draft Notes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(
        skill_commands,
        "scan_skill_commands",
        lambda: {
            "/restart-service": {
                "name": "Restart Service",
                "description": "Restart a service safely.",
                "skill_md_path": str(skill_dir / "SKILL.md"),
                "skill_dir": str(skill_dir),
            }
        },
    )
    monkeypatch.setattr(
        skill_commands,
        "get_skill_commands",
        lambda: {
            "/restart-service": {
                "name": "Restart Service",
                "description": "Restart a service safely.",
                "skill_md_path": str(skill_dir / "SKILL.md"),
                "skill_dir": str(skill_dir),
            }
        },
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/skills"))

    payload = json.loads(result)
    assert payload["schemaVersion"] == "local-command-list.v1"
    assert payload["type"] == "skills.list"
    assert payload["itemType"] == "skill"
    assert payload["count"] == 2
    items_by_id = {item["id"]: item for item in payload["items"]}
    assert items_by_id["ops/restart-service"]["name"] == "Restart Service"
    assert items_by_id["ops/restart-service"]["homepage"] == "https://example.com/restart-service"
    assert items_by_id["ops/restart-service"]["command"] == "/restart-service"
    assert items_by_id["ops/draft-notes"]["name"] == "Draft Notes"
    assert items_by_id["ops/draft-notes"]["command"] == "/draft-notes"

    result = await runner._handle_message(_make_aops_event("/skills list"))
    payload = json.loads(result)
    items_by_id = {item["id"]: item for item in payload["items"]}
    assert items_by_id["ops/restart-service"]["command"] == "/restart-service"
    assert items_by_id["ops/draft-notes"]["command"] == "/draft-notes"


@pytest.mark.asyncio
async def test_aops_toolsets_list_returns_dashboard_config_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hermes_config

    hermes_config.save_config({"platform_toolsets": {"cli": ["web", "terminal"]}})
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/toolsets list"))

    payload = json.loads(result)
    assert payload["schemaVersion"] == "local-command-list.v1"
    assert payload["type"] == "toolsets.list"
    assert payload["ok"] is True
    assert payload["itemType"] == "toolset"
    assert payload["context"]["platform"] == "aops"
    assert payload["context"]["agentId"] == "main"
    assert payload["context"]["profileName"] == "default"
    assert payload["context"]["configPath"] == str(tmp_path / "config.yaml")
    items_by_name = {item["name"]: item for item in payload["items"]}
    assert items_by_name["web"]["label"] == "Web Search & Scraping"
    assert items_by_name["web"]["enabled"] is True
    assert set(items_by_name["web"]["tools"]) == {"web_search", "web_extract"}
    assert items_by_name["terminal"]["enabled"] is True
    assert items_by_name["file"]["enabled"] is False
    assert payload["summary"]["enabled"] >= 2


@pytest.mark.asyncio
async def test_aops_toolsets_list_falls_back_to_legacy_aops_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hermes_config

    hermes_config.save_config({"platform_toolsets": {"aops": ["web", "terminal"]}})
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/toolsets list"))

    payload = json.loads(result)
    assert payload["ok"] is True
    items_by_name = {item["name"]: item for item in payload["items"]}
    assert items_by_name["web"]["enabled"] is True
    assert items_by_name["terminal"]["enabled"] is True


@pytest.mark.asyncio
async def test_aops_toolsets_set_updates_dashboard_and_legacy_platform_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hermes_config

    hermes_config.save_config({"platform_toolsets": {"aops": ["terminal"]}})
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/toolsets set web true"))

    payload = json.loads(result)
    assert payload["type"] == "toolsets.updated"
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["updated"] == {"name": "web", "enabled": True}
    assert payload["items"][0]["name"] == "web"
    assert payload["items"][0]["enabled"] is True
    cfg = hermes_config.load_config()
    assert "web" in cfg["platform_toolsets"]["cli"]
    assert "terminal" in cfg["platform_toolsets"]["cli"]
    assert "web" in cfg["platform_toolsets"]["aops"]
    assert "terminal" in cfg["platform_toolsets"]["aops"]


@pytest.mark.asyncio
async def test_aops_run_agent_uses_dashboard_toolset_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "openai", "api_key": "key", "base_url": "https://example.com", "api_mode": "responses"},
    )
    import hermes_cli.config as hermes_config

    hermes_config.save_config({"platform_toolsets": {"cli": ["terminal"], "aops": ["web", "terminal"]}})

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner(platform=Platform.AOPS, extra={})
    runner.config = GatewayConfig(platforms={})
    source = SessionSource(platform=Platform.AOPS, chat_id="chat", chat_name="AOPS", chat_type="dm", user_id="user-1")

    result = await runner._run_agent(
        message="ping",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:aops:dm",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    enabled_toolsets = set(_CapturingAgent.last_init["enabled_toolsets"])
    assert "terminal" in enabled_toolsets
    assert "web" not in enabled_toolsets


@pytest.mark.asyncio
async def test_aops_toolsets_unknown_name_returns_structured_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/toolsets enable unknown"))

    payload = json.loads(result)
    assert payload["type"] == "toolsets.updated"
    assert payload["ok"] is False
    assert payload["items"] == []
    assert payload["error"] == {
        "code": "TOOLSET_NOT_FOUND",
        "message": "Toolset `unknown` not found.",
    }


@pytest.mark.asyncio
async def test_aops_toolsets_context_uses_profile_and_metadata_agent_id(monkeypatch, tmp_path):
    profile_home = tmp_path / ".hermes" / "profiles" / "ops-2"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(
        _make_silent_aops_event("/toolsets list", metadata={"agentId": "ops-2"})
    )

    payload = json.loads(result)
    assert payload["context"]["agentId"] == "ops-2"
    assert payload["context"]["profileName"] == "ops-2"
    assert payload["context"]["profileHome"] == str(profile_home)


@pytest.mark.asyncio
async def test_aops_cron_local_command_returns_document_fields(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "job_description": "Daily report",
            "status": "ok",
            "started_at": "2026-05-08T09:00:00+00:00",
            "finished_at": "2026-05-08T09:01:30+00:00",
            "duration_ms": 90000,
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron"))

    payload = json.loads(result)
    item = payload["items"][0]
    assert payload["schemaVersion"] == "local-command-list.v1"
    assert payload["type"] == "cron.list"
    assert item["id"] == job["id"]
    assert item["description"] == "Daily report"
    assert item["createdAtMs"] is not None
    assert item["updatedAtMs"] is not None
    assert item["scheduleText"] == "every 60m"
    assert item["payloadKind"] == "agentTurn"
    assert item["lastDurationMs"] == 90000
    assert item["lastDeliveryStatus"] == "not-requested"


@pytest.mark.asyncio
async def test_aops_cron_local_command_accepts_duration_aliases(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "job_description": "Daily report",
            "status": "ok",
            "started_at": "2026-05-08T09:00:00Z",
            "finished_at": "2026-05-08T09:01:30Z",
            "durationMs": "90000",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron"))

    payload = json.loads(result)
    assert payload["items"][0]["lastDurationMs"] == 90000


@pytest.mark.asyncio
async def test_aops_cron_local_command_hides_disabled_next_run(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Stand up", schedule="every 1m", name="站立提醒")
    jobs = cron_jobs.load_jobs()
    jobs[0]["enabled"] = False
    jobs[0]["state"] = "paused"
    jobs[0]["next_run_at"] = "2026-06-05T10:00:00+00:00"
    cron_jobs.save_jobs(jobs)

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron"))

    payload = json.loads(result)
    assert payload["items"][0]["id"] == job["id"]
    assert payload["items"][0]["enabled"] is False
    assert payload["items"][0]["state"] == "paused"
    assert payload["items"][0]["nextRunAtMs"] is None


@pytest.mark.asyncio
async def test_aops_cron_local_command_flags_once_schedule_as_one_shot(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Stand up", schedule="1m", name="站立提醒")
    jobs = cron_jobs.load_jobs()
    jobs[0]["repeat"]["times"] = None
    cron_jobs.save_jobs(jobs)

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron"))

    payload = json.loads(result)
    item = payload["items"][0]
    assert item["id"] == job["id"]
    assert item["scheduleKind"] == "once"
    assert item["isOneShot"] is True
    assert item["deleteAfterRun"] is True
    assert "once schedule" in item["scheduleWarning"]


@pytest.mark.asyncio
async def test_aops_cron_local_command_describes_script_job_without_prompt(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="", schedule="every 1h", name="CPU watchdog", script="cpu_watch.sh", no_agent=True)

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron"))

    payload = json.loads(result)
    assert payload["items"][0]["id"] == job["id"]
    assert payload["items"][0]["description"] == "CPU watchdog"


@pytest.mark.asyncio
async def test_aops_cron_history_returns_structured_list(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    next_run_at = "2026-05-08T10:00:00+00:00"
    jobs = cron_jobs.load_jobs()
    jobs[0]["next_run_at"] = next_run_at
    cron_jobs.save_jobs(jobs)
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "job_description": "Daily report",
            "status": "ok",
            "started_at": "2026-05-08T09:00:00+00:00",
            "finished_at": "2026-05-08T09:01:00+00:00",
            "duration_ms": 60000,
            "session_id": "session-1",
            "session_key": "aops:conv-1:main",
            "model": "aops-model",
            "provider": "custom",
            "usage": {"inputTokens": 12, "outputTokens": 5},
            "response_preview": "报告已生成",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job['id']}"))

    payload = json.loads(result)
    assert payload["type"] == "cron.history.list"
    assert payload["itemType"] == "cron.run"
    assert payload["context"]["jobId"] == job["id"]
    assert payload["summary"]["task"]["id"] == job["id"]
    assert payload["summary"]["task"]["description"] == "Daily report"
    assert payload["items"][0]["jobId"] == job["id"]
    assert payload["items"][0]["description"] == "Daily report"
    assert payload["items"][0]["summary"] == "报告已生成"
    assert payload["items"][0]["durationMs"] == 60000
    assert payload["items"][0]["nextRunAtMs"] == int(datetime.fromisoformat(next_run_at).timestamp() * 1000)
    assert payload["items"][0]["usage"] == {"inputTokens": 12, "outputTokens": 5, "durationMs": 60000}
    assert payload["items"][0]["sessionId"] == "session-1"
    assert payload["items"][0]["sessionKey"] == "aops:conv-1:main"
    assert payload["items"][0]["model"] == "aops-model"
    assert payload["items"][0]["provider"] == "custom"


@pytest.mark.asyncio
async def test_aops_cron_history_falls_back_to_job_description_when_history_missing_description(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="", schedule="every 1h", name="CPU watchdog", script="cpu_watch.sh", no_agent=True)
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "status": "ok",
            "started_at": "2026-05-08T09:00:00+00:00",
            "finished_at": "2026-05-08T09:01:00+00:00",
            "response_preview": "CPU 正常",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job['id']}"))

    payload = json.loads(result)
    assert payload["summary"]["task"]["description"] == "CPU watchdog"
    assert payload["items"][0]["description"] == "CPU watchdog"


@pytest.mark.asyncio
async def test_aops_cron_history_prefers_recorded_next_run(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    recorded_next_run = 1778238000000
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "job_description": "Daily report",
            "status": "ok",
            "startedAt": "2026-05-08T09:00:00Z",
            "finishedAt": "2026-05-08T09:01:00Z",
            "durationMs": "60000",
            "nextRunAtMs": recorded_next_run,
            "response_preview": "报告已生成",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job['id']}"))

    payload = json.loads(result)
    assert payload["items"][0]["durationMs"] == 60000
    assert payload["items"][0]["nextRunAtMs"] == recorded_next_run
    assert payload["items"][0]["usage"] == {"durationMs": 60000}


@pytest.mark.asyncio
async def test_aops_cron_history_returns_newest_first(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    for hour, summary in [(9, "first"), (10, "second"), (11, "third")]:
        cron_jobs.append_cron_history(
            {
                "job_id": job["id"],
                "job_name": job["name"],
                "job_description": "Daily report",
                "status": "ok",
                "started_at": f"2026-05-08T{hour:02d}:00:00+00:00",
                "finished_at": f"2026-05-08T{hour:02d}:01:00+00:00",
                "duration_ms": 60000,
                "response_preview": summary,
            }
        )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job['id']}"))

    payload = json.loads(result)
    assert [item["summary"] for item in payload["items"]] == ["third", "second", "first"]

    anchor_ts = payload["items"][1]["ts"]
    result = await runner._handle_message(_make_aops_event(f"/cron history after {job['id']} {anchor_ts}"))

    payload = json.loads(result)
    assert [item["summary"] for item in payload["items"]] == ["third"]


@pytest.mark.asyncio
async def test_aops_cron_history_explains_deleted_one_shot_with_history(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job_id = "deleted-once-1"
    cron_jobs.append_cron_history(
        {
            "job_id": job_id,
            "job_name": "Stand up once",
            "job_description": "1分钟后提醒我站起来",
            "status": "ok",
            "started_at": "2026-05-08T09:00:00+00:00",
            "finished_at": "2026-05-08T09:01:00+00:00",
            "duration_ms": 60000,
            "response_preview": "站起来活动一下。",
            "schedule_display": "once in 1m",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job_id}"))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["context"]["jobDeleted"] is True
    assert payload["summary"]["jobDeleted"] is True
    assert "执行后已自动删除" in payload["summary"]["message"]
    assert payload["items"][0]["summary"] == "站起来活动一下。"


@pytest.mark.asyncio
async def test_aops_cron_history_falls_back_to_saved_output(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job_id = "output-only-1"
    output_dir = Path(cron_jobs.OUTPUT_DIR) / job_id
    output_dir.mkdir(parents=True)
    output_file = output_dir / "20260508_090000.md"
    output_file.write_text("定时任务已执行，输出来自保存文件。", encoding="utf-8")

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron history {job_id}"))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["context"]["jobDeleted"] is True
    assert payload["items"][0]["jobId"] == job_id
    assert payload["items"][0]["summary"] == "定时任务已执行，输出来自保存文件。"
    assert payload["items"][0]["outputPath"] == str(output_file)


@pytest.mark.asyncio
async def test_aops_cron_list_works_while_agent_running(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")

    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event("/cron")
    session_key = runner._session_key_for_source(event.source)
    running_agent = _RunningAgent()
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts[session_key] = time.time()

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "cron.list"
    assert payload["items"][0]["id"] == job["id"]
    assert running_agent.interrupts == []


@pytest.mark.asyncio
async def test_aops_cron_history_works_while_agent_running(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    cron_jobs.append_cron_history(
        {
            "job_id": job["id"],
            "job_name": job["name"],
            "job_description": "Daily report",
            "status": "ok",
            "started_at": "2026-05-08T09:00:00+00:00",
            "finished_at": "2026-05-08T09:01:00+00:00",
            "duration_ms": 60000,
            "response_preview": "报告已生成",
        }
    )

    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event(f"/cron history {job['id']}")
    session_key = runner._session_key_for_source(event.source)
    running_agent = _RunningAgent()
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts[session_key] = time.time()

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "cron.history.list"
    assert payload["items"][0]["summary"] == "报告已生成"
    assert running_agent.interrupts == []


@pytest.mark.asyncio
async def test_aops_cron_remove_deletes_job(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")
    output_dir = Path(cron_jobs.OUTPUT_DIR) / job["id"]
    output_dir.mkdir(parents=True)
    (output_dir / "old.md").write_text("old output", encoding="utf-8")

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event(f"/cron remove {job['id']}"))

    payload = json.loads(result)
    assert payload["type"] == "cron.removed"
    assert payload["ok"] is True
    assert payload["removed"] is True
    assert payload["task"]["id"] == job["id"]
    assert payload["context"]["jobId"] == job["id"]
    assert cron_jobs.get_job(job["id"]) is None
    assert not output_dir.exists()


@pytest.mark.asyncio
async def test_aops_cron_remove_accepts_silent_message(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    job = cron_jobs.create_job(prompt="Daily report", schedule="every 1h", name="Daily report")

    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event(f"/cron remove {job['id']}"))

    payload = json.loads(result)
    assert payload["type"] == "cron.removed"
    assert payload["ok"] is True
    assert cron_jobs.get_job(job["id"]) is None


@pytest.mark.asyncio
async def test_aops_cron_remove_returns_structured_usage_error(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron remove"))

    payload = json.loads(result)
    assert payload["type"] == "cron.removed"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CRON_REMOVE_MISSING_REF"
    assert payload["context"]["jobRef"] is None


@pytest.mark.asyncio
async def test_aops_cron_remove_returns_not_found(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron remove missing-job"))

    payload = json.loads(result)
    assert payload["type"] == "cron.removed"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CRON_JOB_NOT_FOUND"
    assert payload["context"]["jobRef"] == "missing-job"


@pytest.mark.asyncio
async def test_aops_cron_remove_refuses_ambiguous_name(monkeypatch, tmp_path):
    import cron.jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "HISTORY_FILE", tmp_path / "cron" / "history.jsonl")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    first = cron_jobs.create_job(prompt="A", schedule="every 1h", name="dup")
    second = cron_jobs.create_job(prompt="B", schedule="every 1h", name="dup")
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/cron remove dup"))

    payload = json.loads(result)
    assert payload["type"] == "cron.removed"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CRON_REMOVE_AMBIGUOUS_REF"
    assert {item["id"] for item in payload["matches"]} == {first["id"], second["id"]}
    assert cron_jobs.get_job(first["id"]) is not None
    assert cron_jobs.get_job(second["id"]) is not None


@pytest.mark.asyncio
async def test_aops_blocked_command_is_rejected_and_hidden_from_help():
    runner = _make_runner(extra={"dm_policy": "open", "blocked_commands": ["gateway"]})

    blocked = await runner._handle_message(_make_aops_event("/gateway status"))
    help_text = await runner._handle_message(_make_aops_event("/help"))

    assert "blocked by AOPS config" in blocked
    payload = json.loads(help_text)
    full_commands = {item["fullCommand"] for item in payload["items"]}
    assert "/help" in full_commands
    assert "/skills" in full_commands
    assert "/curator" in full_commands
    assert "/gateway" not in full_commands


@pytest.mark.asyncio
async def test_aops_help_returns_structured_tree_with_dangerous_flag():
    import agent.skill_commands as skill_commands

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        skill_commands,
        "get_skill_commands",
        lambda: {
            "/test-skill": {
                "name": "Test Skill",
                "description": "Run the test skill",
                "skill_md_path": "/tmp/test-skill/SKILL.md",
            }
        },
    )
    runner = _make_runner(
        extra={
            "dm_policy": "open",
            "dangerous_commands": ["/curator run", "/test-skill"],
        }
    )

    try:
        result = await runner._handle_message(_make_aops_event("/help"))
    finally:
        monkeypatch.undo()

    payload = json.loads(result)
    assert payload["schemaVersion"] == "local-command-tree.v2"
    assert payload["type"] == "command.tree"
    curator = next(item for item in payload["items"] if item["fullCommand"] == "/curator")
    fast = next(item for item in payload["items"] if item["fullCommand"] == "/fast")
    skill = next(item for item in payload["items"] if item["fullCommand"] == "/test-skill")
    run_child = next(child for child in curator["children"] if child["command"] == "run")
    pin_child = next(child for child in curator["children"] if child["command"] == "pin")
    assert curator["executable"] is False
    assert run_child["dangerous"] is True
    assert run_child["executable"] is True
    assert skill["dangerous"] is True
    assert skill["usage"] == "/test-skill [prompt]"
    assert skill["executable"] is True
    assert pin_child["executable"] is False
    assert pin_child["completions"] == [
        {
            "name": "skill",
            "description": "技能名称。",
            "required": True,
            "choices": [],
        }
    ]
    assert fast["completions"] == [
        {
            "name": "mode",
            "description": "快速模式选项。",
            "required": False,
            "choices": [
                {"value": "normal", "description": "切换到普通模式。"},
                {"value": "fast", "description": "切换到快速模式。"},
                {"value": "status", "description": "显示快速模式状态。"},
                {"value": "on", "description": "开启快速模式。"},
                {"value": "off", "description": "关闭快速模式。"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_aops_help_uses_structured_required_flags_for_cron_history():
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/help"))

    payload = json.loads(result)
    cron = next(item for item in payload["items"] if item["fullCommand"] == "/cron")
    remove = next(child for child in cron["children"] if child["command"] == "remove")
    history = next(child for child in cron["children"] if child["command"] == "history")
    after = next(child for child in history["children"] if child["command"] == "after")
    assert remove["usage"] == "/cron remove <id|name>"
    assert remove["completions"] == [
        {
            "name": "idOrName",
            "description": "定时任务 ID 或唯一名称。",
            "required": True,
            "choices": [],
        },
    ]
    assert history["completions"] == [
        {
            "name": "id",
            "description": "定时任务 ID。",
            "required": True,
            "choices": [],
        },
        {
            "name": "tsMs",
            "description": "历史锚点时间戳（毫秒）。",
            "required": False,
            "choices": [],
        },
    ]
    assert after["completions"] == [
        {
            "name": "id",
            "description": "定时任务 ID。",
            "required": True,
            "choices": [],
        },
        {
            "name": "tsMs",
            "description": "历史锚点时间戳（毫秒）。",
            "required": True,
            "choices": [],
        },
    ]


@pytest.mark.asyncio
async def test_aops_security_command_updates_approval_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/security set smart"))

    payload = json.loads(result)
    assert payload["type"] == "security.updated"
    assert payload["ok"] is True
    assert payload["current"]["mode"] == "smart"
    import hermes_cli.config as hermes_config

    cfg = hermes_config.load_config()
    assert cfg["approvals"]["mode"] == "smart"
    assert cfg["approvals"]["destructive_slash_confirm"] is True
    assert payload["destructiveSlashConfirm"] is True


@pytest.mark.asyncio
async def test_aops_security_set_off_disables_destructive_slash_confirm(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/security set off"))

    payload = json.loads(result)
    assert payload["type"] == "security.updated"
    assert payload["current"]["mode"] == "off"
    assert payload["destructiveSlashConfirm"] is False
    import hermes_cli.config as hermes_config

    cfg = hermes_config.load_config()
    assert cfg["approvals"]["mode"] == "off"
    assert cfg["approvals"]["destructive_slash_confirm"] is False


@pytest.mark.asyncio
async def test_aops_security_set_manual_reenables_destructive_slash_confirm(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hermes_config

    hermes_config.save_config({"approvals": {"mode": "off", "destructive_slash_confirm": False}})
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/security set manual"))

    payload = json.loads(result)
    assert payload["current"]["mode"] == "manual"
    assert payload["destructiveSlashConfirm"] is True
    cfg = hermes_config.load_config()
    assert cfg["approvals"]["mode"] == "manual"
    assert cfg["approvals"]["destructive_slash_confirm"] is True


@pytest.mark.asyncio
async def test_aops_securty_alias_sets_security_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/securty set off"))

    payload = json.loads(result)
    assert payload["type"] == "security.updated"
    assert payload["current"]["mode"] == "off"
    assert payload["destructiveSlashConfirm"] is False


@pytest.mark.asyncio
async def test_aops_help_includes_security_command():
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/help"))

    payload = json.loads(result)
    security = next(item for item in payload["items"] if item["fullCommand"] == "/security")
    assert security["type"] == "configuration"
    assert security["executable"] is True
    set_child = next(child for child in security["children"] if child["command"] == "set")
    assert set_child["usage"] == "/security set <off|manual|smart>"


@pytest.mark.asyncio
async def test_aops_help_includes_toolsets_command():
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/help"))

    payload = json.loads(result)
    toolsets_node = next(item for item in payload["items"] if item["fullCommand"] == "/toolsets")
    assert toolsets_node["type"] == "custom"
    assert toolsets_node["executable"] is True
    set_child = next(child for child in toolsets_node["children"] if child["command"] == "set")
    assert set_child["usage"] == "/toolsets set <name> <true|false>"
    assert set_child["completions"][1]["choices"] == [
        {"value": "true", "description": "启用。"},
        {"value": "false", "description": "关闭。"},
    ]


@pytest.mark.asyncio
async def test_aops_help_includes_registered_extension_command():
    from gateway import aops_commands

    aops_commands.register_aops_command_extension(
        aops_commands.AopsCommandExtension(
            name="diagnose",
            type="tools",
            description="运行诊断指令。",
            usage="/diagnose <target>",
            executable=False,
            completions=[
                {
                    "name": "target",
                    "description": "诊断目标。",
                    "required": True,
                    "choices": [],
                }
            ],
            children=[
                {
                    "command": "status",
                    "description": "查看诊断状态。",
                    "usage": "/diagnose status",
                    "executable": True,
                }
            ],
        )
    )
    try:
        runner = _make_runner(extra={"dm_policy": "open"})
        result = await runner._handle_message(_make_aops_event("/help"))
        payload = json.loads(result)
        diagnose = next(item for item in payload["items"] if item["fullCommand"] == "/diagnose")
        status = next(child for child in diagnose["children"] if child["command"] == "status")
        assert aops_commands.is_supported_command("diagnose") is True
        assert diagnose["type"] == "tools"
        assert diagnose["usage"] == "/diagnose <target>"
        assert diagnose["completions"][0]["name"] == "target"
        assert status["fullCommand"] == "/diagnose status"
        assert status["executable"] is True
    finally:
        aops_commands.unregister_aops_command_extension("diagnose")


@pytest.mark.asyncio
async def test_aops_help_hides_blocked_registered_extension_command():
    from gateway import aops_commands

    aops_commands.register_aops_command_extension(
        aops_commands.AopsCommandExtension(name="diagnose", description="运行诊断指令。")
    )
    try:
        runner = _make_runner(extra={"dm_policy": "open", "blocked_commands": ["/diagnose"]})
        result = await runner._handle_message(_make_aops_event("/help"))
        payload = json.loads(result)
        assert "/diagnose" not in {item["fullCommand"] for item in payload["items"]}
    finally:
        aops_commands.unregister_aops_command_extension("diagnose")


@pytest.mark.asyncio
async def test_aops_cli_only_command_is_rejected():
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event("/skills search kubernetes")

    result = await runner._handle_message(event)

    assert "not supported on AOPS" in result


@pytest.mark.asyncio
async def test_aops_new_uses_actions_slash_confirm(monkeypatch):
    from tools import slash_confirm as slash_confirm

    runner = _make_runner(extra={"dm_policy": "open"})
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    runner.adapters[Platform.AOPS] = adapter
    event = _make_aops_event("/new")
    session_key = runner._session_key_for_source(event.source)
    slash_confirm.clear(session_key)

    result = await runner._handle_message(event)

    assert result is None
    action_payload = adapter.send_reply_event.await_args.args[0]
    approval = action_payload["content"][0]
    assert action_payload["phase"] == "actions"
    assert action_payload["kind"] == "approval"
    assert action_payload["replyToId"] == "msg-1"
    assert action_payload["messageType"] == "common"
    assert approval["approvalKind"] == "slash"
    assert _approval_commands(approval["allowedActions"]) == ["/approve", "/always", "/cancel"]
    assert _approval_displays(approval["allowedActions"]) == ["执行本次", "始终执行", "取消"]
    assert "Confirm /new" in approval["message"]
    assert slash_confirm.get_pending(session_key) is not None
    slash_confirm.clear(session_key)


@pytest.mark.asyncio
async def test_aops_model_list_fetches_current_gateway_models(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AOPS_MODEL_GATEWAY_KEY", "key-from-env")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model:
  provider: tec01-gateway
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key: ${AOPS_MODEL_GATEWAY_KEY}
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models

    captured = {}

    def fake_probe(api_key, base_url, timeout=5.0, api_mode=None, try_alternate=True):
        captured.update({
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout,
            "api_mode": api_mode,
            "try_alternate": try_alternate,
        })
        return {
            "models": ["qwen-coder", "deepseek-r1"],
            "probed_url": "http://model-gateway.internal/v1/models",
            "resolved_base_url": "http://model-gateway.internal/v1",
            "used_fallback": False,
        }

    monkeypatch.setattr(models, "probe_api_models", fake_probe)
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/model list"))

    payload = json.loads(result)
    assert payload["type"] == "model.list"
    assert payload["ok"] is True
    assert payload["itemType"] == "model"
    assert [item["id"] for item in payload["items"]] == ["qwen-coder", "deepseek-r1"]
    assert payload["items"][0]["current"] is True
    assert payload["items"][1]["command"] == "/model use tec01-gateway deepseek-r1"
    assert payload["context"]["baseUrl"] == "http://model-gateway.internal/v1"
    assert payload["context"]["apiKeyConfigured"] is True
    assert payload["context"]["probedUrl"] == "http://model-gateway.internal/v1/models"
    assert captured["api_key"] == "key-from-env"
    assert captured["base_url"] == "http://model-gateway.internal/v1"
    assert captured["timeout"] <= 1.5
    assert captured["try_alternate"] is False


@pytest.mark.asyncio
async def test_aops_model_list_accepts_camelcase_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "camel-key")
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  apiKey: MODEL_GATEWAY_API_KEY
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models

    captured = {}

    def fake_probe(api_key, base_url, timeout=5.0, api_mode=None, try_alternate=True):
        captured["api_key"] = api_key
        return {"models": ["qwen-coder"], "probed_url": f"{base_url}/models"}

    monkeypatch.setattr(models, "probe_api_models", fake_probe)
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/model"))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["items"][0]["id"] == "qwen-coder"
    assert payload["context"]["apiKeyConfigured"] is True
    assert captured["api_key"] == "camel-key"


@pytest.mark.asyncio
async def test_aops_model_list_accepts_api_key_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "ref-key")
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key_ref: MODEL_GATEWAY_API_KEY
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models

    captured = {}

    def fake_probe(api_key, base_url, timeout=5.0, api_mode=None, try_alternate=True):
        captured["api_key"] = api_key
        return {"models": ["qwen-coder"], "probed_url": f"{base_url}/models"}

    monkeypatch.setattr(models, "probe_api_models", fake_probe)
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/model list"))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["context"]["apiKeyConfigured"] is True
    assert captured["api_key"] == "ref-key"


@pytest.mark.asyncio
async def test_aops_model_list_ignores_unresolved_api_key_template(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "real-key")
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key: '{model.api_key}'
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models

    captured = {}

    def fake_probe(api_key, base_url, timeout=5.0, api_mode=None, try_alternate=True):
        captured["api_key"] = api_key
        return {"models": ["qwen-coder"], "probed_url": f"{base_url}/models"}

    monkeypatch.setattr(models, "probe_api_models", fake_probe)
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/model list"))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["context"]["apiKeyConfigured"] is True
    assert captured["api_key"] == "real-key"


@pytest.mark.asyncio
async def test_aops_model_use_persists_channel_preference(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.model_switch as model_switch
    from gateway import aops_state

    captured = {}

    def fake_switch_model(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            success=True,
            new_model="custom-model",
            target_provider="openrouter",
            provider_label="OpenRouter",
            api_key="key-1",
            base_url="https://openrouter.example/v1",
            api_mode="chat",
            model_info=None,
            error_message=None,
        )

    monkeypatch.setattr(model_switch, "switch_model", fake_switch_model)
    runner = _make_runner(extra={"dm_policy": "open"})

    event = _make_aops_event_for_channel("/model use openrouter custom-model", channel_id="conv-abc", agent_key="oma")
    session_key = runner._session_key_for_source(event.source)

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.switch"
    assert payload["ok"] is True
    assert payload["scope"] == "aops-channel"
    assert payload["persisted"] is True
    assert captured["raw_input"] == "custom-model"
    assert captured["explicit_provider"] == "openrouter"
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="oma")
    pref = aops_state.get_model_preference(pref_key)
    assert pref["model"] == "custom-model"
    assert pref["provider"] == "openrouter"
    runner._load_aops_model_preference_for_event(event, session_key)
    assert runner._session_model_overrides[session_key]["model"] == "custom-model"


@pytest.mark.asyncio
async def test_aops_model_use_custom_uses_current_configured_gateway_without_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("AOPS_MODEL_GATEWAY_KEY", "key-from-env")
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key: ${AOPS_MODEL_GATEWAY_KEY}
  api_mode: chat
""",
        encoding="utf-8",
    )
    from gateway import aops_state

    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model use custom qwen35-122b", channel_id="conv-abc", agent_key="main")
    session_key = runner._session_key_for_source(event.source)

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.switch"
    assert payload["ok"] is True
    assert payload["model"] == "qwen35-122b"
    assert payload["provider"] == "custom"
    assert payload["baseUrl"] == "http://model-gateway.internal/v1"
    assert payload["apiKeyConfigured"] is True
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    pref = aops_state.get_model_preference(pref_key)
    assert pref["model"] == "qwen35-122b"
    assert pref["api_key"] == "key-from-env"
    assert runner._session_model_overrides[session_key]["model"] == "qwen35-122b"
    assert runner._session_model_overrides[session_key]["base_url"] == "http://model-gateway.internal/v1"
    assert payload["configUpdated"] is True
    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "default: qwen35-122b" in saved
    assert "model: qwen35-122b" in saved
    assert "api_key: ${AOPS_MODEL_GATEWAY_KEY}" in saved


@pytest.mark.asyncio
async def test_aops_model_use_replaces_unresolved_api_key_template_with_env_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "key-from-env")
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key: '{model.api_key}'
  api_key_env: MODEL_GATEWAY_API_KEY
  api_mode: chat
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models

    captured = {}

    def fake_probe(api_key, base_url, timeout=5.0, api_mode=None, try_alternate=True):
        captured["api_key"] = api_key
        return {"models": ["qwen35-122b"], "probed_url": f"{base_url}/models"}

    monkeypatch.setattr(models, "probe_api_models", fake_probe)
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model use custom qwen35-122b", channel_id="conv-abc", agent_key="main")

    switch_result = await runner._handle_message(event)
    switch_payload = json.loads(switch_result)
    assert switch_payload["ok"] is True

    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "{model.api_key}" not in saved
    assert "api_key_env: MODEL_GATEWAY_API_KEY" in saved

    list_result = await runner._handle_message(_make_aops_event_for_channel("/model list", channel_id="conv-abc", agent_key="main"))
    list_payload = json.loads(list_result)
    assert list_payload["ok"] is True
    assert list_payload["context"]["apiKeyConfigured"] is True
    assert captured["api_key"] == "key-from-env"


@pytest.mark.asyncio
async def test_aops_model_status_does_not_switch_to_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models
    import agent.models_dev as models_dev
    from gateway import aops_state

    monkeypatch.setattr(models, "probe_api_models", lambda *a, **k: pytest.fail("status must not query /models"))
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *a, **k: pytest.fail("status must not query models.dev"))
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model status", channel_id="conv-abc", agent_key="main")

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.status"
    assert payload["ok"] is True
    assert payload["modelId"] == "qwen-coder"
    assert payload["model"] == "qwen-coder"
    assert payload["provider"] == "custom"
    assert payload["baseUrl"] == "http://model-gateway.internal/v1"
    assert payload["scope"] == "config"
    assert payload["configPath"] == str(tmp_path / "config.yaml")
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_model_status_typo_does_not_switch_to_stutus(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models
    import hermes_cli.model_switch as model_switch
    from gateway import aops_state

    monkeypatch.setattr(models, "probe_api_models", lambda *a, **k: pytest.fail("stutus must be treated as status"))
    monkeypatch.setattr(model_switch, "switch_model", lambda **kwargs: pytest.fail("stutus must not switch model"))
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model stutus", channel_id="conv-abc", agent_key="main")

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.status"
    assert payload["ok"] is True
    assert payload["modelId"] == "qwen-coder"
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_model_status_typo_stattus_does_not_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    import hermes_cli.models as models
    import hermes_cli.model_switch as model_switch
    from gateway import aops_state

    monkeypatch.setattr(models, "probe_api_models", lambda *a, **k: pytest.fail("stattus must be treated as status"))
    monkeypatch.setattr(model_switch, "switch_model", lambda **kwargs: pytest.fail("stattus must not switch model"))
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model stattus", channel_id="conv-abc", agent_key="main")

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.status"
    assert payload["modelId"] == "qwen-coder"
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_model_implicit_switch_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    import hermes_cli.model_switch as model_switch
    from gateway import aops_state

    monkeypatch.setattr(model_switch, "switch_model", lambda **kwargs: pytest.fail("implicit AOPS /model switch is disabled"))
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model qwen3", channel_id="conv-abc", agent_key="main")

    result = await runner._handle_message(event)

    payload = json.loads(result)
    assert payload["type"] == "model.switch"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "MODEL_USAGE"
    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_model_handler_status_typo_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
""",
        encoding="utf-8",
    )
    import hermes_cli.model_switch as model_switch

    monkeypatch.setattr(model_switch, "switch_model", lambda **kwargs: pytest.fail("stutus must not switch model"))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_model_command(_make_aops_event_for_channel("/model stutus", channel_id="conv-abc", agent_key="main"))

    payload = json.loads(result)
    assert payload["type"] == "model.status"
    assert payload["modelId"] == "qwen-coder"


@pytest.mark.asyncio
async def test_aops_model_status_ignores_stale_reserved_channel_preference(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: custom
  default: qwen-coder
  base_url: http://model-gateway.internal/v1
  api_key_env: MODEL_GATEWAY_API_KEY
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "key-from-env")
    from gateway import aops_state

    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    aops_state.set_model_preference(
        pref_key,
        {
            "model": "status]",
            "provider": "custom",
            "base_url": "http://model-gateway.internal/v1",
            "api_key_ref": "MODEL_GATEWAY_API_KEY",
            "api_mode": "chat_completions",
        },
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event_for_channel("/model current", channel_id="conv-abc", agent_key="main"))

    payload = json.loads(result)
    assert payload["type"] == "model.status"
    assert payload["modelId"] == "qwen-coder"
    assert payload["model"] == "qwen-coder"
    assert payload["source"] == "config.model"
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_model_preference_loader_drops_stale_reserved_model(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway import aops_state

    pref_key = aops_state.preference_key(platform="aops", channel_id="conv-abc", agent_key="main")
    aops_state.set_model_preference(pref_key, {"model": "status]", "provider": "custom"})
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event_for_channel("/model current", channel_id="conv-abc", agent_key="main")
    session_key = runner._session_key_for_source(event.source)

    runner._load_aops_model_preference_for_event(event, session_key)

    assert runner._session_model_overrides == {}
    assert aops_state.get_model_preference(pref_key) is None


@pytest.mark.asyncio
async def test_aops_commands_text_hides_removed_commands_and_lists_custom():
    import agent.skill_commands as skill_commands
    import agent.skill_utils as skill_utils

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        skill_commands,
        "get_skill_commands",
        lambda: {
            "/alpha-skill": {
                "name": "Alpha Skill",
                "description": "Alpha description",
                "skill_md_path": "/tmp/alpha/SKILL.md",
            },
            "/blocked-skill": {
                "name": "Blocked Skill",
                "description": "Blocked description",
                "skill_md_path": "/tmp/blocked/SKILL.md",
            },
            "/disabled-skill": {
                "name": "Disabled Skill",
                "description": "Disabled description",
                "skill_md_path": "/tmp/disabled/SKILL.md",
            },
        },
    )
    monkeypatch.setattr(
        skill_utils,
        "get_disabled_skill_names",
        lambda platform=None: {"Disabled Skill"} if platform == "aops" else set(),
    )
    runner = _make_runner(extra={"dm_policy": "open", "blocked_commands": ["/blocked-skill"]})

    try:
        result = await runner._handle_message(_make_aops_event("/commands"))
    finally:
        monkeypatch.undo()

    assert "/update" not in result
    assert "/debug" not in result
    assert "/cron remove <id|name>" in result
    assert "/cron history <id> [tsMs]" in result
    assert "⚡ **Skill Commands**:" in result
    assert "`/alpha-skill` -- Alpha description" in result
    assert "/blocked-skill" not in result
    assert "/disabled-skill" not in result


@pytest.mark.asyncio
async def test_aops_dynamic_skill_command_is_supported_and_invokes_agent(monkeypatch):
    import agent.skill_commands as skill_commands

    monkeypatch.setattr(
        skill_commands,
        "get_skill_commands",
        lambda: {
            "/test-skill": {
                "name": "Test Skill",
                "description": "Run the test skill",
                "skill_md_path": "/tmp/test-skill/SKILL.md",
            }
        },
    )
    monkeypatch.setattr(
        skill_commands,
        "resolve_skill_command_key",
        lambda command: "/test-skill" if str(command).replace("_", "-") == "test-skill" else None,
    )
    monkeypatch.setattr(
        skill_commands,
        "build_skill_invocation_message",
        lambda cmd_key, user_instruction, task_id=None: f"skill:{cmd_key}::{user_instruction}",
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    async def _capture(event, source, quick_key, run_generation):
        return event.text

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(_make_aops_event("/test-skill do the thing"))

    assert result == "skill:/test-skill::do the thing"


@pytest.mark.asyncio
async def test_aops_curator_is_handled_natively(monkeypatch):
    from gateway import aops_commands

    monkeypatch.setattr(aops_commands, "run_curator_command", lambda raw_args: (0, f"curator {raw_args}"))
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/curator status"))

    assert result == "curator status"


@pytest.mark.asyncio
async def test_aops_curator_invalid_subcommand_reaches_native_handler(monkeypatch):
    from gateway import aops_commands

    monkeypatch.setattr(
        aops_commands,
        "run_curator_command",
        lambda raw_args: (2, f"usage: hermes curator\nerror: invalid choice: '{raw_args}'"),
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_aops_event("/curator test"))

    assert "not supported on AOPS" not in result
    assert "invalid choice" in result
    assert "(exit 2)" in result


@pytest.mark.asyncio
async def test_aops_silent_skillhub_explore_returns_structured_result(monkeypatch):
    from gateway import aops_skillhub_bridge
    from gateway.aops_commands import LocalCommandResult

    monkeypatch.setattr(
        aops_skillhub_bridge,
        "_list_market_items",
        lambda: [
            {
                "slug": "knowledge-query",
                "displayName": "knowledge-query",
                "summary": "查询知识库。",
                "tags": [],
                "stats": {"downloads": 5, "stars": 0},
                "updatedAt": 1779094163560,
                "latestVersion": {"version": "20260518.084923"},
            }
        ],
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/bash clawhub explore --json"))

    assert isinstance(result, LocalCommandResult)
    assert result.content
    payload = result.content[0]
    assert payload["type"] == "commandResult"
    assert payload["ok"] is True
    assert payload["context"]["silent"] is True
    assert payload["context"]["parentMessageId"] == 123456
    assert payload["items"][0]["slug"] == "knowledge-query"
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter.send_reply_event = AsyncMock(return_value=SendResult(success=True))
    adapter._reply_flags_by_message_id["msg-1"] = {"silent": True}
    await adapter.send("user-001", result.text, reply_to="msg-1", metadata={"content": result.content})
    end = adapter.send_reply_event.await_args_list[1].args[0]
    assert end["messageType"] == "silent"


@pytest.mark.asyncio
async def test_aops_silent_skillhub_reply_links_to_user_message_id(monkeypatch):
    from gateway import aops_skillhub_bridge

    monkeypatch.setattr(aops_skillhub_bridge, "_list_market_items", lambda: [])
    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._message_handler = _make_runner(extra={"dm_policy": "open"})._handle_message
    adapter._connected_event.set()
    adapter._ws = _FakeWebSocket()

    await adapter._dispatch_payload(_make_wire_silent_aops_payload("/bash clawhub explore --json", message_id="2106450357"))
    if adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))

    sent = [payload["data"] for payload in adapter._ws.sent if payload.get("event") == "message_reply"]
    assert len(sent) == 1
    assert sent[0]["phase"] == "end"
    assert sent[0]["replyToId"] == "2106450357"
    assert sent[0]["messageType"] == "silent"
    assert sent[0]["content"][0]["context"]["parentMessageId"] == "2106450357"


@pytest.mark.asyncio
async def test_aops_message_type_silent_skillhub_explore_returns_structured_result(monkeypatch):
    from gateway import aops_skillhub_bridge
    from gateway.aops_commands import LocalCommandResult

    monkeypatch.setattr(
        aops_skillhub_bridge,
        "_list_market_items",
        lambda: [
            {
                "slug": "knowledge-query",
                "displayName": "knowledge-query",
                "summary": "查询知识库。",
                "tags": [],
                "stats": {"downloads": 5, "stars": 0},
                "updatedAt": 1779094163560,
                "latestVersion": {"version": "20260518.084923"},
            }
        ],
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_message_type_silent_aops_event("/bash clawhub explore --json"))

    assert isinstance(result, LocalCommandResult)
    assert result.content
    payload = result.content[0]
    assert payload["type"] == "commandResult"
    assert payload["ok"] is True
    assert payload["context"]["silent"] is True
    assert payload["context"]["parentMessageId"] == 123456
    assert payload["items"][0]["slug"] == "knowledge-query"


def test_aops_skillhub_market_list_calls_clawhub_listing_api(monkeypatch):
    from gateway import aops_skillhub_bridge

    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {
                        "slug": "knowledge-query",
                        "displayName": "Knowledge Query",
                        "summary": "查询知识库。",
                        "tags": ["knowledge"],
                        "stats": {"downloads": 5, "stars": 1},
                        "updatedAt": "2026-05-18T08:49:23Z",
                        "latestVersion": {"version": "20260518.084923"},
                    }
                ]
            }

    def fake_get(url, *, params, timeout):
        calls.append((url, params, timeout))
        return _Resp()

    monkeypatch.setenv("CLAWHUB_REGISTRY", "https://clawhub.internal")
    monkeypatch.setattr(aops_skillhub_bridge.httpx, "get", fake_get)

    items = aops_skillhub_bridge._list_market_items()

    assert calls == [("https://clawhub.internal/api/v1/skills", {"limit": 200}, 30)]
    assert items == [
        {
            "slug": "knowledge-query",
            "displayName": "Knowledge Query",
            "summary": "查询知识库。",
            "tags": ["knowledge"],
            "stats": {"downloads": 5, "stars": 1},
            "updatedAt": 1779094163000,
            "latestVersion": {"version": "20260518.084923"},
        }
    ]


@pytest.mark.asyncio
async def test_aops_skillhub_context_falls_back_to_top_level_fields(monkeypatch):
    from gateway import aops_skillhub_bridge
    from gateway.aops_commands import LocalCommandResult

    monkeypatch.setattr(aops_skillhub_bridge, "_list_market_items", lambda: [])
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_top_level_context_silent_aops_event("/bash clawhub explore --json"))

    assert isinstance(result, LocalCommandResult)
    payload = result.content[0]
    assert payload["context"]["parentMessageId"] == "msg-top-1"
    assert payload["context"]["botId"] == "bot-top"
    assert payload["context"]["agentId"] == "agent-top"
    assert payload["context"]["model"] == "openclaw"


@pytest.mark.asyncio
async def test_aops_silent_skillhub_install_returns_result_and_done(monkeypatch):
    from gateway import aops_skillhub_bridge
    from gateway.aops_commands import LocalCommandResult

    monkeypatch.setattr(
        aops_skillhub_bridge,
        "_install_skill",
        lambda slug: (True, {"ok": True, "action": "install", "slug": slug, "message": "installed", "installedPath": "research/comment-context"}),
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/bash clawhub install comment-context"))

    assert isinstance(result, LocalCommandResult)
    assert result.content
    assert len(result.content) == 2
    payload = result.content[0]
    done_payload = result.content[1]
    assert payload["action"] == "install"
    assert payload["slug"] == "comment-context"
    assert done_payload["done"] is True


def test_aops_skillhub_install_uses_cli_short_name_and_detects_cli_error(monkeypatch):
    from gateway import aops_skillhub_bridge

    calls = []

    def fake_install(identifier, **kwargs):
        calls.append((identifier, kwargs))
        kwargs["console"].print("[bold red]Error:[/] No skill named 'machine-access-review' found in any source.")

    monkeypatch.setattr(aops_skillhub_bridge, "_configure_clawhub_source_base_url", lambda: "http://clawhub.internal/api/v1")
    monkeypatch.setattr(aops_skillhub_bridge, "_installed_path", lambda slug: None)
    monkeypatch.setattr("hermes_cli.skills_hub.do_install", fake_install)

    ok, payload = aops_skillhub_bridge._install_skill("machine-access-review")

    assert ok is False
    assert payload["ok"] is False
    assert calls[0][0] == "machine-access-review"
    assert calls[0][1]["force"] is True
    assert calls[0][1]["skip_confirm"] is True
    assert payload["error"]["code"] == "INSTALL_FAILED"


def test_aops_skillhub_install_succeeds_when_cli_installs_short_name(monkeypatch):
    from gateway import aops_skillhub_bridge

    def fake_install(identifier, **kwargs):
        kwargs["console"].print("[bold green]Installed:[/] machine-access-review")

    monkeypatch.setattr(aops_skillhub_bridge, "_configure_clawhub_source_base_url", lambda: "http://clawhub.internal/api/v1")
    monkeypatch.setattr(aops_skillhub_bridge, "_installed_path", lambda slug: "machine-access-review")
    monkeypatch.setattr("hermes_cli.skills_hub.do_install", fake_install)

    ok, payload = aops_skillhub_bridge._install_skill("machine-access-review")

    assert ok is True
    assert payload["ok"] is True
    assert payload["installedPath"] == "machine-access-review"


@pytest.mark.asyncio
async def test_aops_silent_skillhub_uninstall_returns_result_and_done(monkeypatch):
    from gateway import aops_skillhub_bridge
    from gateway.aops_commands import LocalCommandResult

    monkeypatch.setattr(
        aops_skillhub_bridge,
        "_uninstall_skill",
        lambda slug: (True, {"ok": True, "action": "uninstall", "slug": slug, "message": "removed"}),
    )
    runner = _make_runner(extra={"dm_policy": "open"})

    result = await runner._handle_message(_make_silent_aops_event("/bash clawhub uninstall comment-context"))

    assert isinstance(result, LocalCommandResult)
    assert result.content
    assert result.content[0]["action"] == "uninstall"
    assert result.content[1]["done"] is True


def test_aops_skillhub_uninstall_detects_cli_error(monkeypatch):
    from gateway import aops_skillhub_bridge

    def fake_uninstall(name, **kwargs):
        kwargs["console"].print("[bold red]Error:[/] 'missing-skill' is not a hub-installed skill (may be a builtin)")

    monkeypatch.setattr(aops_skillhub_bridge, "_configure_clawhub_source_base_url", lambda: "http://clawhub.internal/api/v1")
    monkeypatch.setattr("hermes_cli.skills_hub.do_uninstall", fake_uninstall)

    ok, payload = aops_skillhub_bridge._uninstall_skill("missing-skill")

    assert ok is False
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNINSTALL_FAILED"


@pytest.mark.asyncio
async def test_aops_silent_skillhub_invalid_subcommand_returns_error():
    from gateway.aops_commands import LocalCommandResult

    runner = _make_runner(extra={"dm_policy": "open"})
    result = await runner._handle_message(_make_silent_aops_event("/bash clawhub test"))

    assert isinstance(result, LocalCommandResult)
    assert result.content
    payload = result.content[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED_COMMAND"


def test_clawhub_base_url_uses_registry_env(monkeypatch):
    import tools.skills_hub as skills_hub

    monkeypatch.setenv("CLAWHUB_REGISTRY", "http://clawhub.internal")
    reloaded = importlib.reload(skills_hub)
    try:
        assert reloaded.ClawHubSource.BASE_URL == "http://clawhub.internal/api/v1"
    finally:
        monkeypatch.delenv("CLAWHUB_REGISTRY", raising=False)
        importlib.reload(skills_hub)


def test_aops_clawhub_base_url_reuses_skills_hub_registry_env(monkeypatch):
    import gateway.aops_skillhub_bridge as bridge
    import tools.skills_hub as skills_hub

    monkeypatch.setenv("CLAWHUB_REGISTRY", "http://clawhub.internal")
    importlib.reload(skills_hub)
    try:
        assert bridge._clawhub_base_url() == skills_hub.ClawHubSource.BASE_URL
    finally:
        monkeypatch.delenv("CLAWHUB_REGISTRY", raising=False)
        importlib.reload(skills_hub)


def test_aops_clawhub_base_url_reads_bashrc_when_process_env_missing(monkeypatch, tmp_path):
    import gateway.aops_skillhub_bridge as bridge
    import tools.skills_hub as skills_hub

    (tmp_path / ".bashrc").write_text("export CLAWHUB_REGISTRY=http://bashrc-clawhub.internal\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAWHUB_REGISTRY", raising=False)
    importlib.reload(skills_hub)
    try:
        assert bridge._clawhub_base_url() == "http://bashrc-clawhub.internal/api/v1"
        assert os.environ["CLAWHUB_REGISTRY"] == "http://bashrc-clawhub.internal"
    finally:
        monkeypatch.delenv("CLAWHUB_REGISTRY", raising=False)
        importlib.reload(skills_hub)


def test_aops_target_ref_is_explicit_without_whitespace():
    assert _parse_target_ref("aops", "user-001") == ("user-001", None, True)
    assert _parse_target_ref("aops", "Home (dm)") == (None, None, False)


@pytest.mark.asyncio
async def test_send_to_platform_aops_requires_live_runtime(monkeypatch):
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    result = await _send_to_platform(
        Platform.AOPS,
        PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}),
        "user-001",
        "hello",
    )
    assert "No live adapter for platform 'aops'" in result["error"]


@pytest.mark.asyncio
async def test_send_to_platform_aops_uses_runtime_adapter(monkeypatch):
    fake_adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="m-1")))
    fake_runner = SimpleNamespace(adapters={Platform.AOPS: fake_adapter})
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: fake_runner)

    result = await _send_to_platform(
        Platform.AOPS,
        PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}),
        "user-001",
        "hello",
    )
    assert result == {
        "success": True,
        "platform": "aops",
        "chat_id": "user-001",
        "message_id": "m-1",
    }


def test_toolsets_and_platform_hints_include_aops():
    assert "hermes-aops" in toolsets.TOOLSETS
    assert "hermes-aops" in toolsets.TOOLSETS["hermes-gateway"]["includes"]
    assert "aops" in PLATFORM_HINTS
    assert "tool" in PLATFORM_HINTS["aops"].lower()
    assert "approval" in PLATFORM_HINTS["aops"].lower()


@pytest.mark.asyncio
async def test_run_agent_route_overrides_apply_to_agent_init(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "openai", "api_key": "key", "base_url": "https://example.com", "api_mode": "responses"},
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner(platform=Platform.LOCAL, extra={})
    runner.config = GatewayConfig(platforms={})

    source = SessionSource(platform=Platform.LOCAL, chat_id="cli", chat_name="CLI", chat_type="dm", user_id="user-1")
    result = await runner._run_agent(
        message="ping",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:local:dm",
        route_overrides={
            "model": "gpt-5.4",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "command": "codex",
            "args": ["--fast"],
            "credential_pool": "pool-a",
        },
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["command"] == "codex"
    assert _CapturingAgent.last_init["args"] == ["--fast"]
    assert _CapturingAgent.last_init["credential_pool"] == "pool-a"
