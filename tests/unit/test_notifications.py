"""
Tests for the notifications package: channels and dispatcher.
"""
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from agentdb import AgentDB, EmailChannel, NotificationDispatcher
from agentdb.notifications.channels import NotificationChannel
from agentdb.notifications.dispatcher import _format


# ---------------------------------------------------------------------------
# NotificationChannel protocol
# ---------------------------------------------------------------------------

def test_email_channel_is_notification_channel():
    ch = EmailChannel("user@example.com")
    assert isinstance(ch, NotificationChannel)


def test_slack_channel_missing_dep_raises():
    # The import is lazy (inside __init__), so setting sys.modules["slack_sdk"] = None
    # makes `from slack_sdk import WebClient` raise ImportError immediately.
    from agentdb.notifications.channels import SlackChannel as _SlackChannel
    with patch.dict("sys.modules", {"slack_sdk": None}):
        with pytest.raises(ImportError, match="slack-sdk"):
            _SlackChannel("xoxb-token", "#channel")


# ---------------------------------------------------------------------------
# EmailChannel
# ---------------------------------------------------------------------------

class TestEmailChannel:
    def test_default_config(self):
        ch = EmailChannel("ops@example.com")
        assert ch.to_address == "ops@example.com"
        assert ch.smtp_host == "localhost"
        assert ch.smtp_port == 25
        assert ch.use_tls is False
        assert ch.smtp_user is None

    def test_send_calls_smtp(self):
        ch = EmailChannel("ops@example.com", smtp_host="relay.local", smtp_port=25)
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            ch.send("Test subject", "Test body")
        mock_smtp_cls.assert_called_once_with("relay.local", 25, context=None)
        mock_server.send_message.assert_called_once()

    def test_send_with_credentials_calls_login(self):
        ch = EmailChannel(
            "ops@example.com",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="secret",
        )
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            ch.send("Subject", "Body")
        mock_server.login.assert_called_once_with("user", "secret")
        mock_server.send_message.assert_called_once()

    def test_send_tls_uses_smtp_ssl(self):
        ch = EmailChannel(
            "ops@example.com",
            smtp_host="smtp.example.com",
            smtp_port=465,
            use_tls=True,
        )
        mock_server = MagicMock()
        with patch("smtplib.SMTP_SSL") as mock_ssl_cls:
            mock_ssl_cls.return_value.__enter__ = lambda s: mock_server
            mock_ssl_cls.return_value.__exit__ = MagicMock(return_value=False)
            ch.send("Subject", "Body")
        mock_ssl_cls.assert_called_once()
        args, kwargs = mock_ssl_cls.call_args
        assert args[0] == "smtp.example.com"
        assert args[1] == 465

    def test_email_message_headers(self):
        ch = EmailChannel("ops@example.com", from_address="agentdb@myapp.com")
        captured = []

        def fake_send(msg):
            captured.append(msg)

        mock_server = MagicMock()
        mock_server.send_message = fake_send
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            ch.send("Hello", "World")

        assert len(captured) == 1
        msg = captured[0]
        assert msg["To"] == "ops@example.com"
        assert msg["From"] == "agentdb@myapp.com"
        assert msg["Subject"] == "Hello"


# ---------------------------------------------------------------------------
# SlackChannel
# ---------------------------------------------------------------------------

class TestSlackChannel:
    def _make_slack_channel(self):
        mock_client = MagicMock()
        mock_webclient_cls = MagicMock(return_value=mock_client)
        mock_slack_sdk = MagicMock()
        mock_slack_sdk.WebClient = mock_webclient_cls

        import sys
        sys.modules["slack_sdk"] = mock_slack_sdk
        # Reload channels so SlackChannel picks up the mock
        import importlib
        import agentdb.notifications.channels as ch_mod
        importlib.reload(ch_mod)

        ch = ch_mod.SlackChannel("xoxb-test-token", "#ops-alerts")
        return ch, mock_client

    def test_send_calls_post_message(self):
        ch, mock_client = self._make_slack_channel()
        ch.send("Alert", "Something happened")
        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "#ops-alerts"
        assert "Alert" in call_kwargs["text"]
        assert "Something happened" in call_kwargs["text"]

    def test_send_includes_subject_bold(self):
        ch, mock_client = self._make_slack_channel()
        ch.send("My Subject", "Body text")
        text = mock_client.chat_postMessage.call_args[1]["text"]
        assert "*My Subject*" in text


