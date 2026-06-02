import asyncio
import importlib
import json
import os
import sys
import threading
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


def _read_aops_wire_records(hermes_home):
    files = sorted((hermes_home / "logs" / "aops").glob("aops-wire-*.log"))
    records = []
    for path in files:
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def _aops_wire_actions(hermes_home):
    return [record.get("action") for record in _read_aops_wire_records(hermes_home)]


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
    records = _read_aops_wire_records(tmp_path)
    assert [record["action"] for record in records] == ["ws.receive", "ws.send"]
    assert records[0]["direction"] == "in"
    assert records[0]["event"] == "ping"
    assert records[0]["payload"]["message"] == "heartbeat"
    assert records[1]["direction"] == "out"
    assert records[1]["event"] == "pong"
    assert records[1]["ts"].endswith("Z")
    assert records[1]["localTime"]


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

    assert fake_ws.sent == []
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_id == "msg-1"
    assert event.text == "check cpu"


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


def test_aops_wire_log_keeps_recent_seven_days(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    log_dir = tmp_path / "logs" / "aops"
    log_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    old_log = log_dir / f"aops-wire-{(today - timedelta(days=8)).isoformat()}.log"
    recent_log = log_dir / f"aops-wire-{(today - timedelta(days=6)).isoformat()}.log"
    old_log.write_text('{"old": true}\n', encoding="utf-8")
    recent_log.write_text('{"recent": true}\n', encoding="utf-8")

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
    assert recent_log.exists()
    records = _read_aops_wire_records(tmp_path)
    current = next(record for record in records if record.get("messageId") == "botmsg-1")
    assert current["ts"].endswith("Z")
    assert current["localTime"]
    assert current["event"] == "message_reply"
    assert current["seq"] == 2
    assert current["phase"] == "end"
    assert current["kind"] == "final"
    assert current["channelId"] == "conv-1"
    assert current["replyToId"] == "msg-1"
    assert current["runId"] == "run-1"
    assert current["payload"]["data"]["text"] == "完整正文"


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
    actions = _aops_wire_actions(tmp_path)
    assert "attachment.download.start" in actions
    assert "attachment.download.success" in actions


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
    assert "attachment.download.skipped" in _aops_wire_actions(tmp_path)


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
    assert "attachment.download.failed" in _aops_wire_actions(tmp_path)


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
    records = _read_aops_wire_records(tmp_path)
    assert [record["action"] for record in records] == [
        "tool_progress.dropped",
        "tool_progress.dropped",
    ]
    assert all(record["direction"] == "drop" for record in records)
    assert records[0]["payload"]["event_type"] == "reasoning.available"
    assert records[0]["payload"]["preview"] == "测试收到。我是 数智运维专家。"
    assert records[0]["ts"].endswith("Z")
    assert records[0]["localTime"]


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
    end_payload = adapter.send_reply_event.await_args_list[1].args[0]
    approval = end_payload["content"][0]
    assert end_payload["phase"] == "end"
    assert approval["type"] == "approval"
    assert approval["approvalKind"] == "exec"
    assert approval["allowedActions"] == ["allow-once", "allow-always", "deny"]
    assert approval["expiresAtMs"] > 1760000000000


def test_agent_report_payload_uses_agent_routes():
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
async def test_aops_cron_history_returns_structured_list(monkeypatch, tmp_path):
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
    history = next(child for child in cron["children"] if child["command"] == "history")
    after = next(child for child in history["children"] if child["command"] == "after")
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
async def test_aops_cli_only_command_is_rejected():
    runner = _make_runner(extra={"dm_policy": "open"})
    event = _make_aops_event("/skills search kubernetes")

    result = await runner._handle_message(event)

    assert "not supported on AOPS" in result


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
    assert len(sent) == 2
    assert [item["phase"] for item in sent] == ["start", "end"]
    assert sent[0]["replyToId"] == "2106450357"
    assert sent[1]["replyToId"] == "2106450357"
    assert sent[0]["messageType"] == "silent"
    assert sent[1]["messageType"] == "silent"
    assert sent[1]["content"][0]["context"]["parentMessageId"] == "2106450357"


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
