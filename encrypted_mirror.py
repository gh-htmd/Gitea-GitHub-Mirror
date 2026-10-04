#!/usr/bin/env python3
"""
Encrypted bidirectional mirror between Gitea and GitHub.

    python3 encrypted_mirror.py push   # Gitea -> GitHub (encrypt before pushing)
    python3 encrypted_mirror.py pull   # GitHub -> Gitea (decrypt, then push)

How it works
------------
push:
    1. List repositories on your Gitea instance.
    2. For each repo: `git clone --mirror` from Gitea, then
       `git bundle create repo.bundle --all` — one file holding the full
       history, every branch and every tag.
    3. Encrypt that bundle with ENCRYPTION_PASSPHRASE (AES-256-GCM, see
       crypto.py) and push `<repo>.bundle.enc` plus a plaintext marker file
       into the GitHub repo (created private if missing). Unchanged repos
       are skipped via a SHA-256 fingerprint stored in the marker.

pull:
    1. List your GitHub repos and detect the marker file
       `.gitea-encrypted-mirror` — only those are treated as encrypted mirrors.
    2. Clone, decrypt the bundle with ENCRYPTION_PASSPHRASE, verify it.
    3. Create the Gitea repo if missing, then `git push --mirror` the
       decrypted content to Gitea.

Security properties
-------------------
The GitHub side never sees plaintext: file names, file contents, commit
messages, author names and branch names are all sealed inside the encrypted
bundle. The GitHub repo only ever contains the opaque `<repo>.bundle.enc`
blob, the marker, and a README explaining the repo is encrypted.

Environment variables (or .env file — shared with mirror.py):
    GITEA_URL, GITEA_TOKEN, GITEA_USER
    GITHUB_TOKEN, GITHUB_USER
    ENCRYPTION_PASSPHRASE   - the encryption key (prompted if a TTY and unset)
    GITHUB_MIRROR_PRIVATE   - create GitHub mirror repos as private (default: true)
    SKIP_REPOS              - comma-separated repo names to skip
    MAX_WORKERS             - concurrent workers (default: 5)
    LOG_LEVEL               - DEBUG, INFO, WARNING, ERROR (default: INFO)

Requires: pip install -r requirements-encrypted.txt   (cryptography)

License: MIT
"""

import argparse
import getpass
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import crypto

try:
    import mirror

    _MIRROR_AVAILABLE = True
except ImportError:  # pragma: no cover - standalone fallback
    _MIRROR_AVAILABLE = False
    mirror = None  # type: ignore

VERSION = "2.5.0"
SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
ENV_FILE = SCRIPT_DIR / ".env"

MARKER_FILE = ".gitea-encrypted-mirror"
BUNDLE_SUFFIX = ".bundle.enc"
README_FILE = "README.md"
GIT_USER_NAME = "gitea-github-mirror"
GIT_USER_EMAIL = "gitea-github-mirror@localhost"
CLONE_TIMEOUT = int(os.environ.get("GIT_CLONE_TIMEOUT", "1800"))

_print_lock = threading.Lock()
_shutdown_event = threading.Event()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
class GitError(Exception):
    """A git subprocess failed."""


class ApiError(Exception):
    """A GitHub/Gitea API call failed."""


def _redact(text: str, secrets: Tuple[str, ...]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def setup_logging(level_name: str) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"encmirror_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger("encmirror")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(threadName)-12s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)
    return logger


def run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = CLONE_TIMEOUT,
    secrets: Tuple[str, ...] = (),
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Run git, return stdout. Raises GitError with redacted output on failure."""
    cmd = ["git"] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(
            f"git command timed out after {timeout}s: {_redact(' '.join(cmd), secrets)}"
        ) from e
    if proc.returncode != 0:
        raise GitError(
            f"git {_redact(' '.join(args), secrets)} failed (exit {proc.returncode}): "
            f"{_redact(proc.stderr.strip()[:800], secrets)}"
        )
    return proc.stdout


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never ask for credentials interactively
    return env


def https_clone_url(base_url: str, username: str, token: str, owner: str, repo: str) -> str:
    """Build an authenticated HTTPS clone URL. Token is URL-encoded."""
    host = base_url.split("://", 1)[1].rstrip("/")
    user_q = urllib.parse.quote(username, safe="")
    token_q = urllib.parse.quote(token, safe="")
    return f"https://{user_q}:{token_q}@{host}/{owner}/{repo}.git"


def _api_request(
    url: str,
    token: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    token_scheme: str = "token",
    timeout: int = 30,
) -> Tuple[int, Any]:
    """Minimal JSON API helper. Returns (status_code, parsed_body_or_None)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"{token_scheme} {token}")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body.strip() else None
        except Exception:
            parsed = None
        return e.code, parsed


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def github_repo_exists(owner: str, name: str, token: str) -> bool:
    status, _ = _api_request(f"https://api.github.com/repos/{owner}/{name}", token)
    return status == 200