# ---------------------------------------------------------------------------
# _format helper
# ---------------------------------------------------------------------------

class TestFormat:
    def test_skill_stale(self):
        subject, body = _format("skill_stale", {"skill_id": "orders-model"})
        assert "orders-model" in subject
        assert "stale" in subject.lower()
        assert "0.6" in body

    def test_skill_review_needed(self):
        subject, body = _format("skill_review_needed", {
            "skill_id": "orders-model",
            "memory_key": "memory/orders/note",
            "agent_id": "a1",
        })
        assert "orders-model" in subject
        assert "a1" in body
        assert "memory/orders/note" in body

    def test_untracked_skill_candidate(self):
        subject, body = _format("untracked_skill_candidate", {
            "sample_key": "memory/revenue/schema",
            "cluster_size": 5,
        })
        assert "Untracked" in subject
        assert "5" in body
        assert "memory/revenue/schema" in body

    def test_conflict_detected(self):
        subject, body = _format("conflict_detected", {
            "key": "memory/orders/schema",
            "agent_id": "a1",
            "conflict_type": "temporal",
        })
        assert "memory/orders/schema" in subject
        assert "temporal" in body

    def test_unknown_event_fallback(self):
        subject, body = _format("custom_event", {"foo": "bar"})
        assert "custom_event" in subject
        assert "foo" in body


# ---------------------------------------------------------------------------
# NotificationDispatcher
# ---------------------------------------------------------------------------

