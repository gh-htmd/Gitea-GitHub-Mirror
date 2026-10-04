"""Tests for multi-destination encrypted pushes."""

import json
import logging
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import encrypted_mirror as em  # noqa: E402
import notify  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)
LOG = logging.getLogger("test-multidest")


def _sh(*args, cwd=None):
    subprocess.run(args, check=True, capture_output=True, cwd=cwd)


def _make_source_repo(root: Path, name: str = "repo1") -> Path:
    src = root / f"{name}.git"
    _sh("git", "init", "--bare", "-q", str(src))
    work = root / f"{name}-work"
    _sh("git", "clone", "-q", str(src), str(work))
    (work / "f.txt").write_text("hello multi-dest\n")
    _sh("git", "-C", str(work), "add", "-A")
    _sh(
        "git",
        "-C",
        str(work),
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "c1",
    )
    _sh("git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/main")
    return src


@pytest.fixture()
def dest_repos():
    root = Path(tempfile.mkdtemp(prefix="multidest-"))
    d1 = root / "dest1.git"
    d2 = root / "dest2.git"
    _sh("git", "init", "--bare", "-q", str(d1))
    _sh("git", "init", "--bare", "-q", str(d2))
    src = _make_source_repo(root)
    yield src, d1, d2


def _dest(label, clone_url, primary=False):
    return {
        "label": label,
        "type": "github",
        "url": "https://github.com",
        "owner": "u",
        "username": "u",
        "token": "x",
        "private": True,
        "primary": primary,
        "skip_api": True,
        "clone_url": clone_url,
    }


# --- destination config -----------------------------------------------------


def test_normalize_dest_defaults():
    d = em._normalize_dest({"label": "b", "owner": "o", "token": "t"})
    assert d["type"] == "github"
    assert d["url"] == "https://github.com"
    assert d["username"] == "o"
    assert d["private"] is True
    assert d["enabled"] is True
    assert d["primary"] is False


def test_normalize_dest_gitea_needs_url():
    with pytest.raises(ValueError):
        em._normalize_dest({"label": "c", "type": "gitea", "owner": "o", "token": "t"})
    d = em._normalize_dest(
        {"label": "c", "type": "gitea", "url": "https://codeberg.org", "owner": "o", "token": "t"}
    )
    assert d["url"] == "https://codeberg.org"


def test_normalize_dest_rejects_bad():
    with pytest.raises(ValueError):
        em._normalize_dest({"owner": "o", "token": "t"})  # no label
    with pytest.raises(ValueError):
        em._normalize_dest({"label": "x", "owner": "o"})  # no token
    with pytest.raises(ValueError):
        em._normalize_dest({"label": "x", "type": "gitlab", "owner": "o", "token": "t"})


def test_load_extra_destinations_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))  # empty dir: no UI db
    monkeypatch.setenv(
        "MIRROR_DESTINATIONS",
        json.dumps(
            [
                {"label": "backup", "type": "github", "owner": "o", "token": "t"},
                {"label": "bad"},  # skipped with a warning
            ]
        ),
    )
    dests = em.load_extra_destinations()
    assert [d["label"] for d in dests] == ["backup"]
    assert dests[0]["source"] == "env"


