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
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import encrypted_mirror as em

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
    if key in os.environ:
        return "environment"
    if key in _dotenv_values():
        return "ui"
    return "unset"


def write_ui_config(updates: Dict[str, str]) -> None:
    """Persist UI-edited values to DATA_DIR/.env (never touches real env vars)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = _read_dotenv(DOTENV_UI)
    for key, value in updates.items():
        if key in os.environ:
            continue  # environment-managed: read-only in the UI
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
        "repo_keys": get_all_repo_keys(),
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
# Database: per-repo keys + sync history
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
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
        conn.commit()
        g.db = conn
    return g.db


def get_all_repo_keys() -> Dict[str, str]:
    try:
        rows = _db().execute("SELECT repo, passphrase FROM repo_keys").fetchall()
        return {r["repo"]: r["passphrase"] for r in rows}
    except Exception:
        return {}


def set_repo_key(repo: str, passphrase: str) -> None:
    _db().execute(
        "INSERT OR REPLACE INTO repo_keys(repo, passphrase, updated_at) VALUES (?,?,?)",
        (repo, passphrase, _utcnow()),
    )
    _db().commit()


def clear_repo_key(repo: str) -> None:
    _db().execute("DELETE FROM repo_keys WHERE repo = ?", (repo,))
    _db().commit()


def record_runs(direction: str, started_at: str, results: List[Dict[str, Any]]) -> None:
    finished = _utcnow()
    db = _db()
    for r in results:
        db.execute(
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
    db.commit()


def last_run_per_repo() -> Dict[Tuple[str, str], sqlite3.Row]:
    rows = (
        _db()
        .execute(
            "SELECT repo, direction, status, started_at, detail FROM sync_runs "
            "WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY repo, direction)"
        )
        .fetchall()
    )
    return {(r["repo"], r["direction"]): r for r in rows}


def recent_runs(limit: int = 20) -> List[sqlite3.Row]:
    return (
        _db()
        .execute(
            "SELECT repo, direction, status, started_at, duration_s, detail FROM sync_runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )


def runs_last_24h() -> Dict[str, int]:
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    rows = (
        _db()
        .execute(
            "SELECT status, COUNT(*) AS n FROM sync_runs "
            "WHERE strftime('%s', started_at) > ? GROUP BY status",
            (cutoff,),
        )
        .fetchall()
    )
    return {r["status"]: r["n"] for r in rows}


def last_sync_overall() -> Optional[sqlite3.Row]:
    return (
        _db()
        .execute("SELECT direction, status, finished_at FROM sync_runs ORDER BY id DESC LIMIT 1")
        .fetchone()
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
    ui_password = _ui_password()
    if not ui_password:
        raise RuntimeError(
            "UI_PASSWORD is not set. Refusing to start the web UI without a password. "
            "Set the UI_PASSWORD environment variable (or add it to the .env file)."
        )

    app = Flask(__name__, template_folder=str(APP_DIR / "templates"))
    app.secret_key = hashlib.sha256(f"ggm-webui:{ui_password}".encode()).hexdigest()
    app.config["UI_PASSWORD"] = ui_password

    @app.teardown_appcontext
    def _close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

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
        fields = []
        for key, label, ftype, required in CONFIG_FIELDS:
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "type": ftype,
                    "required": required,
                    "value": "" if ftype == "password" else effective_value(key),
                    "is_set": bool(effective_value(key)) if ftype == "password" else None,
                    "source": value_source(key),
                    "readonly": value_source(key) == "environment",
                }
            )
        ui_pw_source = value_source("UI_PASSWORD")
        return render_template(
            "config.html",
            fields=fields,
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
        for key, _label, ftype, _required in CONFIG_FIELDS:
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
        if new_ui_pw and value_source("UI_PASSWORD") != "environment":
            session.clear()
            return redirect(url_for("login"))
        flash("Configuration saved.", "ok")
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