class TestNotificationDispatcher:
    def _mock_channel(self):
        ch = MagicMock(spec=NotificationChannel)
        return ch

    def test_attach_subscribes_to_default_events(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        dispatcher = NotificationDispatcher(db.gateway, channels=[ch])
        dispatcher.attach()
        for event in ("skill_stale", "skill_review_needed",
                      "untracked_skill_candidate", "conflict_detected"):
            assert event in db.gateway._handlers

    def test_skill_stale_routes_to_channel(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        dispatcher = NotificationDispatcher(db.gateway, channels=[ch])
        dispatcher.attach()

        db.gateway.dispatch("skill_stale", skill_id="my-skill")

        ch.send.assert_called_once()
        subject, body = ch.send.call_args[0]
        assert "my-skill" in subject

    def test_skill_review_needed_routes_to_channel(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        NotificationDispatcher(db.gateway, channels=[ch]).attach()

        db.gateway.dispatch("skill_review_needed",
                            skill_id="orders", memory_key="memory/x", agent_id="a1")

        ch.send.assert_called_once()

    def test_multiple_channels_all_receive_event(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch1, ch2 = self._mock_channel(), self._mock_channel()
        NotificationDispatcher(db.gateway, channels=[ch1, ch2]).attach()

        db.gateway.dispatch("skill_stale", skill_id="s1")

        ch1.send.assert_called_once()
        ch2.send.assert_called_once()

    def test_failing_channel_does_not_block_others(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        bad_ch = MagicMock(spec=NotificationChannel)
        bad_ch.send.side_effect = RuntimeError("connection refused")
        good_ch = self._mock_channel()

        NotificationDispatcher(db.gateway, channels=[bad_ch, good_ch]).attach()
        db.gateway.dispatch("skill_stale", skill_id="s1")  # must not raise

        good_ch.send.assert_called_once()

    def test_custom_events_list(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        dispatcher = NotificationDispatcher(
            db.gateway, channels=[ch], events=["skill_stale"]
        )
        dispatcher.attach()

        assert "skill_stale" in db.gateway._handlers
        assert "conflict_detected" not in db.gateway._handlers

    def test_empty_events_list_subscribes_nothing(self, tmp_path):
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        before = set(db.gateway._handlers.keys())
        NotificationDispatcher(db.gateway, channels=[ch], events=[]).attach()
        after = set(db.gateway._handlers.keys())
        assert after == before

    def test_dispatcher_with_real_db_skill_stale_event(self, tmp_path):
        """Smoke test: writing a skill that goes stale fires channel via gateway."""
        db = AgentDB(path=tmp_path / "test.db")
        ch = self._mock_channel()
        NotificationDispatcher(db.gateway, channels=[ch]).attach()

        # Manually trigger the skill_stale event (staleness is governed by
        # SkillStore health score; we test the dispatch path directly here).
        db.gateway.dispatch("skill_stale", skill_id="revenue-model")

        ch.send.assert_called_once()
        subject, _ = ch.send.call_args[0]
        assert "revenue-model" in subject


# ---------------------------------------------------------------------------
# YAML-driven wiring via memory.yaml notifications: block
# ---------------------------------------------------------------------------

def _write_memory_yaml(memory_dir: "Path", content: str) -> None:
    config_dir = memory_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "memory.yaml").write_text(content)


class TestYamlNotifications:
    def test_no_notifications_block_is_silent(self, tmp_path):
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, "profile:\n  name: budget_optimized\n")
        db = AgentDB(memory_dir=memory_dir)
        # Should not raise and no notification handlers added beyond defaults
        assert "skill_stale" not in db.gateway._handlers

    def test_email_channel_wired_from_yaml(self, tmp_path):
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  channels:
    - type: email
      to: ops@example.com
      smtp_host: relay.local
      smtp_port: 2525
""")
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            db = AgentDB(memory_dir=memory_dir)
            db.gateway.dispatch("skill_stale", skill_id="orders-model")

        mock_server.send_message.assert_called_once()
        msg = mock_server.send_message.call_args[0][0]
        assert "orders-model" in msg["Subject"]
        assert msg["To"] == "ops@example.com"

    def test_slack_channel_wired_from_yaml(self, tmp_path):
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  channels:
    - type: slack
      bot_token: xoxb-test
      channel: "#alerts"
""")
        mock_client = MagicMock()
        mock_webclient_cls = MagicMock(return_value=mock_client)
        mock_slack_sdk = MagicMock()
        mock_slack_sdk.WebClient = mock_webclient_cls

        import sys
        sys.modules["slack_sdk"] = mock_slack_sdk
        import importlib
        import agentdb.notifications.channels as ch_mod
        importlib.reload(ch_mod)

        db = AgentDB(memory_dir=memory_dir)
        db.gateway.dispatch("skill_stale", skill_id="orders-model")

        mock_client.chat_postMessage.assert_called_once()
        assert mock_client.chat_postMessage.call_args[1]["channel"] == "#alerts"

    def test_events_subset_from_yaml(self, tmp_path):
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  events:
    - skill_stale
  channels:
    - type: email
      to: ops@example.com
""")
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            db = AgentDB(memory_dir=memory_dir)

        assert "skill_stale" in db.gateway._handlers
        assert "conflict_detected" not in db.gateway._handlers

    def test_missing_email_to_raises(self, tmp_path):
        from agentdb.interface.client import ConfigurationError
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  channels:
    - type: email
""")
        with pytest.raises(ConfigurationError, match="'to' is required"):
            AgentDB(memory_dir=memory_dir)

    def test_missing_slack_token_raises(self, tmp_path):
        from agentdb.interface.client import ConfigurationError
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  channels:
    - type: slack
      channel: "#alerts"
""")
        with pytest.raises(ConfigurationError, match="bot_token"):
            AgentDB(memory_dir=memory_dir)

    def test_unknown_channel_type_raises(self, tmp_path):
        from agentdb.interface.client import ConfigurationError
        memory_dir = tmp_path / "mem"
        _write_memory_yaml(memory_dir, """\
profile:
  name: budget_optimized
notifications:
  channels:
    - type: pager
      number: "555-1234"
""")
        with pytest.raises(ConfigurationError, match="unknown type"):
            AgentDB(memory_dir=memory_dir)