def ensure_github_repo(
    owner: str, name: str, token: str, private: bool, logger: logging.Logger
) -> None:
    """Create the GitHub repo if it does not exist yet."""
    if github_repo_exists(owner, name, token):
        return
    status, body = _api_request(
        "https://api.github.com/user/repos",
        token,
        method="POST",
        payload={
            "name": name,
            "private": private,
            "description": (
                "Encrypted mirror — all data is AES-256-GCM encrypted. "
                "See README.md. Managed by gitea-github-mirror."
            ),
            "auto_init": False,
        },
    )
    if status == 201:
        logger.debug(f"Created GitHub repo {owner}/{name}")
        return
    # 422 "already exists" can race with the GET above — treat as success.
    if status == 422 and body and "already exists" in json.dumps(body).lower():
        return
    raise ApiError(f"Could not create GitHub repo {owner}/{name}: HTTP {status}: {body}")


def github_has_encrypted_marker(owner: str, name: str, token: str) -> bool:
    status, _ = _api_request(
        f"https://api.github.com/repos/{owner}/{name}/contents/{MARKER_FILE}", token
    )
    return status == 200


# ---------------------------------------------------------------------------
# Gitea API helpers
# ---------------------------------------------------------------------------
def gitea_repo_exists(base_url: str, owner: str, name: str, token: str) -> bool:
    status, _ = _api_request(f"{base_url}/api/v1/repos/{owner}/{name}", token)
    return status == 200


def ensure_gitea_repo(
    base_url: str, owner: str, name: str, token: str, private: bool, logger: logging.Logger
) -> None:
    """Create the Gitea repo if it does not exist yet."""
    if gitea_repo_exists(base_url, owner, name, token):
        return
    status, body = _api_request(
        f"{base_url}/api/v1/user/repos",
        token,
        method="POST",
        payload={"name": name, "private": private, "auto_init": False},
    )
    if status == 201:
        logger.debug(f"Created Gitea repo {owner}/{name}")
        return
    if status == 422 and body and "already exists" in json.dumps(body).lower():
        return
    raise ApiError(f"Could not create Gitea repo {owner}/{name}: HTTP {status}: {body}")


# ---------------------------------------------------------------------------
# Encrypted bundle helpers
# ---------------------------------------------------------------------------
def _marker_dict(bundle_name: str, sha256: str) -> Dict[str, str]:
    return {
        "format": crypto.FORMAT_NAME,
        "bundle": bundle_name,
        "sha256": sha256,
        "created_utc": _utcnow(),
        "tool": f"gitea-github-mirror/{VERSION}",
        "note": (
            "Encrypted mirror. The bundle is AES-256-GCM encrypted; "
            "it cannot be read without the encryption passphrase."
        ),
    }


def _readme_text(bundle_name: str) -> str:
    return f"""# Encrypted mirror

This repository is an **encrypted mirror** of a Gitea repository, managed by
[gitea-github-mirror](https://github.com/yuanweize/gitea-github-mirror)
(`encrypted_mirror.py`, fork with encrypted-mirror support).

- `{bundle_name}` holds the **entire repository** — every file, the full
  history, all branches and tags — serialized with `git bundle` and then
  encrypted with **AES-256-GCM** (PBKDF2-HMAC-SHA256 key derivation).
- Nothing in this repo can be read without the encryption passphrase:
  not file names, not contents, not commit messages.
- `{MARKER_FILE}` is a plaintext manifest (format version + SHA-256
  fingerprint) so tooling can detect and verify the bundle.

## Restore

```bash
pip install -r requirements-encrypted.txt
ENCRYPTION_PASSPHRASE="your passphrase" \\
  python3 encrypted_mirror.py pull --only <repo-name>
```

This decrypts the bundle and pushes the restored repository to your Gitea
instance. Keep the passphrase somewhere safe — losing it means losing the
backup.
"""


