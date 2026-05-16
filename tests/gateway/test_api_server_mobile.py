"""Tests for the API server iOS mobile chat endpoints."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.mobile_store import DEFAULT_GROUP_ID
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter(api_key: str = "sk-secret") -> APIServerAdapter:
    return APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": api_key,
                "mobile_store_path": ":memory:",
            },
        )
    )


def _create_mobile_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/mobile/pairings", adapter._handle_mobile_create_pairing)
    app.router.add_post("/api/mobile/register", adapter._handle_mobile_register)
    app.router.add_get("/api/mobile/bootstrap", adapter._handle_mobile_bootstrap)
    app.router.add_post("/api/mobile/conversations/{conversation_id}/messages", adapter._handle_mobile_send_message)
    app.router.add_get("/api/mobile/conversations/{conversation_id}/events", adapter._handle_mobile_events)
    app.router.add_post("/api/mobile/runs/{run_id}/stop", adapter._handle_mobile_stop_run)
    app.router.add_post("/api/mobile/messages/{message_id}/retry", adapter._handle_mobile_retry_message)
    return app


async def _create_pairing(cli: TestClient) -> dict:
    resp = await cli.post(
        "/api/mobile/pairings",
        json={},
        headers={"Authorization": "Bearer sk-secret"},
    )
    assert resp.status == 200
    return await resp.json()


async def _register(cli: TestClient, pairing: dict, code: str, name: str = "iPhone") -> dict:
    resp = await cli.post(
        "/api/mobile/register",
        json={
            "pairing_id": pairing["pairing_id"],
            "verification_code": pairing["verification_code"],
            "registration_code": code,
            "device_name": name,
        },
    )
    assert resp.status == 200
    return await resp.json()


async def _read_sse_event(resp, expected_event: str | None = None, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        line = await asyncio.wait_for(resp.content.readline(), timeout=timeout)
        if not line:
            await asyncio.sleep(0)
            continue
        decoded = line.decode("utf-8").strip()
        if not decoded.startswith("data: "):
            continue
        payload = json.loads(decoded[6:])
        if expected_event is None or payload.get("event") == expected_event:
            return payload
    raise AssertionError(f"SSE event not received: {expected_event}")


class TestMobilePairingRegistration:
    @pytest.mark.asyncio
    async def test_pairing_creation_requires_api_key(self):
        adapter = _make_adapter()
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/mobile/pairings", json={})
            assert resp.status == 401

            pairing = await _create_pairing(cli)
            assert pairing["pairing_id"].startswith("pairing_")
            assert pairing["verification_code"].isdigit()
            assert len(pairing["verification_code"]) == 6
            assert "/api/mobile/register" in pairing["registration_url"]

    @pytest.mark.asyncio
    async def test_register_rejects_bad_expired_and_duplicate_pairing(self):
        adapter = _make_adapter()
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            pairing = await _create_pairing(cli)

            bad = await cli.post(
                "/api/mobile/register",
                json={
                    "pairing_id": pairing["pairing_id"],
                    "verification_code": "000000",
                    "registration_code": "ios-bad",
                    "device_name": "Bad Phone",
                },
            )
            assert bad.status == 400
            assert (await bad.json())["error"]["code"] == "invalid_verification_code"

            expired_pairing = await _create_pairing(cli)
            adapter._mobile_store.expire_pairing(expired_pairing["pairing_id"])
            expired = await cli.post(
                "/api/mobile/register",
                json={
                    "pairing_id": expired_pairing["pairing_id"],
                    "verification_code": expired_pairing["verification_code"],
                    "registration_code": "ios-expired",
                },
            )
            assert expired.status == 410

            registered = await _register(cli, pairing, "ios-001", "Armin iPhone")
            assert registered["device_id"].startswith("dev_")
            assert registered["device_token"]
            assert registered["default_conversation"]["conversation_id"] == DEFAULT_GROUP_ID

            duplicate = await cli.post(
                "/api/mobile/register",
                json={
                    "pairing_id": pairing["pairing_id"],
                    "verification_code": pairing["verification_code"],
                    "registration_code": "ios-001",
                },
            )
            assert duplicate.status == 409


class TestMobileBootstrapAndEvents:
    @pytest.mark.asyncio
    async def test_bootstrap_requires_device_token_and_returns_default_group(self):
        adapter = _make_adapter()
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            pairing = await _create_pairing(cli)
            registered = await _register(cli, pairing, "ios-boot")

            no_token = await cli.get("/api/mobile/bootstrap")
            assert no_token.status == 401

            bootstrap = await cli.get(
                "/api/mobile/bootstrap",
                headers={"Authorization": f"Bearer {registered['device_token']}"},
            )
            assert bootstrap.status == 200
            data = await bootstrap.json()
            assert data["status"] == "registered"
            assert data["default_conversation"]["conversation_id"] == DEFAULT_GROUP_ID
            assert any(item["conversation_id"] == DEFAULT_GROUP_ID for item in data["conversations"])
            default = next(item for item in data["conversations"] if item["conversation_id"] == DEFAULT_GROUP_ID)
            assert default["messages"] == []

    @pytest.mark.asyncio
    async def test_send_message_streams_created_delta_and_completed_events(self):
        adapter = _make_adapter()
        async def fake_run_agent(**kwargs):
            kwargs["stream_delta_callback"]("Hello ")
            kwargs["stream_delta_callback"]("from Hermes")
            return {"final_response": "Hello from Hermes"}, {}

        adapter._run_agent = AsyncMock(side_effect=fake_run_agent)
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            pairing = await _create_pairing(cli)
            registered = await _register(cli, pairing, "ios-stream")
            headers = {"Authorization": f"Bearer {registered['device_token']}"}

            events = await cli.get(f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/events", headers=headers)
            assert events.status == 200
            assert (await _read_sse_event(events, "connected"))["event"] == "connected"

            send = await cli.post(
                f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/messages",
                json={"text": "@Hermes please handle this"},
                headers=headers,
            )
            assert send.status == 202
            send_data = await send.json()
            assert send_data["run_id"].startswith("run_")

            created = await _read_sse_event(events, "message.created")
            delta = await _read_sse_event(events, "message.delta")
            completed = await _read_sse_event(events, "message.completed")
            assert created["message"]["kind"] in {"user", "assistant"}
            assert delta["delta"]
            assert completed["message"]["status"] == "completed"

            bootstrap = await cli.get("/api/mobile/bootstrap", headers=headers)
            data = await bootstrap.json()
            default = next(item for item in data["conversations"] if item["conversation_id"] == DEFAULT_GROUP_ID)
            messages = {item["message_id"]: item for item in default["messages"]}
            assert send_data["message"]["message_id"] in messages
            assert completed["message"]["message_id"] in messages
            assert messages[completed["message"]["message_id"]]["status"] == "completed"
            events.close()

    @pytest.mark.asyncio
    async def test_forward_receipts_mark_online_and_offline_recipients(self):
        adapter = _make_adapter()
        async def fake_run_agent(**kwargs):
            kwargs["stream_delta_callback"]("Forwarded")
            return {"final_response": "Forwarded"}, {}

        adapter._run_agent = AsyncMock(side_effect=fake_run_agent)
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            sender = await _register(cli, await _create_pairing(cli), "ios-sender", "Sender")
            online = await _register(cli, await _create_pairing(cli), "ios-online", "Online")
            offline = await _register(cli, await _create_pairing(cli), "ios-offline", "Offline")

            online_events = await cli.get(
                f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/events",
                headers={"Authorization": f"Bearer {online['device_token']}"},
            )
            assert online_events.status == 200
            await _read_sse_event(online_events, "connected")

            sender_events = await cli.get(
                f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/events",
                headers={"Authorization": f"Bearer {sender['device_token']}"},
            )
            assert sender_events.status == 200
            await _read_sse_event(sender_events, "connected")

            send = await cli.post(
                f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/messages",
                json={"text": "Hermes please forward this message to other users"},
                headers={"Authorization": f"Bearer {sender['device_token']}"},
            )
            assert send.status == 202
            completed = await _read_sse_event(sender_events, "message.completed")
            receipts = {item["device_name"]: item["status"] for item in completed["delivery_receipts"]}
            assert receipts["Online"] == "delivered"
            assert receipts["Offline"] == "pending_offline"
            online_events.close()
            sender_events.close()


class TestMobileStopRetry:
    @pytest.mark.asyncio
    async def test_stop_and_retry_mobile_run(self):
        adapter = _make_adapter()
        run_started = asyncio.Event()

        async def slow_run_agent(**kwargs):
            run_started.set()
            await asyncio.sleep(10)
            return {"final_response": "done"}, {}

        adapter._run_agent = AsyncMock(side_effect=slow_run_agent)
        app = _create_mobile_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            registered = await _register(cli, await _create_pairing(cli), "ios-stop")
            headers = {"Authorization": f"Bearer {registered['device_token']}"}

            send = await cli.post(
                f"/api/mobile/conversations/{DEFAULT_GROUP_ID}/messages",
                json={"text": "@Hermes take your time"},
                headers=headers,
            )
            assert send.status == 202
            send_data = await send.json()
            await asyncio.wait_for(run_started.wait(), timeout=1.0)

            stop = await cli.post(f"/api/mobile/runs/{send_data['run_id']}/stop", headers=headers)
            assert stop.status == 200
            assert (await stop.json())["status"] == "stopping"

            async def retry_run_agent(**kwargs):
                kwargs["stream_delta_callback"]("Retried")
                return {"final_response": "Retried"}, {}

            adapter._run_agent = AsyncMock(side_effect=retry_run_agent)
            retry = await cli.post(
                f"/api/mobile/messages/{send_data['assistant_message']['message_id']}/retry",
                headers=headers,
            )
            assert retry.status == 202
            retry_data = await retry.json()
            assert retry_data["run_id"].startswith("run_")
            assert retry_data["message"]["retry_of"] == send_data["assistant_message"]["message_id"]
