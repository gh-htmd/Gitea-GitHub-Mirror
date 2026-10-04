#!/usr/bin/env python3
"""
Web UI for the encrypted Gitea <-> GitHub mirror.

Run with Docker (recommended):
    docker compose up --build mirror-ui        # serves on http://localhost:5000

Or directly:
    pip install -r requirements-webui.txt
    UI_PASSWORD="choose-a-strong-password" python3 webui.py

What it does
------------
- Dashboard with simple stats: repo counts, last sync, 24h results, recent runs.
- Repositories page: per-repo encryption keys (a custom key per repo, or fall
  back to the shared ENCRYPTION_PASSPHRASE), last push/pull status, and
  buttons to trigger an encrypted push or a decrypt pull per repo or for all.
- Config page: every setting (Gitea/GitHub credentials, shared passphrase,
  workers, schedule) is seeded from environment variables and editable in the
  UI. Values set via real environment variables are shown read-only; the rest
  are stored in the data directory's `.env` file.
- Built-in scheduler: optionally runs push/pull automatically on an interval.

Security
--------
- The UI is password-gated (UI_PASSWORD). It refuses to start without one.
- Passphrases are never rendered back into the UI; only "set / not set" badges.
- CSRF tokens on all mutating forms.

Layout (DATA_DIR, /app/data in Docker):
    data/
      .env          UI-managed config (env vars always win over this file)
      ui.db         repo_keys + sync_runs tables

License: MIT
"""

import hashlib
import logging
import os
import secrets as pysecrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import encrypted_mirror as em
import notify

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WEBUI_DATA_DIR", str(APP_DIR / "data")))
DOTENV_UI = DATA_DIR / ".env"  # UI-managed config file (persisted)
DOTENV_APP = APP_DIR / ".env"  # classic CLI config file (read-only fallback)
DB_PATH = DATA_DIR / "ui.db"

# (env key, label, field type, required)
CONFIG_FIELDS: List[Tuple[str, str, str, bool]] = [
    ("GITEA_URL", "Gitea URL", "text", True),
    ("GITEA_TOKEN", "Gitea API token", "password", True),
    ("GITEA_USER", "Gitea user (repo owner)", "text", True),
    ("GITHUB_TOKEN", "GitHub personal access token", "password", True),
    ("GITHUB_USER", "GitHub user", "text", True),
    ("ENCRYPTION_PASSPHRASE", "Shared encryption passphrase", "password", False),
    ("GITHUB_MIRROR_PRIVATE", "Create GitHub mirror repos as private", "bool", False),
    ("MAX_WORKERS", "Concurrent workers", "int", False),
    ("SKIP_REPOS", "Skip repos (comma-separated)", "text", False),
    ("SYNC_INTERVAL_HOURS", "Auto-sync interval (hours)", "float", False),
    ("AUTO_SYNC", "Enable scheduled sync", "bool", False),
    ("SYNC_DIRECTION", "Scheduled sync direction", "select", False),
]

# Notification settings (rendered as their own section on the Config page)
NOTIFY_FIELDS: List[Tuple[str, str, str, bool]] = [
    ("NOTIFY_MODE", "Notify me", "notify_mode", False),
    ("NOTIFY_TELEGRAM_BOT_TOKEN", "Telegram bot token", "password", False),
    ("NOTIFY_TELEGRAM_CHAT_ID", "Telegram chat ID", "text", False),
    ("NOTIFY_NTFY_TOPIC", "ntfy topic", "text", False),
    ("NOTIFY_NTFY_SERVER", "ntfy server", "text", False),
    ("NOTIFY_SMTP_HOST", "SMTP host", "text", False),
    ("NOTIFY_SMTP_PORT", "SMTP port", "int", False),
    ("NOTIFY_SMTP_USER", "SMTP username", "text", False),
    ("NOTIFY_SMTP_PASS", "SMTP password", "password", False),
    ("NOTIFY_SMTP_FROM", "Email from address", "text", False),
    ("NOTIFY_EMAIL_TO", "Email recipients (comma-separated)", "text", False),
]