def _read_marker(gh_dir: Path) -> Optional[Dict[str, Any]]:
    marker = gh_dir / MARKER_FILE
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return None


def _detect_default_branch(
    src_git_dir: Path, clone_url: str, secrets: Tuple[str, ...]
) -> Optional[str]:
    """Figure out the source repo's default branch (full ref, e.g. refs/heads/main).

    A `git clone --mirror` copies the remote's HEAD symref verbatim, which may
    dangle (e.g. bare repos initialized with a default HEAD of `master` while
    the real branch is `main`). The bundle would then carry the wrong HEAD, so
    normalize it here: prefer the remote's advertised HEAD symref when it
    actually exists, else main/master, else the first branch.
    """
    branches: List[str] = []
    try:
        out = run_git(
            ["--git-dir", str(src_git_dir), "for-each-ref", "--format=%(refname)"],
            secrets=secrets,
        )
        branches = sorted(r for r in out.splitlines() if r.startswith("refs/heads/"))
    except GitError:
        pass
    if not branches:
        return None

    advertised: Optional[str] = None
    try:
        out = run_git(["ls-remote", "--symref", clone_url, "HEAD"], secrets=secrets)
        for line in out.splitlines():
            if line.startswith("ref:"):
                advertised = line.split()[1]
                break
    except GitError:
        pass
    if advertised and advertised in branches:
        return advertised
    for candidate in ("refs/heads/main", "refs/heads/master"):
        if candidate in branches:
            return candidate
    return branches[0]


def _bundle_from_mirror_clone(
    src_git_dir: Path, bundle_path: Path, clone_url: str, secrets: Tuple[str, ...]
) -> int:
    """Create a full bundle from a --mirror clone. Returns ref count."""
    refs = run_git(
        ["--git-dir", str(src_git_dir), "for-each-ref", "--format=%(refname)"],
        secrets=secrets,
    )
    ref_list = [r for r in refs.splitlines() if r.strip()]
    if not ref_list:
        raise GitError("EMPTY_REPO")
    # Normalize HEAD so the bundle carries the real default branch.
    default_branch = _detect_default_branch(src_git_dir, clone_url, secrets)
    if default_branch:
        try:
            run_git(
                ["--git-dir", str(src_git_dir), "symbolic-ref", "HEAD", default_branch],
                secrets=secrets,
            )
        except GitError:
            pass
    run_git(
        ["--git-dir", str(src_git_dir), "bundle", "create", str(bundle_path), "--all"],
        secrets=secrets,
    )
    return len(ref_list)


def _verify_bundle_bytes(bundle_bytes: bytes, work_dir: Path, secrets: Tuple[str, ...]) -> None:
    """Write bundle bytes to disk and run `git bundle verify` on them."""
    probe_repo = work_dir / "probe.git"
    run_git(["init", "--quiet", "--bare", str(probe_repo)], secrets=secrets)
    probe = work_dir / "verify.bundle"
    probe.write_bytes(bundle_bytes)
    try:
        run_git(["--git-dir", str(probe_repo), "bundle", "verify", str(probe)], secrets=secrets)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# PUSH: Gitea -> GitHub (encrypted)
