"""Tests for notify.py (Telegram / ntfy / email notifications)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402


def _settings(**overrides):
    base = {"NOTIFY_MODE": "failures"}
    base.update(overrides)
    return notify.settings_from_dict(base)


def _results():
    return [
        {"name": "a", "status": "success", "duration": 1.0, "error": ""},
        {"name": "b", "status": "failed", "duration": 2.0, "error": "boom"},
    ]


def test_configured_channels():
    assert notify.configured_channels(_settings()) == []
    assert notify.configured_channels(
        _settings(NOTIFY_TELEGRAM_BOT_TOKEN="tok", NOTIFY_TELEGRAM_CHAT_ID="123")
    ) == ["telegram"]
    assert notify.configured_channels(_settings(NOTIFY_NTFY_TOPIC="t")) == ["ntfy"]
    assert notify.configured_channels(
        _settings(NOTIFY_SMTP_HOST="smtp.example.com", NOTIFY_EMAIL_TO="a@b.c")
    ) == ["email"]
    # telegram needs both token and chat id
    assert notify.configured_channels(_settings(NOTIFY_TELEGRAM_BOT_TOKEN="tok")) == []


def test_sync_finished_respects_mode():
    ok_results = [{"name": "a", "status": "success", "duration": 1.0, "error": ""}]
    # failures mode: silent on success
    assert notify.sync_finished(_settings(), "push", ok_results) == []
    # never: silent even on failure
    assert notify.sync_finished(_settings(NOTIFY_MODE="never"), "push", _results()) == []
    # empty results: never notify
    assert notify.sync_finished(_settings(NOTIFY_MODE="always"), "push", []) == []


def test_format_summary():
    subject, body = notify.format_summary("push", _results())
    assert "1 ok" in subject and "1 failed" in subject
    assert "Gitea -> GitHub" in body
    assert "FAIL b: boom" in body


def test_send_all_no_channels():
    assert notify.send_all(_settings(), "subj", "body") == []
    assert notify.test_message(_settings()) == []


def test_telegram_send_called():
    s = _settings(NOTIFY_TELEGRAM_BOT_TOKEN="tok", NOTIFY_TELEGRAM_CHAT_ID="123")
    with patch.object(notify, "_post_json") as mock_post:
        outcomes = notify.send_all(s, "subj", "body")
    assert outcomes == [{"channel": "telegram", "ok": True, "error": ""}]
    url = mock_post.call_args[0][0]
    assert "api.telegram.org/bottok/sendMessage" in url
    assert mock_post.call_args[0][1]["chat_id"] == "123"


def test_telegram_failure_recorded():
    s = _settings(NOTIFY_TELEGRAM_BOT_TOKEN="tok", NOTIFY_TELEGRAM_CHAT_ID="123")
    with patch.object(notify, "_post_json", side_effect=RuntimeError("nope")):
        outcomes = notify.send_all(s, "subj", "body")
    assert outcomes[0]["ok"] is False
    assert "nope" in outcomes[0]["error"]


def test_ntfy_send_called():
    s = _settings(NOTIFY_NTFY_TOPIC="my-topic", NOTIFY_NTFY_SERVER="https://ntfy.example.com")
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_open.return_value = mock_resp
        outcomes = notify.send_all(s, "subj", "body")
    assert outcomes[0] == {"channel": "ntfy", "ok": True, "error": ""}
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://ntfy.example.com/my-topic"


def test_email_send_called():
    s = _settings(
        NOTIFY_SMTP_HOST="smtp.example.com",
        NOTIFY_SMTP_PORT="587",
        NOTIFY_SMTP_USER="user",
        NOTIFY_SMTP_PASS="pass",
        NOTIFY_SMTP_FROM="from@example.com",
        NOTIFY_EMAIL_TO="a@example.com, b@example.com",
    )
    with patch("smtplib.SMTP") as mock_smtp:
        server = MagicMock()
        server.__enter__.return_value = server
        mock_smtp.return_value = server
        outcomes = notify.send_all(s, "subj", "body")
    assert outcomes[0]["ok"] is True
    server.login.assert_called_once_with("user", "pass")
    sent = server.send_message.call_args[0][0]
    assert sent["To"] == "a@example.com, b@example.com"
    assert sent["Subject"] == "subj"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_MODE", "always")
    monkeypatch.setenv("NOTIFY_NTFY_TOPIC", "env-topic")
    s = notify.settings_from_env()
    assert s["NOTIFY_MODE"] == "always"
    assert s["NOTIFY_NTFY_TOPIC"] == "env-topic"
    assert s["NOTIFY_TELEGRAM_BOT_TOKEN"] == ""