NOTIFY_FIELD_HINTS = {
    "ENCRYPTION_PASSPHRASE": "The shared key used for repos without a custom per-repo key (see Repositories).",
    "SYNC_DIRECTION": "Which direction the scheduled sync runs.",
    "NOTIFY_MODE": "When to send notifications: on failures only, after every run, or never.",
    "NOTIFY_TELEGRAM_BOT_TOKEN": "Create a bot with @BotFather; paste its token here.",
    "NOTIFY_TELEGRAM_CHAT_ID": "Your chat ID (ask @userinfobot) or a group/channel ID.",
    "NOTIFY_NTFY_TOPIC": "Any topic name, e.g. mirror-alerts-xyz. Subscribe in the ntfy app.",
    "NOTIFY_NTFY_SERVER": "Defaults to https://ntfy.sh; use your own server if self-hosted.",
}

# Extra tunables for the encrypted sync (encrypted_mirror.py)
TUNING_FIELDS: List[Tuple[str, str, str, bool]] = [
    ("KEYS_FILE", "Per-repo keys file (JSON path)", "text", False),
    ("GIT_CLONE_TIMEOUT", "Git clone timeout (seconds)", "int", False),
    ("LOG_LEVEL", "Log verbosity", "log_level", False),
]

# Settings for the classic mirror.py CLI (used when running it via docker/command line)
CLASSIC_FIELDS: List[Tuple[str, str, str, bool]] = [
    ("PRESERVE_ORGS", "Strict organization replication", "bool", False),
    ("SYNC_NOW", "Sync now (existing mirrors)", "bool", False),
    ("FORCE_RECREATE", "Delete and recreate all mirrors", "bool", False),
    ("MIRROR_INTERVAL", "Mirror sync interval", "text", False),
    ("MIRROR_LFS", "Enable Git LFS on migrated mirrors", "bool", False),
    ("MIRROR_EXTRAS", "Migrate wiki/issues/PRs/labels", "bool", False),
    ("LANG_MIRROR", "CLI language", "lang", False),
    ("MAX_RETRIES", "Max retries per repo", "int", False),
    ("RETRY_DELAY", "Initial retry delay (seconds)", "int", False),
    ("REQUEST_TIMEOUT", "HTTP timeout per request (seconds)", "int", False),
    ("REPORT_MAX_COUNT", "Archived reports to keep", "int", False),
    ("NOTIFY_WEBHOOK", "Webhook URL (classic mirror)", "text", False),
    ("NOTIFY_TYPE", "Webhook type", "notify_type", False),
    ("NOTIFY_CHAT_ID", "Telegram chat ID (classic mirror)", "text", False),
    ("NOTIFY_ONLY_ON_FAILURE", "Classic webhook: only on failure", "bool", False),
    ("NOTIFY_INCLUDE_REPORT", "Classic webhook: include report", "bool", False),
]

# (section title, fields, description) rendered in this order on the Config page
CONFIG_GROUPS: List[Tuple[str, list, str]] = [
    ("Mirror", CONFIG_FIELDS, "Connection, encryption and scheduling for the encrypted sync."),
    ("Encrypted sync", TUNING_FIELDS, "Fine-tuning for the encrypted push/pull engine."),
    (
        "Classic mirror",
        CLASSIC_FIELDS,
        "Applies when running the classic mirror.py directly (docker service or "
        "command line). The web UI itself runs the encrypted sync above.",
    ),
    ("Notifications", NOTIFY_FIELDS, "Alerts for finished sync runs (Telegram, ntfy, email)."),
]