# ---------------------------------------------------------------------------
def push_encrypted_repo(
    repo_name: str,
    gitea_clone_url: str,
    github_clone_url: str,
    github_api: Optional[Dict[str, Any]],
    passphrase: str,
    work_root: Path,
    dry_run: bool,
    verify: bool,
    logger: logging.Logger,
    secrets: Tuple[str, ...],
) -> Dict[str, Any]:
    """Mirror one Gitea repo to GitHub, encrypted. Returns a result dict."""
    start = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"ggm-push-{repo_name}-", dir=str(work_root)))
    try:
        # 1. Mirror-clone from Gitea.
        src_git = tmp / "src.git"
        try:
            run_git(
                ["clone", "--mirror", "--quiet", gitea_clone_url, str(src_git)],
                secrets=secrets,
            )
        except GitError as e:
            return _result(repo_name, start, "failed", f"Gitea clone failed: {e}")

        # 2. Bundle everything.
        bundle_path = tmp / "repo.bundle"
        try:
            ref_count = _bundle_from_mirror_clone(src_git, bundle_path, gitea_clone_url, secrets)
        except GitError as e:
            if "EMPTY_REPO" in str(e):
                return _result(repo_name, start, "skipped", "empty repo (no commits)")
            return _result(repo_name, start, "failed", f"bundle failed: {e}")
        bundle_bytes = bundle_path.read_bytes()

        # 3. Encrypt.
        try:
            encrypted = crypto.encrypt_bytes(bundle_bytes, passphrase)
        except crypto.EncryptionError as e:
            return _result(repo_name, start, "failed", f"encryption failed: {e}")
        # Fingerprint the PLAINTEXT bundle: encryption is randomized (fresh
        # salt+nonce per run), so only the plaintext hash is stable for
        # change detection. A SHA-256 reveals nothing about the content.
        sha256 = hashlib.sha256(bundle_bytes).hexdigest()
        bundle_name = f"{repo_name}{BUNDLE_SUFFIX}"

        # 4. Verify round-trip before pushing anything.
        if verify:
            try:
                decrypted = crypto.decrypt_bytes(encrypted, passphrase)
                if decrypted != bundle_bytes:
                    return _result(repo_name, start, "failed", "verify: round-trip mismatch")
                _verify_bundle_bytes(decrypted, tmp, secrets)
            except (crypto.DecryptionError, GitError) as e:
                return _result(repo_name, start, "failed", f"verify failed: {e}")

        if dry_run:
            return _result(
                repo_name, start, "success", f"dry-run: would push {len(encrypted)} encrypted bytes"
            )

        # 5. Ensure the GitHub repo exists (API step skipped in local/test mode).
        if github_api is not None:
            try:
                ensure_github_repo(
                    github_api["owner"],
                    github_api["name"],
                    github_api["token"],
                    github_api["private"],
                    logger,
                )
            except ApiError as e:
                return _result(repo_name, start, "failed", str(e))

        # 6. Clone the GitHub mirror repo (shallow — we only need the tip).
        gh_dir = tmp / "gh"
        try:
            run_git(
                ["clone", "--depth", "1", "--quiet", github_clone_url, str(gh_dir)],
                secrets=secrets,
            )
        except GitError as e:
            return _result(repo_name, start, "failed", f"GitHub clone failed: {e}")

        # 7. Skip when the encrypted bundle is unchanged.
        marker = _read_marker(gh_dir)
        if (
            marker
            and marker.get("format") == crypto.FORMAT_NAME
            and crypto.constant_time_compare(marker.get("sha256", ""), sha256)
            and (gh_dir / bundle_name).is_file()
        ):
            return _result(repo_name, start, "skipped", "unchanged (bundle fingerprint match)")

        # 8. Write bundle + marker + README, commit, push.
        (gh_dir / bundle_name).write_bytes(encrypted)
        (gh_dir / MARKER_FILE).write_text(
            json.dumps(_marker_dict(bundle_name, sha256), indent=2) + "\n", encoding="utf-8"
        )
        (gh_dir / README_FILE).write_text(_readme_text(bundle_name), encoding="utf-8")

        run_git(["-C", str(gh_dir), "add", "-A"], secrets=secrets)
        status_out = run_git(["-C", str(gh_dir), "status", "--porcelain"], secrets=secrets)
        if not status_out.strip():
            return _result(repo_name, start, "skipped", "unchanged (nothing to commit)")

        run_git(
            [
                "-C",
                str(gh_dir),
                "-c",
                f"user.name={GIT_USER_NAME}",
                "-c",
                f"user.email={GIT_USER_EMAIL}",
                "commit",
                "--quiet",
                "-m",
                f"Encrypted mirror sync {_utcnow()} ({ref_count} refs)",
            ],
            secrets=secrets,
        )
        branch = run_git(
            ["-C", str(gh_dir), "rev-parse", "--abbrev-ref", "HEAD"], secrets=secrets
        ).strip()
        if branch == "HEAD":  # unborn HEAD (brand-new empty repo)
            branch = "main"
            run_git(["-C", str(gh_dir), "checkout", "--quiet", "-B", branch], secrets=secrets)
            run_git(["-C", str(gh_dir), "push", "--quiet", "-u", "origin", branch], secrets=secrets)
        else:
            run_git(["-C", str(gh_dir), "push", "--quiet", "origin", branch], secrets=secrets)

        return _result(
            repo_name,
            start,
            "success",
            f"pushed {len(encrypted)} encrypted bytes ({ref_count} refs)",
        )
    except GitError as e:
        return _result(repo_name, start, "failed", f"git error: {e}")
    except Exception as e:  # never let a worker thread die silently
        return _result(repo_name, start, "failed", f"unexpected: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# PULL: GitHub -> Gitea (decrypt)
# ---------------------------------------------------------------------------
def pull_decrypted_repo(
    repo_name: str,
    github_clone_url: str,
    gitea_push_url: str,
    gitea_api: Optional[Dict[str, Any]],
    passphrase: str,
    work_root: Path,
    dry_run: bool,
    verify: bool,
    logger: logging.Logger,
    secrets: Tuple[str, ...],
) -> Dict[str, Any]:
    """Restore one encrypted GitHub mirror into Gitea. Returns a result dict."""
    start = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"ggm-pull-{repo_name}-", dir=str(work_root)))
    try:
        # 1. Shallow-clone the encrypted GitHub repo (we only need HEAD's files).
        gh_dir = tmp / "gh"
        try:
            run_git(
                ["clone", "--depth", "1", "--quiet", github_clone_url, str(gh_dir)],
                secrets=secrets,
            )
        except GitError as e:
            return _result(repo_name, start, "failed", f"GitHub clone failed: {e}")

        marker = _read_marker(gh_dir)
        if not marker or marker.get("format") != crypto.FORMAT_NAME:
            return _result(repo_name, start, "skipped", "no encrypted-mirror marker")
        bundle_name = marker.get("bundle", f"{repo_name}{BUNDLE_SUFFIX}")
        bundle_file = gh_dir / bundle_name
        if not bundle_file.is_file():
            return _result(repo_name, start, "failed", f"marker present but {bundle_name} missing")

        blob = bundle_file.read_bytes()
        if not crypto.is_encrypted_blob(blob):
            return _result(repo_name, start, "failed", "bundle file is not an encrypted blob")

        # 2. Decrypt.
        try:
            bundle_bytes = crypto.decrypt_bytes(blob, passphrase)
        except crypto.DecryptionError as e:
            return _result(repo_name, start, "failed", f"decryption failed: {e}")

        # Fingerprint check against the marker (tamper evidence; AES-GCM
        # already authenticates, this is defense in depth).
        expected_sha = marker.get("sha256", "")
        if expected_sha and not crypto.constant_time_compare(
            hashlib.sha256(bundle_bytes).hexdigest(), expected_sha
        ):
            return _result(
                repo_name, start, "failed", "bundle SHA-256 does not match marker (tampered?)"
            )

        if verify:
            try:
                _verify_bundle_bytes(bundle_bytes, tmp, secrets)
            except GitError as e:
                return _result(repo_name, start, "failed", f"bundle verify failed: {e}")

        if dry_run:
            return _result(repo_name, start, "success", "dry-run: bundle decrypted and verified")

        # 3. Ensure the Gitea repo exists (API step skipped in local/test mode).
        if gitea_api is not None:
            try:
                ensure_gitea_repo(
                    gitea_api["base_url"],
                    gitea_api["owner"],
                    gitea_api["name"],
                    gitea_api["token"],
                    gitea_api.get("private", True),
                    logger,
                )
            except ApiError as e:
                return _result(repo_name, start, "failed", str(e))

        # 4. Restore from the bundle and mirror-push to Gitea.
        # --mirror clone maps every ref 1:1 (no refs/remotes/* tracking refs),
        # so `push --mirror` reproduces the source ref set exactly.
        bundle_path = tmp / "repo.bundle"
        bundle_path.write_bytes(bundle_bytes)
        restore_dir = tmp / "restore.git"
        try:
            run_git(
                ["clone", "--mirror", "--quiet", str(bundle_path), str(restore_dir)],
                secrets=secrets,
            )
        except GitError as e:
            return _result(repo_name, start, "failed", f"bundle restore failed: {e}")
        try:
            run_git(
                ["--git-dir", str(restore_dir), "push", "--mirror", "--quiet", gitea_push_url],
                secrets=secrets,
            )
        except GitError as e:
            return _result(repo_name, start, "failed", f"Gitea push failed: {e}")

        ref_count = len(
            run_git(
                ["--git-dir", str(restore_dir), "for-each-ref", "--format=%(refname)"],
                secrets=secrets,
            ).splitlines()
        )

        # 5. Point the destination at the bundle's default branch.
        # `push --mirror` reproduces every ref but leaves the remote HEAD
        # symref untouched (dangling at the init default), so set it.
        try:
            head_ref = run_git(
                ["--git-dir", str(restore_dir), "symbolic-ref", "HEAD"],
                secrets=secrets,
            ).strip()
            if head_ref.startswith("refs/heads/"):
                _set_remote_default_branch(
                    gitea_push_url, gitea_api, head_ref[len("refs/heads/") :], logger, secrets
                )
        except (GitError, ApiError) as e:
            logger.warning(f"Could not set default branch on {repo_name}: {e} (non-fatal)")

        return _result(repo_name, start, "success", f"decrypted and pushed ({ref_count} refs)")
    except GitError as e:
        return _result(repo_name, start, "failed", f"git error: {e}")
    except Exception as e:
        return _result(repo_name, start, "failed", f"unexpected: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _set_remote_default_branch(
    push_url: str,
    gitea_api: Optional[Dict[str, Any]],
    branch: str,
    logger: logging.Logger,
    secrets: Tuple[str, ...],
) -> None:
    """Set the destination repo's default branch (remote HEAD)."""
    if gitea_api is not None:
        status, body = _api_request(
            f"{gitea_api['base_url']}/api/v1/repos/{gitea_api['owner']}/{gitea_api['name']}",
            gitea_api["token"],
            method="PATCH",
            payload={"default_branch": branch},
        )
        if status not in (200, 201):
            raise ApiError(f"PATCH default_branch -> HTTP {status}: {body}")
        logger.debug(f"Set default branch to {branch}")
        return
    # No API (local/test mode): set the symref directly when the target is a path.
    path = push_url
    if path.startswith("file://"):
        path = path[len("file://") :]
    if "://" not in path and Path(path).is_dir():
        run_git(
            ["--git-dir", path, "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
            secrets=secrets,
        )
        logger.debug(f"Set local HEAD to refs/heads/{branch}")


def _result(name: str, start: float, status: str, error: str) -> Dict[str, Any]:
    return {"name": name, "status": status, "duration": time.time() - start, "error": error}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _load_config(logger: logging.Logger) -> Dict[str, Any]:
    if _MIRROR_AVAILABLE:
        mirror.load_env_file(ENV_FILE)
    elif ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

    cfg = {
        "gitea_url": os.environ.get("GITEA_URL", "").rstrip("/"),
        "gitea_token": os.environ.get("GITEA_TOKEN", ""),
        "gitea_user": os.environ.get("GITEA_USER", ""),
        "github_token": os.environ.get("GITHUB_TOKEN", ""),
        "github_user": os.environ.get("GITHUB_USER", ""),
        "passphrase": os.environ.get("ENCRYPTION_PASSPHRASE", ""),
        "github_private": os.environ.get("GITHUB_MIRROR_PRIVATE", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        "max_workers": int(os.environ.get("MAX_WORKERS", "5")),
        "skip_repos": {r.strip() for r in os.environ.get("SKIP_REPOS", "").split(",") if r.strip()},
    }
    missing = [
        var
        for var, key in [
            ("GITEA_URL", "gitea_url"),
            ("GITEA_TOKEN", "gitea_token"),
            ("GITEA_USER", "gitea_user"),
            ("GITHUB_TOKEN", "github_token"),
            ("GITHUB_USER", "github_user"),
        ]
        if not cfg[key]
    ]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)} (.env or export)")
        sys.exit(1)
    if not cfg["passphrase"]:
        if sys.stdin.isatty():
            cfg["passphrase"] = getpass.getpass("Encryption passphrase (hidden input): ")
        if not cfg["passphrase"]:
            logger.error("ENCRYPTION_PASSPHRASE is required for encrypted mirroring.")
            sys.exit(1)
    return cfg


