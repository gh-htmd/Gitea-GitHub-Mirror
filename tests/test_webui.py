"""Tests for the encrypted-mirror web UI (webui.py)."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="webui-test-"))
os.environ["WEBUI_DATA_DIR"] = str(_TMP)
os.environ["WEBUI_NO_SCHEDULER"] = "1"
os.environ.setdefault("UI_PASSWORD", "test-password")

import encrypted_mirror as em  # noqa: E402
import webui  # noqa: E402
from webui import (  # noqa: E402
    build_cfg,
    effective_value,
    value_source,
    write_ui_config,
)

app = webui.app


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _login(client):
    rv = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
    assert rv.status_code == 302
    with client.session_transaction() as sess:
        return sess["csrf"]


def _csrf_post(client, url, data):
    token = _login(client)
    data = dict(data)
    data["csrf_token"] = token
    return client.post(url, data=data, follow_redirects=False)


def test_login_required(client):
    for path in ("/", "/repos", "/runs", "/config"):
        rv = client.get(path, follow_redirects=False)
        assert rv.status_code == 302
        assert "/login" in rv.headers["Location"]


def test_login_wrong_password(client):
    rv = client.post("/login", data={"password": "nope"})
    assert rv.status_code == 401
    assert b"Wrong password" in rv.data


def test_login_ok(client):
    _login(client)
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"Dashboard" in rv.data


def test_config_roundtrip(client):
    _login(client)
    rv = _csrf_post(
        client, "/config", {"GITEA_URL": "https://git.example.com", "GITEA_USER": "tester"}
    )
    assert rv.status_code == 302
    assert effective_value("GITEA_URL") == "https://git.example.com"
    assert value_source("GITEA_URL") == "ui"
    # persisted to the data dir .env
    saved = (_TMP / ".env").read_text(encoding="utf-8")
    assert "GITEA_URL=https://git.example.com" in saved
    # and the config page renders it back (non-password field)
    rv = client.get("/config")
    assert b"https://git.example.com" in rv.data


def test_password_field_never_rendered(client):
    _login(client)
    _csrf_post(client, "/config", {"GITEA_TOKEN": "super-secret-token"})
    rv = client.get("/config")
    assert b"super-secret-token" not in rv.data
    assert effective_value("GITEA_TOKEN") == "super-secret-token"


def test_env_value_is_readonly(client, monkeypatch):
    monkeypatch.setenv("GITEA_USER", "env-user")
    _login(client)
    rv = client.get("/config")
    assert b"environment" in rv.data
    # attempt to override via the UI must not take effect
    _csrf_post(client, "/config", {"GITEA_USER": "ui-user"})
    assert effective_value("GITEA_USER") == "env-user"
    assert "GITEA_USER" not in (_TMP / ".env").read_text(encoding="utf-8")


def test_bool_field_saved(client):
    _login(client)
    _csrf_post(client, "/config", {"AUTO_SYNC": "on", "GITHUB_MIRROR_PRIVATE": ""})
    assert effective_value("AUTO_SYNC") == "true"
    assert effective_value("GITHUB_MIRROR_PRIVATE") == "false"


def test_repo_keys_crud_and_resolution(client):
    _login(client)
    with app.app_context():
        webui.set_repo_key("demo", "custom-key-1")
        keys = webui.get_all_repo_keys()
        assert keys["demo"] == "custom-key-1"
        cfg = build_cfg()
        assert em.resolve_passphrase(cfg, "demo") == "custom-key-1"
        webui.set_repo_key("demo", "custom-key-2")
        cfg = build_cfg()
        assert em.resolve_passphrase(cfg, "demo") == "custom-key-2"
        webui.clear_repo_key("demo")
        cfg = build_cfg()
        assert "demo" not in cfg["repo_keys"]


def test_shared_key_fallback(client):
    _login(client)
    _csrf_post(client, "/config", {"ENCRYPTION_PASSPHRASE": "shared-secret"})
    with app.app_context():
        cfg = build_cfg()
        assert em.resolve_passphrase(cfg, "any-repo") == "shared-secret"
        assert em.resolve_passphrase(cfg, "demo") == "shared-secret"


def test_per_repo_key_beats_shared(client):
    _login(client)
    _csrf_post(client, "/config", {"ENCRYPTION_PASSPHRASE": "shared-secret"})
    with app.app_context():
        webui.set_repo_key("demo", "custom-key")
        cfg = build_cfg()
        assert em.resolve_passphrase(cfg, "demo") == "custom-key"
        assert em.resolve_passphrase(cfg, "other") == "shared-secret"
        webui.clear_repo_key("demo")


def test_set_key_route(client):
    _login(client)
    rv = _csrf_post(client, "/repos/demo/key", {"passphrase": "route-key"})
    assert rv.status_code == 302
    with app.app_context():
        assert webui.get_all_repo_keys()["demo"] == "route-key"
    rv = _csrf_post(client, "/repos/demo/key/clear", {})
    assert rv.status_code == 302
    with app.app_context():
        assert "demo" not in webui.get_all_repo_keys()


def test_repos_page_shows_key_badges(client):
    _login(client)
    with app.app_context():
        webui.set_repo_key("demo", "k")
    # listing Gitea will fail (no real server) -> page shows the error, still 200
    rv = client.get("/repos")
    assert rv.status_code == 200
    with app.app_context():
        webui.clear_repo_key("demo")


def test_run_sync_records_config_failure(client):
    _login(client)
    with app.app_context():
        # wipe the bits run_sync needs so it fails fast without network
        for key in (
            "GITEA_URL",
            "GITEA_TOKEN",
            "GITEA_USER",
            "GITHUB_TOKEN",
            "GITHUB_USER",
            "ENCRYPTION_PASSPHRASE",
        ):
            os.environ.pop(key, None)
        write_ui_config(
            {
                k: ""
                for k in (
                    "GITEA_URL",
                    "GITEA_TOKEN",
                    "GITEA_USER",
                    "GITHUB_TOKEN",
                    "GITHUB_USER",
                    "ENCRYPTION_PASSPHRASE",
                )
            }
        )
        ok, msg = webui.run_sync("push")
        assert ok is False
        assert "Config incomplete" in msg
        runs = webui.recent_runs(5)
        assert runs and runs[0]["status"] == "failed"


def test_fail_closed_without_password(monkeypatch):
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    # make sure no dotenv file provides it either
    for dotenv_path in (webui.DOTENV_UI, webui.DOTENV_APP):
        if dotenv_path.is_file():
            kept = [
                line
                for line in dotenv_path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("UI_PASSWORD=")
            ]
            dotenv_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    with pytest.raises(RuntimeError, match="UI_PASSWORD"):
        webui.create_app()


def test_csrf_rejected(client):
    _login(client)
    rv = client.post("/config", data={"GITEA_URL": "https://x.example.com", "csrf_token": "bogus"})
    assert rv.status_code == 302  # redirected back with an error flash
    assert effective_value("GITEA_URL") != "https://x.example.com"


def test_human_bytes():
    assert webui.human_bytes(500) == "500 B"
    assert webui.human_bytes(2048) == "2.0 KB"
    assert webui.human_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert webui.human_bytes(3 * 1024**3) == "3.0 GB"


def test_sparkline():
    assert webui.sparkline_svg([]) == ""
    svg = webui.sparkline_svg([("2026-10-01", 100), ("2026-10-02", 300), ("2026-10-03", 200)])
    assert "<svg" in svg and "2026-10-01" in svg and "polyline" in svg


def test_storage_recording_and_queries(client):
    _login(client)
    with app.app_context():
        webui.record_runs(
            "push",
            "2026-10-04 10:00:00",
            [
                {
                    "name": "size-a",
                    "status": "success",
                    "duration": 1.0,
                    "error": "",
                    "bytes": 1000,
                },
                {
                    "name": "size-b",
                    "status": "success",
                    "duration": 2.0,
                    "error": "",
                    "bytes": 3000,
                },
                {"name": "size-c", "status": "failed", "duration": 0.5, "error": "x"},
            ],
        )
        latest = {r["repo"]: r["bytes"] for r in webui.storage_latest()}
        assert latest["size-a"] == 1000
        assert latest["size-b"] == 3000
        assert "size-c" not in latest  # failed runs record no size
        # newer measurement supersedes
        webui.record_runs(
            "push",
            "2026-10-04 11:00:00",
            [
                {
                    "name": "size-a",
                    "status": "success",
                    "duration": 1.0,
                    "error": "",
                    "bytes": 1500,
                },
            ],
        )
        latest = {r["repo"]: r["bytes"] for r in webui.storage_latest()}
        assert latest["size-a"] == 1500
        totals = webui.storage_daily_totals(90)
        assert totals, "expected daily totals"
        assert totals[-1][1] == 4500  # latest per repo: 1500 + 3000


def test_dashboard_shows_storage_panel(client):
    _login(client)
    with app.app_context():
        webui.record_runs(
            "push",
            "2026-10-04 12:00:00",
            [
                {
                    "name": "size-a",
                    "status": "success",
                    "duration": 1.0,
                    "error": "",
                    "bytes": 1500,
                },
            ],
        )
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"Encrypted storage" in rv.data
    assert b"size-a" in rv.data


def test_notify_fields_render(client):
    _login(client)
    rv = client.get("/config")
    assert b"NOTIFY_TELEGRAM_BOT_TOKEN" in rv.data
    assert b"NOTIFY_NTFY_TOPIC" in rv.data
    assert b"test-notify" in rv.data


def test_notify_config_saved_and_detected(client):
    _login(client)
    _csrf_post(client, "/config", {"NOTIFY_MODE": "always", "NOTIFY_NTFY_TOPIC": "my-topic"})
    assert webui.effective_value("NOTIFY_MODE") == "always"
    settings = webui.notify_settings()
    assert settings["NOTIFY_NTFY_TOPIC"] == "my-topic"
    assert webui.notify.configured_channels(settings) == ["ntfy"]


def test_test_notify_no_channels(client, monkeypatch):
    _login(client)
    for key in webui.notify.SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)
    with app.app_context():
        webui.write_ui_config({k: "" for k in webui.notify.SETTING_KEYS})
    token = _login(client)
    rv = client.post("/config/test-notify", data={"csrf_token": token}, follow_redirects=True)
    assert b"No notification channels configured yet." in rv.data


def test_test_notify_sends(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        webui.notify,
        "test_message",
        lambda settings: [{"channel": "ntfy", "ok": True, "error": ""}],
    )
    monkeypatch.setattr(webui.notify, "configured_channels", lambda settings: ["ntfy"])
    token = _login(client)
    rv = client.post("/config/test-notify", data={"csrf_token": token}, follow_redirects=True)
    assert b"Test notification sent via ntfy." in rv.data


def test_config_renders_all_sections(client):
    _login(client)
    rv = client.get("/config")
    html = rv.data.decode()
    for needle in [
        "KEYS_FILE",
        "GIT_CLONE_TIMEOUT",
        "LOG_LEVEL",
        "PRESERVE_ORGS",
        "SYNC_NOW",
        "FORCE_RECREATE",
        "MIRROR_INTERVAL",
        "MIRROR_LFS",
        "MIRROR_EXTRAS",
        "LANG_MIRROR",
        "MAX_RETRIES",
        "RETRY_DELAY",
        "REQUEST_TIMEOUT",
        "REPORT_MAX_COUNT",
        "NOTIFY_WEBHOOK",
        "NOTIFY_TYPE",
        "NOTIFY_CHAT_ID",
        "NOTIFY_ONLY_ON_FAILURE",
        "NOTIFY_INCLUDE_REPORT",
        "Encrypted sync",
        "Classic mirror",
    ]:
        assert needle in html, needle


def test_config_save_tuning_and_classic(client):
    _login(client)
    _csrf_post(
        client,
        "/config",
        {
            "GIT_CLONE_TIMEOUT": "300",
            "PRESERVE_ORGS": "on",
            "LOG_LEVEL": "DEBUG",
            "NOTIFY_TYPE": "telegram",
            "LANG_MIRROR": "cn",
        },
    )
    assert webui.effective_value("GIT_CLONE_TIMEOUT") == "300"
    assert webui.effective_value("PRESERVE_ORGS") == "true"
    assert webui.effective_value("LOG_LEVEL") == "DEBUG"
    assert webui.effective_value("NOTIFY_TYPE") == "telegram"
    assert webui.effective_value("LANG_MIRROR") == "cn"
    # UI-saved values stay editable (not shown as environment read-only)
    assert webui.value_source("GIT_CLONE_TIMEOUT") == "ui"
    # tidy up: LOG_LEVEL in os.environ could affect other tests' logging
    _csrf_post(
        client,
        "/config",
        {
            "GIT_CLONE_TIMEOUT": "",
            "LOG_LEVEL": "",
            "PRESERVE_ORGS": "",
            "NOTIFY_TYPE": "",
            "LANG_MIRROR": "",
        },
    )


def test_export_ui_env_roundtrip(client, monkeypatch):
    _login(client)
    # UI-saved value is exported for os.environ readers...
    _csrf_post(client, "/config", {"GIT_CLONE_TIMEOUT": "123"})
    assert os.environ.get("GIT_CLONE_TIMEOUT") == "123"
    # ...real env wins and is never clobbered...
    monkeypatch.setenv("GIT_CLONE_TIMEOUT", "999")
    webui.export_ui_env()
    assert os.environ["GIT_CLONE_TIMEOUT"] == "999"
    assert webui.value_source("GIT_CLONE_TIMEOUT") == "environment"
    # ...and clearing the UI value removes a previous export
    monkeypatch.delenv("GIT_CLONE_TIMEOUT")
    _csrf_post(client, "/config", {"GIT_CLONE_TIMEOUT": ""})
    assert "GIT_CLONE_TIMEOUT" not in os.environ
    assert webui.effective_value("GIT_CLONE_TIMEOUT") == ""


def test_build_cfg_merges_keys_file(client, tmp_path):
    import json

    _login(client)
    kf = tmp_path / "keys.json"
    kf.write_text(json.dumps({"repo-a": "file-key", "repo-b": "file-key-b"}))
    _csrf_post(client, "/config", {"KEYS_FILE": str(kf)})
    webui.set_repo_key("repo-a", "db-key")
    cfg = webui.build_cfg()
    assert cfg["repo_keys"]["repo-a"] == "db-key"  # UI key wins
    assert cfg["repo_keys"]["repo-b"] == "file-key-b"  # file key as fallback
    webui.clear_repo_key("repo-a")
    _csrf_post(client, "/config", {"KEYS_FILE": ""})