FIELD_HINTS = {
    **NOTIFY_FIELD_HINTS,
    "KEYS_FILE": "JSON file mapping repo names to passphrases. The UI's per-repo keys "
    "take precedence; this is an extra fallback, mainly for CLI runs.",
    "GIT_CLONE_TIMEOUT": "How long a single git clone may take before it is aborted.",
    "LOG_LEVEL": "Applies to the sync engine logs.",
    "PRESERVE_ORGS": "Replicate the GitHub organization structure strictly on Gitea.",
    "SYNC_NOW": "Trigger an immediate sync of already-mirrored repos on the next run.",
    "FORCE_RECREATE": "DANGER: deletes every mirrored repo on Gitea and migrates from scratch.",
    "MIRROR_INTERVAL": "How often Gitea pull-mirrors sync, e.g. 8h0m0s.",
    "MIRROR_LFS": "Enable Git LFS when creating the Gitea mirrors.",
    "MIRROR_EXTRAS": "Also migrate wiki, issues, pull requests and labels.",
    "MAX_RETRIES": "How many times a failed repo is retried.",
    "RETRY_DELAY": "Wait between retries (exponential backoff).",
    "REQUEST_TIMEOUT": "Per-request HTTP timeout for API calls.",
    "REPORT_MAX_COUNT": "Old run reports are pruned beyond this count.",
    "NOTIFY_WEBHOOK": "Where the classic mirror posts its webhook (see NOTIFY_TYPE).",
    "NOTIFY_TYPE": "Which chat system's format the webhook uses.",
    "NOTIFY_CHAT_ID": "Needed only for the Telegram webhook type.",
}

ALL_FIELD_KEYS = [f[0] for _, fields, _ in CONFIG_GROUPS for f in fields]

log = logging.getLogger("webui")


# ---------------------------------------------------------------------------
# Config: env vars seed everything, UI edits persist to DATA_DIR/.env
# ---------------------------------------------------------------------------
def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in values:
            values[key] = value
    return values


def _dotenv_values() -> Dict[str, str]:
    """UI-managed file wins over the classic .env; real env vars win over both."""
    merged = _read_dotenv(DOTENV_APP)
    merged.update(_read_dotenv(DOTENV_UI))
    return merged


def effective_value(key: str) -> str:
    if key in os.environ:
        return os.environ[key]
    return _dotenv_values().get(key, "")


def value_source(key: str) -> str:
    """Where the effective value comes from: environment | ui | unset."""
    if key in os.environ and key not in _exported:
        return "environment"
    if key in _dotenv_values():
        return "ui"
    return "unset"


_exported: Dict[str, str] = {}  # key -> value this process placed into os.environ


def export_ui_env() -> None:
    """Publish UI-saved values into os.environ for code that reads it directly.

    encrypted_mirror.py and notify.py read several settings straight from
    os.environ (LOG_LEVEL, KEYS_FILE, GIT_CLONE_TIMEOUT, ...). Genuine process
    environment variables always win; empty UI values clear a previous export.
    Called at startup and after every config save.
    """
    merged = _dotenv_values()
    for key in ALL_FIELD_KEYS:
        current = os.environ.get(key)
        if key in _exported:
            if current != _exported[key]:
                # A genuine environment value appeared under our export:
                # adopt it and stop managing this key.
                del _exported[key]
                continue
        elif current:
            continue  # genuine pre-existing environment value: leave untouched
        value = merged.get(key, "")
        if value:
            os.environ[key] = value
            _exported[key] = value
        else:
            os.environ.pop(key, None)
            _exported.pop(key, None)