def test_load_extra_destinations_bad_json(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MIRROR_DESTINATIONS", "not json{")
    assert em.load_extra_destinations() == []


def test_push_destinations_filter():
    cfg = {
        "github_user": "u",
        "github_token": "t",
        "github_private": True,
        "destinations": [
            {
                "label": "a",
                "type": "github",
                "url": "https://github.com",
                "owner": "o",
                "username": "o",
                "token": "t",
                "private": True,
                "enabled": True,
                "primary": False,
            },
            {
                "label": "b",
                "type": "github",
                "url": "https://github.com",
                "owner": "o",
                "username": "o",
                "token": "t",
                "private": True,
                "enabled": False,
                "primary": False,
            },
        ],
    }
    dests = em._push_destinations(cfg, Namespace(dest=None), LOG)
    assert [d["label"] for d in dests] == ["primary", "a"]  # disabled 'b' excluded
    dests = em._push_destinations(cfg, Namespace(dest="a"), LOG)
    assert [d["label"] for d in dests] == ["a"]
    dests = em._push_destinations(cfg, Namespace(dest="nope"), LOG)
    assert dests == []


# --- two-phase push ----------------------------------------------------------


def test_build_then_push_two_dests(dest_repos):
    src, d1, d2 = dest_repos
    work_root = Path(tempfile.mkdtemp(prefix="md-work-"))

    build = em.build_encrypted_payload(
        "repo1",
        src.as_uri(),
        "test-passphrase",
        work_root,
        dry_run=False,
        verify=True,
        logger=LOG,
        secrets=(),
    )
    assert build["status"] == "success", build
    payload = build["payload"]
    assert len(payload["encrypted"]) > 0
    assert len(payload["sha256"]) == 64

    r1 = em.push_payload_to_dest(
        "repo1",
        payload,
        _dest("primary", d1.as_uri(), primary=True),
        work_root,
        False,
        LOG,
        (),
    )
    assert r1["status"] == "success", r1
    assert r1["dest"] == "primary"
    assert r1["bytes"] == len(payload["encrypted"])

    # Same payload pushed again to the same dest -> skipped (per-dest fingerprint).
    r1b = em.push_payload_to_dest(
        "repo1",
        payload,
        _dest("primary", d1.as_uri(), primary=True),
        work_root,
        False,
        LOG,
        (),
    )
    assert r1b["status"] == "skipped", r1b

    # A brand-new destination gets the payload even though it is unchanged elsewhere.
    r2 = em.push_payload_to_dest(
        "repo1",
        payload,
        _dest("codeberg", d2.as_uri()),
        work_root,
        False,
        LOG,
        (),
    )
    assert r2["status"] == "success", r2
    assert r2["dest"] == "codeberg"
    assert "bytes" not in r2  # storage stats recorded once per repo

    # Both dests hold the marker with the same fingerprint.
    for d in (d1, d2):
        branches = subprocess.run(
            ["git", "--git-dir", str(d), "branch", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        found = False
        for br in branches:
            out = subprocess.run(
                ["git", "--git-dir", str(d), "show", f"{br}:.gitea-encrypted-mirror"],
                capture_output=True,
                text=True,
            )
            if out.returncode == 0:
                marker = json.loads(out.stdout)
                assert marker["sha256"] == payload["sha256"]
                found = True
                break
        assert found, f"no marker found on {d}"


def test_build_dry_run(dest_repos):
    src, _, _ = dest_repos
    work_root = Path(tempfile.mkdtemp(prefix="md-dry-"))
    build = em.build_encrypted_payload(
        "repo1",
        src.as_uri(),
        "test-passphrase",
        work_root,
        dry_run=True,
        verify=False,
        logger=LOG,
        secrets=(),
    )
    assert build["status"] == "success"
    assert "payload" not in build
    assert "dry-run" in build["error"]


def test_compat_wrapper_single_dest(dest_repos):
    src, d1, _ = dest_repos
    work_root = Path(tempfile.mkdtemp(prefix="md-compat-"))
    res = em.push_encrypted_repo(
        repo_name="repo1",
        gitea_clone_url=src.as_uri(),
        github_clone_url=d1.as_uri(),
        github_api=None,  # test mode: skip repo creation
        passphrase="test-passphrase",
        work_root=work_root,
        dry_run=False,
        verify=True,
        logger=LOG,
        secrets=(),
    )
    assert res["status"] == "success", res
    assert res["dest"] == "primary"


# --- notifications ------------------------------------------------------------


def test_notify_per_dest_section():
    results = [
        {"name": "a", "status": "success", "dest": "primary", "error": ""},
        {"name": "a", "status": "success", "dest": "backup", "error": ""},
        {"name": "b", "status": "failed", "dest": "backup", "error": "boom"},
    ]
    subject, body = notify.format_summary("push", results)
    assert "Destinations:" in body
    assert "FAIL backup/b: boom" in body
    assert "2 ok" in subject


def test_notify_single_dest_unchanged():
    results = [
        {"name": "a", "status": "success", "error": ""},
        {"name": "b", "status": "failed", "error": "boom"},
    ]
    subject, body = notify.format_summary("push", results)
    assert "Destinations:" not in body
    assert "FAIL b: boom" in body
    assert "1 ok" in subject
