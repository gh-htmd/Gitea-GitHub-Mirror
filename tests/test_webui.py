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