def write_ui_config(updates: Dict[str, str]) -> None:
    """Persist UI-edited values to DATA_DIR/.env (never touches real env vars)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = _read_dotenv(DOTENV_UI)
    for key, value in updates.items():
        if key in os.environ and key not in _exported:
            continue  # genuine environment value: read-only in the UI
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    lines = [f"{k}={v}" for k, v in sorted(current.items())]
    DOTENV_UI.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_cfg() -> Dict[str, Any]:
    """Assemble an encrypted_mirror cfg dict from effective config + DB repo keys."""
    repo_keys = get_all_repo_keys()
    keys_file = effective_value("KEYS_FILE")
    if keys_file:
        try:
            file_keys = em.load_keys_file(keys_file)
            repo_keys = {**file_keys, **repo_keys}  # UI keys take precedence
        except Exception as e:
            log.warning("Could not load KEYS_FILE %s: %s", keys_file, e)
    return {
        "gitea_url": effective_value("GITEA_URL").rstrip("/"),
        "gitea_token": effective_value("GITEA_TOKEN"),
        "gitea_user": effective_value("GITEA_USER"),
        "github_token": effective_value("GITHUB_TOKEN"),
        "github_user": effective_value("GITHUB_USER"),
        "passphrase": effective_value("ENCRYPTION_PASSPHRASE"),
        "github_private": _as_bool(effective_value("GITHUB_MIRROR_PRIVATE") or "true"),
        "max_workers": int(effective_value("MAX_WORKERS") or "5"),
        "skip_repos": {r.strip() for r in effective_value("SKIP_REPOS").split(",") if r.strip()},
        "repo_keys": repo_keys,
    }


def cfg_problems(cfg: Dict[str, Any]) -> List[str]:
    problems = []
    for key, label in [
        ("gitea_url", "GITEA_URL"),
        ("gitea_token", "GITEA_TOKEN"),
        ("gitea_user", "GITEA_USER"),
        ("github_token", "GITHUB_TOKEN"),
        ("github_user", "GITHUB_USER"),
    ]:
        if not cfg[key]:
            problems.append(f"{label} is not set")
    if not cfg["passphrase"] and not cfg["repo_keys"]:
        problems.append("No encryption key: set ENCRYPTION_PASSPHRASE or per-repo keys")
    return problems


# ---------------------------------------------------------------------------
# Database: per-repo keys + sync history + bundle sizes.
# Thread-safe: syncs run in background threads (no Flask `g` there), so every
# operation opens its own short-lived connection, serialized by a lock.
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()


def _db_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS repo_keys("
        "repo TEXT PRIMARY KEY, passphrase TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_runs("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, direction TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, "
        "duration_s REAL, detail TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_repo ON sync_runs(repo, direction, started_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bundle_sizes("
        "repo TEXT NOT NULL, recorded_at TEXT NOT NULL, bytes INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sizes_repo ON bundle_sizes(repo, recorded_at)")
    conn.commit()
    return conn


def get_all_repo_keys() -> Dict[str, str]:
    try:
        with _db_lock:
            conn = _db_conn()
            try:
                rows = conn.execute("SELECT repo, passphrase FROM repo_keys").fetchall()
            finally:
                conn.close()
        return {r["repo"]: r["passphrase"] for r in rows}
    except Exception:
        return {}


def set_repo_key(repo: str, passphrase: str) -> None:
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO repo_keys(repo, passphrase, updated_at) VALUES (?,?,?)",
                (repo, passphrase, _utcnow()),
            )
            conn.commit()
        finally:
            conn.close()


def clear_repo_key(repo: str) -> None:
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("DELETE FROM repo_keys WHERE repo = ?", (repo,))
            conn.commit()
        finally:
            conn.close()


def record_runs(direction: str, started_at: str, results: List[Dict[str, Any]]) -> None:
    finished = _utcnow()
    with _db_lock:
        conn = _db_conn()
        try:
            for r in results:
                conn.execute(
                    "INSERT INTO sync_runs(repo, direction, status, started_at, finished_at,"
                    " duration_s, detail) VALUES (?,?,?,?,?,?,?)",
                    (
                        r.get("name", "?"),
                        direction,
                        r.get("status", "failed"),
                        started_at,
                        finished,
                        float(r.get("duration", 0) or 0),
                        str(r.get("error", ""))[:500],
                    ),
                )
                # Track encrypted bundle sizes for the storage stats (push only).
                size = r.get("bytes")
                if direction == "push" and r.get("status") == "success" and isinstance(size, int):
                    conn.execute(
                        "INSERT INTO bundle_sizes(repo, recorded_at, bytes) VALUES (?,?,?)",
                        (r.get("name", "?"), finished, size),
                    )
            # Prune: keep the last 200 measurements per repo.
            conn.execute(
                "DELETE FROM bundle_sizes WHERE rowid IN ("
                "SELECT rowid FROM ("
                "SELECT rowid, ROW_NUMBER() OVER (PARTITION BY repo ORDER BY recorded_at DESC)"
                " AS rn FROM bundle_sizes) WHERE rn > 200)"
            )
            conn.commit()
        finally:
            conn.close()


def last_run_per_repo() -> Dict[Tuple[str, str], sqlite3.Row]:
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT repo, direction, status, started_at, detail FROM sync_runs "
                "WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY repo, direction)"
            ).fetchall()
        finally:
            conn.close()
    return {(r["repo"], r["direction"]): r for r in rows}


def recent_runs(limit: int = 20) -> List[sqlite3.Row]:
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT repo, direction, status, started_at, duration_s, detail FROM sync_runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return rows


def runs_last_24h() -> Dict[str, int]:
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM sync_runs "
                "WHERE strftime('%s', started_at) > ? GROUP BY status",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    return {r["status"]: r["n"] for r in rows}


def last_sync_overall() -> Optional[sqlite3.Row]:
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute(
                "SELECT direction, status, finished_at FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    return row


def storage_latest() -> List[sqlite3.Row]:
    """Latest encrypted bundle size per repo (for the storage panel)."""
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT repo, bytes, recorded_at FROM bundle_sizes "
                "WHERE rowid IN (SELECT MAX(rowid) FROM bundle_sizes GROUP BY repo) "
                "ORDER BY bytes DESC"
            ).fetchall()
        finally:
            conn.close()
    return rows


def storage_daily_totals(days: int = 90) -> List[Tuple[str, int]]:
    """Total encrypted bytes per day (latest measurement per repo per day)."""
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT date(recorded_at) AS d, SUM(bytes) AS total FROM bundle_sizes "
                "WHERE rowid IN ("
                "  SELECT MAX(rowid) FROM bundle_sizes GROUP BY repo, date(recorded_at)) "
                "AND recorded_at > datetime('now', ?) "
                "GROUP BY d ORDER BY d",
                (f"-{days} days",),
            ).fetchall()
        finally:
            conn.close()
    return [(r["d"], r["total"]) for r in rows]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def human_bytes(n: int) -> str:
    """Format a byte count for display (B/KB/MB/GB/TB)."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def sparkline_svg(points: List[Tuple[str, int]], width: int = 560, height: int = 110) -> str:
    """Inline SVG area chart of (label, value) points. Dependency-free."""
    if not points:
        return ""
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pad = 8
    n = len(points)
    step_x = (width - 2 * pad) / max(n - 1, 1)

    def xy(i: int, v: int) -> Tuple[float, float]:
        x = pad + i * step_x
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        return x, y

    pts = " ".join(f"{x:.1f},{y:.1f}" for i, (_, v) in enumerate(points) for x, y in [xy(i, v)])
    last_x, last_y = xy(n - 1, values[-1])
    first_x, _ = xy(0, values[0])
    area = f"{pts} {last_x:.1f},{height - pad} {first_x:.1f},{height - pad}"
    first_label, last_label = points[0][0], points[-1][0]
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="storage over time">'
        f'<polygon points="{area}" fill="#dbeafe"/>'
        f'<polyline points="{pts}" fill="none" stroke="#2563eb" stroke-width="2"/>'
        f'<text x="{pad}" y="{height - 1}" font-size="10" fill="#6b7484">{first_label}</text>'
        f'<text x="{width - pad}" y="{height - 1}" font-size="10" fill="#6b7484" '
        f'text-anchor="end">{last_label}</text>'
        f'<text x="{width - pad}" y="{pad + 6}" font-size="10" fill="#1c2330" text-anchor="end">'
        f"{human_bytes(hi)}</text>"
        "</svg>"
    )