def _filter_repos(names: List[str], only: Optional[List[str]], skip: set) -> List[str]:
    selected = [n for n in names if n not in skip]
    if only:
        only_set = {o.strip().lower() for o in only if o.strip()}
        selected = [n for n in selected if n.lower() in only_set]
    return sorted(selected)


def _install_sigint_handler(logger: logging.Logger):
    original = signal.getsignal(signal.SIGINT)

    def _handler(sig, frame):
        _shutdown_event.set()
        with _print_lock:
            logger.warning("\nShutdown requested, finishing current repos...")

    signal.signal(signal.SIGINT, _handler)
    return original


def run_push(cfg: Dict[str, Any], args: argparse.Namespace, logger: logging.Logger) -> int:
    secrets = (cfg["gitea_token"], cfg["github_token"])
    logger.info("Fetching Gitea repository list...")
    gitea_repos = mirror.fetch_gitea_repos(cfg["gitea_url"], cfg["gitea_token"], logger)
    # Default: mirror repos owned by GITEA_USER; --all-gitea includes org repos too.
    names = []
    for key, info in gitea_repos.items():
        if not args.all_gitea and info.get("owner", "").lower() != cfg["gitea_user"].lower():
            continue
        names.append(info["name"])
    names = _filter_repos(names, args.only, cfg["skip_repos"])
    if not names:
        logger.info("Nothing to push (no matching Gitea repos).")
        return 0

    logger.info(f"Encrypted push: {len(names)} repo(s) Gitea -> GitHub")
    for n in names:
        logger.info(f"  - {n}")
    if not args.yes and not args.dry_run:
        if (
            input(f"\nEncrypt and push {len(names)} repo(s) to GitHub? (y/N): ").strip().lower()
            != "y"
        ):
            logger.info("Cancelled.")
            return 0

    work_root = Path(tempfile.mkdtemp(prefix="ggm-work-"))
    results: List[Dict[str, Any]] = []
    original = _install_sigint_handler(logger)
    try:
        with ThreadPoolExecutor(
            max_workers=args.workers or cfg["max_workers"], thread_name_prefix="push"
        ) as pool:
            futures = {}
            for idx, name in enumerate(names, 1):
                if _shutdown_event.is_set():
                    break
                gitea_clone = https_clone_url(
                    cfg["gitea_url"], cfg["gitea_user"], cfg["gitea_token"], cfg["gitea_user"], name
                )
                github_clone = https_clone_url(
                    "https://github.com",
                    cfg["github_user"],
                    cfg["github_token"],
                    cfg["github_user"],
                    name,
                )
                fut = pool.submit(
                    push_encrypted_repo,
                    repo_name=name,
                    gitea_clone_url=gitea_clone,
                    github_clone_url=github_clone,
                    github_api={
                        "owner": cfg["github_user"],
                        "name": name,
                        "token": cfg["github_token"],
                        "private": cfg["github_private"],
                    },
                    passphrase=cfg["passphrase"],
                    work_root=work_root,
                    dry_run=args.dry_run,
                    verify=not args.no_verify,
                    logger=logger,
                    secrets=secrets,
                )
                futures[fut] = (idx, name)
            for fut in as_completed(futures):
                idx, name = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # pragma: no cover - defensive
                    res = _result(name, time.time(), "failed", f"worker crashed: {e}")
                results.append(res)
                with _print_lock:
                    _log_result(logger, idx, len(names), res)
    finally:
        signal.signal(signal.SIGINT, original)
        shutil.rmtree(work_root, ignore_errors=True)
    return _summarize(results, logger, args)


