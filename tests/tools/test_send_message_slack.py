"""Slack-specific send_message delivery regressions."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform
from tools.send_message_tool import _send_slack, _send_to_platform


def _ensure_slack_mock(monkeypatch):
    """Install lightweight Slack modules when optional Slack deps are absent."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_slack_send_to_platform_prefers_live_adapter_when_available(monkeypatch):
    """Gateway Slack sends should use the live adapter before standalone HTTP."""
    _ensure_slack_mock(monkeypatch)
    import gateway.platforms.slack as slack_mod

    monkeypatch.setattr(slack_mod, "SLACK_AVAILABLE", True)
    live_send = AsyncMock(return_value={"success": True, "message_id": "live-ts"})
    standalone_send = AsyncMock(side_effect=AssertionError("standalone Slack send should not run"))

    with (
        patch("tools.send_message_tool._send_via_adapter", live_send),
        patch("tools.send_message_tool._send_slack", standalone_send),
    ):
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                SimpleNamespace(enabled=True, token="bad-token,good-token", extra={}),
                "C123",
                "**hello** from [Hermes](<https://example.com>)",
                thread_id="171.1",
            )
        )

    assert result == {"success": True, "message_id": "live-ts"}
    live_send.assert_awaited_once_with(
        Platform.SLACK,
        SimpleNamespace(enabled=True, token="bad-token,good-token", extra={}),
        "C123",
        "**hello** from [Hermes](<https://example.com>)",
        thread_id="171.1",
        media_files=[],
        force_document=False,
    )
    standalone_send.assert_not_awaited()


class _SlackResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _SlackPostContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SlackSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, headers, json, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((token, json))
        if token == "good-token":
            payload = {"ok": True, "ts": "171.123"}
        else:
            payload = {"ok": False, "error": "invalid_auth"}
        return _SlackPostContext(_SlackResponse(payload))


def test_send_slack_tries_comma_separated_tokens_individually(monkeypatch):
    """Multi-workspace token lists must not be sent as one literal token."""
    fake_session = _SlackSession()

    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = asyncio.run(_send_slack("bad-token, good-token", "C123", "hello"))

    assert result == {
        "success": True,
        "platform": "slack",
        "chat_id": "C123",
        "message_id": "171.123",
    }
    assert [token for token, _payload in fake_session.calls] == [
        "bad-token",
        "good-token",
    ]