def notify_settings() -> Dict[str, str]:
    """Notification settings from the effective config (env first, then UI .env)."""
    return {k: effective_value(k) for k in notify.SETTING_KEYS}


# ---------------------------------------------------------------------------
# Sync execution (background threads)
# ---------------------------------------------------------------------------
_run_state_lock = threading.Lock()
_active_runs: Dict[str, bool] = {}


def is_running(mode: str) -> bool:
    with _run_state_lock:
        return bool(_active_runs.get(mode))


def run_sync(mode: str, only: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Run an encrypted push/pull in the calling thread. Returns (started, message)."""
    with _run_state_lock:
        if _active_runs.get(mode):
            return False, f"A {mode} sync is already running."
        _active_runs[mode] = True
    started_at = _utcnow()
    try:
        cfg = build_cfg()
        problems = cfg_problems(cfg)
        if problems:
            record_runs(
                mode,
                started_at,
                [
                    {
                        "name": "-",
                        "status": "failed",
                        "duration": 0,
                        "error": "Config incomplete: " + "; ".join(problems),
                    }
                ],
            )
            return False, "Config incomplete: " + "; ".join(problems)
        cfg["result_sink"] = lambda results: record_runs(mode, started_at, results)
        cfg["notify_settings"] = notify_settings()
        args = SimpleNamespace(
            only=only, workers=None, dry_run=False, yes=True, no_verify=False, all_gitea=False
        )
        sync_log = logging.getLogger("webui-sync")
        try:
            if mode == "push":
                rc = em.run_push(cfg, args, sync_log)
            else:
                rc = em.run_pull(cfg, args, sync_log)
        except SystemExit as e:
            record_runs(
                mode,
                started_at,
                [{"name": "-", "status": "failed", "duration": 0, "error": f"aborted: {e}"}],
            )
            return False, f"Sync aborted: {e}"
        except Exception as e:  # never kill the scheduler thread silently
            record_runs(
                mode,
                started_at,
                [{"name": "-", "status": "failed", "duration": 0, "error": f"crashed: {e}"}],
            )
            return False, f"Sync crashed: {e}"
        return True, f"Sync finished (exit code {rc})."
    finally:
        with _run_state_lock:
            _active_runs.pop(mode, None)


def _run_in_background(mode: str, only: Optional[List[str]] = None) -> None:
    thread = threading.Thread(target=run_sync, args=(mode, only), daemon=True, name=f"sync-{mode}")
    thread.start()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
def _ui_password() -> str:
    pw = os.environ.get("UI_PASSWORD", "") or _dotenv_values().get("UI_PASSWORD", "")
    return pw


def create_app() -> Flask:
    export_ui_env()  # UI-saved values become visible to os.environ readers
    ui_password = _ui_password()
    if not ui_password:
        raise RuntimeError(
            "UI_PASSWORD is not set. Refusing to start the web UI without a password. "
            "Set the UI_PASSWORD environment variable (or add it to the .env file)."
        )

    app = Flask(__name__, template_folder=str(APP_DIR / "templates"))
    app.secret_key = hashlib.sha256(f"ggm-webui:{ui_password}".encode()).hexdigest()
    app.config["UI_PASSWORD"] = ui_password

    def login_required(view):
        from functools import wraps

        @wraps(view)
        def wrapped(*a, **kw):
            if not session.get("authed"):
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)

        return wrapped

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = pysecrets.token_hex(16)
        return session["csrf"]

    def check_csrf() -> bool:
        return session.get("csrf") and request.form.get("csrf_token") == session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    # -- tiny live caches (avoid hammering the APIs on every page view) --
    cache: Dict[str, Any] = {}

    def cached(key: str, ttl: int, fn):
        now = time.time()
        entry = cache.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
        try:
            value = fn()
        except Exception as e:
            value = e
        cache[key] = (now, value)
        return value

    def fetch_gitea_repo_names() -> List[str]:
        cfg = build_cfg()

        def _fetch():
            repos = em.mirror.fetch_gitea_repos(cfg["gitea_url"], cfg["gitea_token"], log)
            return sorted(
                info["name"]
                for info in repos.values()
                if info.get("owner", "").lower() == cfg["gitea_user"].lower()
            )

        result = cached("gitea_repos", 300, _fetch)
        if isinstance(result, Exception):
            raise result
        names = [n for n in result if n not in cfg["skip_repos"]]
        return names

    def count_github_encrypted() -> int:
        cfg = build_cfg()

        def _fetch():
            repos = em.mirror.fetch_github_repos(cfg["github_token"], log)
            n = 0
            for r in repos:
                if r.get("owner", {}).get("login", "").lower() != cfg["github_user"].lower():
                    continue
                try:
                    if em.github_has_encrypted_marker(
                        cfg["github_user"], r["name"], cfg["github_token"]
                    ):
                        n += 1
                except Exception:
                    pass
            return n

        result = cached("github_encrypted", 300, _fetch)
        if isinstance(result, Exception):
            raise result
        return result

    # -- routes --
    @app.get("/login")
    def login():
        if session.get("authed"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None)

    @app.post("/login")
    def login_post():
        if request.form.get("password", "") == app.config["UI_PASSWORD"]:
            session["authed"] = True
            session["csrf"] = pysecrets.token_hex(16)
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html", error="Wrong password."), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        stats = {"gitea_repos": "?", "github_encrypted": "?", "error": None}
        try:
            stats["gitea_repos"] = len(fetch_gitea_repo_names())
        except Exception as e:
            stats["error"] = f"Could not reach Gitea: {e}"
        if not stats["error"]:
            try:
                stats["github_encrypted"] = count_github_encrypted()
            except Exception as e:
                stats["error"] = f"Could not reach GitHub: {e}"
        sizes = storage_latest()
        total_bytes = sum(r["bytes"] for r in sizes)
        history = storage_daily_totals(90)
        return render_template(
            "dashboard.html",
            stats=stats,
            last_sync=last_sync_overall(),
            last_24h=runs_last_24h(),
            recent=recent_runs(15),
            auto_sync=_as_bool(effective_value("AUTO_SYNC")),
            interval=effective_value("SYNC_INTERVAL_HOURS") or "12",
            direction=effective_value("SYNC_DIRECTION") or "push",
            running={"push": is_running("push"), "pull": is_running("pull")},
            storage_total=human_bytes(total_bytes),
            storage_rows=[
                {
                    "repo": r["repo"],
                    "bytes": r["bytes"],
                    "human": human_bytes(r["bytes"]),
                    "pct": round(100 * r["bytes"] / total_bytes) if total_bytes else 0,
                    "recorded_at": r["recorded_at"],
                }
                for r in sizes
            ],
            storage_chart=sparkline_svg(history),
            notify_channels=notify.configured_channels(notify_settings()),
        )

    @app.get("/repos")
    @login_required
    def repos():
        try:
            names = fetch_gitea_repo_names()
            error = None
        except Exception as e:
            names = []
            error = f"Could not reach Gitea: {e}"
        keys = get_all_repo_keys()
        last = last_run_per_repo()
        rows = [
            {
                "name": n,
                "key": "custom" if n in keys else "global",
                "push": last.get((n, "push")),
                "pull": last.get((n, "pull")),
            }
            for n in names
        ]
        return render_template(
            "repos.html",
            rows=rows,
            error=error,
            global_key_set=bool(effective_value("ENCRYPTION_PASSPHRASE")),
            running={"push": is_running("push"), "pull": is_running("pull")},
        )

    @app.post("/repos/<repo>/key")
    @login_required
    def set_key(repo):
        if not check_csrf():
            flash("Invalid form token. Please try again.", "error")
            return redirect(url_for("repos"))
        passphrase = request.form.get("passphrase", "")
        if not passphrase:
            flash("Passphrase must not be empty.", "error")
        else:
            set_repo_key(repo, passphrase)
            flash(f"Custom encryption key set for {repo}.", "ok")
        return redirect(url_for("repos"))

    @app.post("/repos/<repo>/key/clear")
    @login_required
    def clear_key(repo):
        if not check_csrf():
            flash("Invalid form token. Please try again.", "error")
            return redirect(url_for("repos"))
        clear_repo_key(repo)
        flash(f"{repo} now uses the shared encryption key.", "ok")
        return redirect(url_for("repos"))

    @app.post("/run")
    @login_required
    def run():
        if not check_csrf():
            flash("Invalid form token. Please try again.", "error")
            return redirect(request.form.get("next") or url_for("dashboard"))
        mode = request.form.get("mode", "push")
        if mode not in ("push", "pull"):
            flash("Unknown sync mode.", "error")
            return redirect(url_for("dashboard"))
        only = [request.form.get("repo")] if request.form.get("repo") else None

        def _go():
            ok, msg = run_sync(mode, only)
            # flash from a thread is unsafe; record instead and surface via cache
            cache["last_trigger"] = (time.time(), msg, ok)

        threading.Thread(target=_go, daemon=True, name=f"trigger-{mode}").start()
        flash(f"{mode.capitalize()} sync started in the background.", "ok")
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.get("/runs")
    @login_required
    def runs():
        return render_template("runs.html", runs=recent_runs(100))

    @app.get("/config")
    @login_required
    def config():
        def _field(key, label, ftype, required):
            options = None
            if ftype == "select":
                options = ["push", "pull", "both"]
            elif ftype == "notify_mode":
                options = ["failures", "always", "never"]
            elif ftype == "log_level":
                options = ["DEBUG", "INFO", "WARNING", "ERROR"]
            elif ftype == "lang":
                options = ["en", "cn"]
            elif ftype == "notify_type":
                options = ["slack", "discord", "teams", "feishu", "dingtalk", "telegram", "generic"]
            return {
                "key": key,
                "label": label,
                "type": ftype,
                "required": required,
                "options": options,
                "value": "" if ftype == "password" else effective_value(key),
                "is_set": bool(effective_value(key)) if ftype == "password" else None,
                "source": value_source(key),
                "readonly": value_source(key) == "environment",
                "hint": FIELD_HINTS.get(key, ""),
            }

        groups = []
        for title, fields, description in CONFIG_GROUPS:
            groups.append(
                {
                    "title": title,
                    "description": description,
                    "fields": [_field(*f) for f in fields],
                }
            )
        ui_pw_source = value_source("UI_PASSWORD")
        return render_template(
            "config.html",
            groups=groups,
            notify_channels=notify.configured_channels(notify_settings()),
            ui_pw_source=ui_pw_source,
            ui_pw_readonly=ui_pw_source == "environment",
        )

    @app.post("/config")
    @login_required
    def config_post():
        if not check_csrf():
            flash("Invalid form token. Please try again.", "error")
            return redirect(url_for("config"))
        updates: Dict[str, str] = {}
        for _title, fields, _desc in CONFIG_GROUPS:
            for key, _label, ftype, _required in fields:
                if value_source(key) == "environment":
                    continue  # read-only: managed by the environment
                raw = request.form.get(key, "")
                if ftype == "bool":
                    updates[key] = "true" if raw == "on" else "false"
                elif ftype == "password":
                    if raw:  # empty password field = leave unchanged
                        updates[key] = raw
                else:
                    updates[key] = raw.strip()
        new_ui_pw = request.form.get("UI_PASSWORD", "")
        if new_ui_pw and value_source("UI_PASSWORD") != "environment":
            updates["UI_PASSWORD"] = new_ui_pw
        write_ui_config(updates)
        export_ui_env()  # make the new values visible to os.environ readers
        if new_ui_pw and value_source("UI_PASSWORD") != "environment":
            session.clear()
            return redirect(url_for("login"))
        flash("Configuration saved.", "ok")
        return redirect(url_for("config"))

    @app.post("/config/test-notify")
    @login_required
    def test_notify():
        if not check_csrf():
            flash("Invalid form token. Please try again.", "error")
            return redirect(url_for("config"))
        settings = notify_settings()
        channels = notify.configured_channels(settings)
        if not channels:
            flash("No notification channels configured yet.", "error")
            return redirect(url_for("config"))
        outcomes = notify.test_message(settings)
        for o in outcomes:
            if o["ok"]:
                flash(f"Test notification sent via {o['channel']}.", "ok")
            else:
                flash(f"{o['channel']} failed: {o['error']}", "error")
        return redirect(url_for("config"))

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Scheduler (started in the server process, not in tests)
# ---------------------------------------------------------------------------
def scheduler_loop() -> None:
    log.info("Scheduler started.")
    while True:
        try:
            interval_h = float(effective_value("SYNC_INTERVAL_HOURS") or "12")
        except ValueError:
            interval_h = 12
        time.sleep(max(interval_h, 0.05) * 3600)
        try:
            if not _as_bool(effective_value("AUTO_SYNC")):
                continue
            direction = (effective_value("SYNC_DIRECTION") or "push").strip().lower()
            modes = ["push", "pull"] if direction == "both" else [direction]
            for mode in modes:
                if mode not in ("push", "pull"):
                    continue
                with app.app_context():
                    ok, msg = run_sync(mode)
                log.info("Scheduled %s sync: %s (%s)", mode, msg, "started" if ok else "skipped")
        except Exception as e:  # never kill the scheduler thread
            log.exception("Scheduler error: %s", e)


if os.environ.get("WEBUI_NO_SCHEDULER") != "1":
    _sched = threading.Thread(target=scheduler_loop, daemon=True, name="webui-scheduler")
    _sched.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    port = int(os.environ.get("PORT", "5000"))
    # Dev server is fine for a personal admin UI; Docker uses gunicorn (see Dockerfile).
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