def run_pull(cfg: Dict[str, Any], args: argparse.Namespace, logger: logging.Logger) -> int:
    secrets = (cfg["gitea_token"], cfg["github_token"])
    logger.info("Fetching GitHub repository list...")
    all_repos = mirror.fetch_github_repos(cfg["github_token"], logger)
    owned = [
        r
        for r in all_repos
        if r.get("owner", {}).get("login", "").lower() == cfg["github_user"].lower()
    ]
    logger.info(f"Checking {len(owned)} GitHub repo(s) for the encrypted-mirror marker...")
    encrypted_names = []
    for r in owned:
        if _shutdown_event.is_set():
            break
        try:
            if github_has_encrypted_marker(cfg["github_user"], r["name"], cfg["github_token"]):
                encrypted_names.append(r["name"])
        except Exception as e:
            logger.warning(f"  ? could not check {r['name']}: {e}")
    encrypted_names = _filter_repos(encrypted_names, args.only, cfg["skip_repos"])
    if not encrypted_names:
        logger.info("No encrypted mirrors found on GitHub (nothing to pull).")
        return 0

    logger.info(f"Decrypt pull: {len(encrypted_names)} repo(s) GitHub -> Gitea")
    for n in encrypted_names:
        logger.info(f"  - {n}")
    if not args.yes and not args.dry_run:
        if (
            input(f"\nDecrypt and push {len(encrypted_names)} repo(s) to Gitea? (y/N): ")
            .strip()
            .lower()
            != "y"
        ):
            logger.info("Cancelled.")
            return 0

    work_root = Path(tempfile.mkdtemp(prefix="ggm-work-"))
    results: List[Dict[str, Any]] = []
    original = _install_sigint_handler(logger)
    try:
        with ThreadPoolExecutor(
            max_workers=args.workers or cfg["max_workers"], thread_name_prefix="pull"
        ) as pool:
            futures = {}
            for idx, name in enumerate(encrypted_names, 1):
                if _shutdown_event.is_set():
                    break
                github_clone = https_clone_url(
                    "https://github.com",
                    cfg["github_user"],
                    cfg["github_token"],
                    cfg["github_user"],
                    name,
                )
                gitea_push = https_clone_url(
                    cfg["gitea_url"], cfg["gitea_user"], cfg["gitea_token"], cfg["gitea_user"], name
                )
                fut = pool.submit(
                    pull_decrypted_repo,
                    repo_name=name,
                    github_clone_url=github_clone,
                    gitea_push_url=gitea_push,
                    gitea_api={
                        "base_url": cfg["gitea_url"],
                        "owner": cfg["gitea_user"],
                        "name": name,
                        "token": cfg["gitea_token"],
                        "private": True,
                    },
                    passphrase=cfg["passphrase"],
                    work_root=work_root,
                    dry_run=args.dry_run,
                    verify=not args.no_verify,
                    logger=logger,
                    secrets=secrets,
                )
                futures[fut] = (idx, name)
            for fut in as_completed(futures):
                idx, name = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # pragma: no cover - defensive
                    res = _result(name, time.time(), "failed", f"worker crashed: {e}")
                results.append(res)
                with _print_lock:
                    _log_result(logger, idx, len(encrypted_names), res)
    finally:
        signal.signal(signal.SIGINT, original)
        shutil.rmtree(work_root, ignore_errors=True)
    return _summarize(results, logger, args)


