#!/usr/bin/env python3
"""
Notifications for the encrypted mirror: Telegram, ntfy, and email (SMTP).

All channels are optional and configured independently — set up one, several,
or none. Uses only the Python standard library.

Settings (dict keys, usually from environment variables):
    NOTIFY_MODE                 failures | always | never   (default: failures)
    NOTIFY_TELEGRAM_BOT_TOKEN   Telegram bot token (from @BotFather)
    NOTIFY_TELEGRAM_CHAT_ID     Telegram chat ID (message @userinfobot, or your group)
    NOTIFY_NTFY_TOPIC           ntfy topic name, e.g. "mirror-alerts-xyz"
    NOTIFY_NTFY_SERVER          ntfy server (default: https://ntfy.sh)
    NOTIFY_SMTP_HOST            SMTP server hostname
    NOTIFY_SMTP_PORT            SMTP port (default: 587)
    NOTIFY_SMTP_USER            SMTP username (optional)
    NOTIFY_SMTP_PASS            SMTP password (optional)
    NOTIFY_SMTP_FROM            From address
    NOTIFY_EMAIL_TO             Recipient(s), comma-separated

License: MIT
"""

import json
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Dict, List

DEFAULT_NTFY_SERVER = "https://ntfy.sh"

SETTING_KEYS = [
    "NOTIFY_MODE",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_ID",
    "NOTIFY_NTFY_TOPIC",
    "NOTIFY_NTFY_SERVER",
    "NOTIFY_SMTP_HOST",
    "NOTIFY_SMTP_PORT",
    "NOTIFY_SMTP_USER",
    "NOTIFY_SMTP_PASS",
    "NOTIFY_SMTP_FROM",
    "NOTIFY_EMAIL_TO",
]


def settings_from_env() -> Dict[str, str]:
    """Collect notification settings from environment variables."""
    import os

    return {k: os.environ.get(k, "").strip() for k in SETTING_KEYS}


def settings_from_dict(source: Dict[str, Any]) -> Dict[str, str]:
    """Collect notification settings from a dict (e.g. the web UI's effective config)."""
    return {k: str(source.get(k, "") or "").strip() for k in SETTING_KEYS}


def configured_channels(settings: Dict[str, str]) -> List[str]:
    """Names of channels that have enough config to send."""
    channels = []
    if settings.get("NOTIFY_TELEGRAM_BOT_TOKEN") and settings.get("NOTIFY_TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if settings.get("NOTIFY_NTFY_TOPIC"):
        channels.append("ntfy")
    if settings.get("NOTIFY_SMTP_HOST") and settings.get("NOTIFY_EMAIL_TO"):
        channels.append("email")
    return channels


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "gitea-github-mirror/notify")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def send_telegram(settings: Dict[str, str], text: str) -> None:
    token = settings["NOTIFY_TELEGRAM_BOT_TOKEN"]
    chat_id = settings["NOTIFY_TELEGRAM_CHAT_ID"]
    _post_json(
        f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/sendMessage",
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
    )


def send_ntfy(settings: Dict[str, str], title: str, text: str) -> None:
    server = (settings.get("NOTIFY_NTFY_SERVER") or DEFAULT_NTFY_SERVER).rstrip("/")
    topic = settings["NOTIFY_NTFY_TOPIC"]
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"
    req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST")
    req.add_header("Title", title[:60])
    req.add_header("User-Agent", "gitea-github-mirror/notify")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def send_email(settings: Dict[str, str], subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.get("NOTIFY_SMTP_FROM") or settings.get("NOTIFY_SMTP_USER", "")
    recipients = [r.strip() for r in settings["NOTIFY_EMAIL_TO"].split(",") if r.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)

    host = settings["NOTIFY_SMTP_HOST"]
    port = int(settings.get("NOTIFY_SMTP_PORT") or "587")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls()
            smtp.ehlo()
        except smtplib.SMTPException:
            pass  # server doesn't offer STARTTLS; try plain
        if settings.get("NOTIFY_SMTP_USER"):
            smtp.login(settings["NOTIFY_SMTP_USER"], settings.get("NOTIFY_SMTP_PASS", ""))
        smtp.send_message(msg)


def send_all(settings: Dict[str, str], subject: str, body: str) -> List[Dict[str, Any]]:
    """Send to every configured channel. Returns per-channel results (never raises)."""
    text = f"{subject}\n\n{body}".strip()
    outcomes = []
    for channel in configured_channels(settings):
        try:
            if channel == "telegram":
                send_telegram(settings, text)
            elif channel == "ntfy":
                send_ntfy(settings, subject, body)
            elif channel == "email":
                send_email(settings, subject, body)
            outcomes.append({"channel": channel, "ok": True, "error": ""})
        except Exception as e:
            outcomes.append({"channel": channel, "ok": False, "error": str(e)[:300]})
    return outcomes


def format_summary(mode: str, results: List[Dict[str, Any]]) -> tuple:
    """Build a (subject, body) summary for a finished push/pull run."""
    counts = {"success": 0, "skipped": 0, "failed": 0}
    failed_lines = []
    for r in results:
        counts[r.get("status", "failed")] = counts.get(r.get("status"), 0) + 1
        if r.get("status") == "failed":
            failed_lines.append(f"FAIL {r.get('name', '?')}: {r.get('error', '')}")
    direction = "Gitea -> GitHub (encrypted)" if mode == "push" else "GitHub -> Gitea (decrypted)"
    subject = (
        f"Encrypted mirror {mode}: {counts['success']} ok, "
        f"{counts['skipped']} skipped, {counts['failed']} failed"
    )
    lines = [direction, ""]
    lines.append(
        f"Success: {counts['success']}  Skipped: {counts['skipped']}  "
        f"Failed: {counts['failed']}"
    )
    if failed_lines:
        lines += ["", "Failures:"] + failed_lines
    return subject, "\n".join(lines)


def sync_finished(
    settings: Dict[str, str], mode: str, results: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Send a notification for a finished sync run if the mode calls for it."""
    notify_mode = (settings.get("NOTIFY_MODE") or "failures").strip().lower()
    if notify_mode == "never" or not results:
        return []
    failed = sum(1 for r in results if r.get("status") == "failed")
    if notify_mode == "failures" and failed == 0:
        return []
    subject, body = format_summary(mode, results)
    return send_all(settings, subject, body)


def test_message(settings: Dict[str, str]) -> List[Dict[str, Any]]:
    """Send a test notification to every configured channel."""
    return send_all(
        settings,
        "Encrypted mirror: test notification",
        "If you received this, notifications are configured correctly.",
    )
