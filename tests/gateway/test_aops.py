import asyncio
import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from agent.prompt_builder import PLATFORM_HINTS
from gateway.config import GatewayConfig, Platform, PlatformConfig, _apply_env_overrides
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
    def __init__(self, *, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


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
        self.get_calls = []
        self.post_calls = []
        self.ws_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
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
    runner.hooks.loaded_hooks = []
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


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


def test_platform_aops_registered():
    assert Platform.AOPS.value == "aops"


def test_get_connected_platforms_recognizes_aops():
    config = GatewayConfig(
        platforms={Platform.AOPS: PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"})}
    )
    assert config.get_connected_platforms() == [Platform.AOPS]


def test_apply_env_overrides_reads_aops(monkeypatch):
    monkeypatch.setenv("AOPS_BOT_TOKEN", "tok")
    monkeypatch.setenv("AOPS_BASE_URL", "https://aops.example.com")
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
    monkeypatch.setattr("gateway.platforms.aops._machine_id", lambda: "machine-123")

    adapter = AopsAdapter(PlatformConfig(enabled=True, token="tok", extra={"base_url": "https://aops.example.com"}))
    adapter._listen_loop = AsyncMock(return_value=None)

    assert await adapter.connect() is True
    assert fake_session.get_calls
    get_url, get_kwargs = fake_session.get_calls[0]
    assert get_url.endswith("/api/v1/bot/me")
    assert get_kwargs["headers"]["Authorization"] == "Bearer tok"
    assert get_kwargs["headers"]["tec-client-ip"] == "machine-123"
    assert fake_session.ws_calls
    ws_url, ws_kwargs = fake_session.ws_calls[0]
    assert ws_url == "wss://aops.example.com/api/v1/ws"
    assert ws_kwargs["headers"]["Authorization"] == "Bearer tok"
    assert fake_ws.sent == [{"action": "auth", "token": "tok"}]
    assert fake_session.post_calls
    report_url, report_kwargs = fake_session.post_calls[0]
    assert report_url.endswith("/api/v1/bot/agents/report")
    assert report_kwargs["json"]["source"] == "hermes"


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