def _log_result(logger: logging.Logger, idx: int, total: int, res: Dict[str, Any]) -> None:
    status = res["status"]
    icon = {"success": "OK", "skipped": "SKIP", "failed": "FAIL"}.get(status, status)
    logger.info(f"[{idx}/{total}] {res['name']} ... {icon} ({res['duration']:.1f}s) {res['error']}")


def _summarize(
    results: List[Dict[str, Any]], logger: logging.Logger, args: argparse.Namespace
) -> int:
    counts = {"success": 0, "skipped": 0, "failed": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    logger.info("")
    logger.info("=" * 55)
    logger.info(
        f"Done: {counts['success']} ok, {counts['skipped']} skipped, {counts['failed']} failed"
    )
    for r in results:
        if r["status"] == "failed":
            logger.info(f"  FAIL {r['name']}: {r['error']}")
    logger.info("=" * 55)
    if _MIRROR_AVAILABLE and not args.dry_run and results:
        try:
            mirror.generate_report(results, sum(r["duration"] for r in results), 1, 0, "en", logger)
        except Exception:
            pass
    return 1 if counts["failed"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypted bidirectional mirror between Gitea and GitHub."
    )
    parser.add_argument(
        "mode",
        choices=["push", "pull"],
        help="push: Gitea->GitHub (encrypt). pull: GitHub->Gitea (decrypt).",
    )
    parser.add_argument(
        "--only", default=None, help="Comma-separated repo names to process (default: all)."
    )
    parser.add_argument(
        "--all-gitea",
        action="store_true",
        help="(push) include Gitea org repos, not just GITEA_USER's.",
    )
    parser.add_argument("--workers", type=int, default=None, help="Concurrent workers.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt.")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip decrypt-and-verify of bundles before pushing.",
    )
    args = parser.parse_args()
    if args.only:
        args.only = [o.strip() for o in args.only.split(",") if o.strip()]

    logger = setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    logger.info(f"Gitea <-> GitHub encrypted mirror v{VERSION} [{args.mode}]")
    if not _MIRROR_AVAILABLE:
        logger.error("mirror.py must sit next to encrypted_mirror.py (shared API helpers).")
        sys.exit(1)
    try:
        import cryptography  # noqa: F401  (fail fast with a clear message)
    except ImportError:
        logger.error(
            "The 'cryptography' package is required: pip install -r requirements-encrypted.txt"
        )
        sys.exit(1)

    cfg = _load_config(logger)
    if args.mode == "push":
        sys.exit(run_push(cfg, args, logger))
    else:
        sys.exit(run_pull(cfg, args, logger))


if __name__ == "__main__":
    main()
