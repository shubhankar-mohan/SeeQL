"""Unit tests for alert delivery channels (P4-16 debt).

`SlackChannel.send` / `WebhookChannel.send` against a mocked urllib layer:
success, HTTP error (returns False, no raise), timeout (returns False, no
raise), and Slack's message-length truncation (Slack rejects section text
over ~4000 chars, so long alert messages must be truncated with a marker
before posting).
"""

import json
import socket
import urllib.error

import alerting.channels as channels_module
from alerting.channels import LogChannel, SlackChannel, WebhookChannel
from alerting.models import Alert, Severity


def _alert(message="test alert message", context=None):
    return Alert(
        rule_name="test_rule:default",
        severity=Severity.WARNING,
        message=message,
        context=context if context is not None else {"server_id": "default"},
    )


class _FakeResponse:
    """Minimal stand-in for the context-managed object urlopen() returns."""

    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestSlackChannel:
    def test_send_success_returns_true(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["request"] = req
            return _FakeResponse(200)

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = SlackChannel("https://hooks.slack.test/webhook")
        assert channel.send(_alert()) is True
        assert captured["request"].full_url == "https://hooks.slack.test/webhook"

    def test_send_http_error_returns_false_no_raise(self, monkeypatch):
        def fake_urlopen(req, timeout=10):
            raise urllib.error.HTTPError(
                "https://hooks.slack.test/webhook", 500, "Internal Server Error", hdrs=None, fp=None
            )

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = SlackChannel("https://hooks.slack.test/webhook")
        assert channel.send(_alert()) is False

    def test_send_timeout_returns_false_no_raise(self, monkeypatch):
        def fake_urlopen(req, timeout=10):
            raise socket.timeout("timed out")

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = SlackChannel("https://hooks.slack.test/webhook")
        assert channel.send(_alert()) is False

    def test_long_message_is_truncated_before_posting(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(200)

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = SlackChannel("https://hooks.slack.test/webhook")
        long_message = "x" * 5000
        assert channel.send(_alert(message=long_message)) is True

        section_text = captured["payload"]["blocks"][1]["text"]["text"]
        assert "…[truncated]" in section_text
        # Bounded well under Slack's section-text limit, not the raw 5000 chars.
        assert len(section_text) < 4000

    def test_short_message_is_not_truncated(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(200)

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = SlackChannel("https://hooks.slack.test/webhook")
        channel.send(_alert(message="short message"))

        section_text = captured["payload"]["blocks"][1]["text"]["text"]
        assert "…[truncated]" not in section_text
        assert "short message" in section_text


class TestWebhookChannel:
    def test_send_success_returns_true(self, monkeypatch):
        def fake_urlopen(req, timeout=10):
            return _FakeResponse(200)

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = WebhookChannel("https://example.test/webhook")
        assert channel.send(_alert()) is True

    def test_send_http_error_returns_false_no_raise(self, monkeypatch):
        def fake_urlopen(req, timeout=10):
            raise urllib.error.HTTPError(
                "https://example.test/webhook", 500, "Internal Server Error", hdrs=None, fp=None
            )

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = WebhookChannel("https://example.test/webhook")
        assert channel.send(_alert()) is False

    def test_send_timeout_returns_false_no_raise(self, monkeypatch):
        def fake_urlopen(req, timeout=10):
            raise socket.timeout("timed out")

        monkeypatch.setattr(channels_module.urllib.request, "urlopen", fake_urlopen)

        channel = WebhookChannel("https://example.test/webhook")
        assert channel.send(_alert()) is False


class TestLogChannel:
    def test_send_always_returns_true(self):
        assert LogChannel().send(_alert()) is True
