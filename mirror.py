#!/usr/bin/env python3
"""
Gitea GitHub Mirror - Bulk mirror all GitHub repositories to a self-hosted Gitea instance.

Uses concurrent workers to parallelize the synchronous Gitea migration API,
while strictly validating each response (HTTP 201 = confirmed success).

Usage:
    python3 mirror.py [--lang en|cn] [--yes] [--include-orgs] [--dry-run] [--workers N]

Environment Variables (or .env file):
    GITEA_URL           - Your Gitea instance URL (e.g. https://git.example.com)
    GITEA_TOKEN         - Gitea API access token
    GITEA_USER          - Gitea username (repo owner)
    GITHUB_TOKEN        - GitHub personal access token (with 'repo' scope)
    GITHUB_USER         - GitHub username for filtering owner repos
    MIRROR_INTERVAL     - (Optional) Mirror sync interval (e.g. '8h0m0s')
    MIRROR_LFS          - (Optional) Enable Git LFS for migrated mirrors (true/false)
    NOTIFY_WEBHOOK      - (Optional) Webhook URL for notifications
    NOTIFY_TYPE         - (Optional) Webhook type override:
                          slack|discord|teams|feishu|dingtalk|telegram|generic
    NOTIFY_CHAT_ID      - (Optional) Telegram chat_id when using Telegram webhook
    NOTIFY_ONLY_ON_FAILURE - (Optional) Send webhook only when failures occur (true/false)
    NOTIFY_INCLUDE_REPORT  - (Optional) Include report content in webhook (true/false)
    REQUEST_TIMEOUT     - (Optional) HTTP timeout per request in seconds (default: 600)
    MAX_RETRIES         - (Optional) Max retry attempts per repo (default: 3)
    RETRY_DELAY         - (Optional) Initial retry delay in seconds (default: 10)
    MAX_WORKERS         - (Optional) Concurrent worker threads (default: 5)
    LOG_LEVEL           - (Optional) DEBUG, INFO, WARNING, ERROR (default: INFO)
    REPORT_MAX_COUNT    - (Optional) Max archived reports to keep (default: 50)
    LANG_MIRROR         - (Optional) Language: 'en' or 'cn' (default: en)

License: MIT
"""

import argparse
import glob
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "2.8.0"
SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
REPORTS_DIR = SCRIPT_DIR / "reports"
ENV_FILE = SCRIPT_DIR / ".env"

# ---------------------------------------------------------------------------
# Internationalization (i18n)
# ---------------------------------------------------------------------------
MESSAGES = {
    "en": {
        "banner": f"""
╔══════════════════════════════════════════════════════════╗
║    Gitea ⇄ GitHub  Bulk Mirror Tool  v{VERSION}           ║
║    Concurrent • Strict • Reliable                       ║
╚══════════════════════════════════════════════════════════╝""",
        "prompt_github_token": "👉 Paste your GitHub Token (hidden input, press Enter): ",
        "fetching": "\n🔍 Fetching your GitHub repository list via API...",
        "fetch_fail": "\n❌ Failed to fetch GitHub repos. Check your token. Error: {}",
        "rate_limit_hit": "\n⏳ GitHub rate limit hit. Sleeping until reset at {} ({}s)...",
        "rate_limit_skip": "\n⚠️  GitHub rate limit reset time missing; retrying after {}s...",
        "notify_sent": "\n🔔 Notification sent via webhook.",
        "notify_failed": "\n⚠️  Notification failed: {}",
        "dry_run_diff": (
            "\n🧪 Dry-run: {} new repo(s) would be migrated, {} already exist and would be skipped."
        ),
        "fetching_gitea": "\n🔍 Fetching existing repositories from Gitea...",
        "fetch_gitea_fail": (
            "\n⚠️  Failed to fetch Gitea repos. Continuing without incremental skip. Error: {}"
        ),
        "incremental_summary_legacy": "\n🧮 Incremental sync: {} existing on Gitea, {} will be skipped.",
        "status_header": "\n📋 Repo status (GitHub selection vs Gitea)",
        "status_legend": "Legend: {} GitHub-only  {} In both  {} Gitea-only",
        "status_github_only": "GitHub-only",
        "status_both": "In both (healthy)",
        "status_broken": "🔧 Broken mirror (empty)",
        "status_gitea_only": "Gitea-only",
        "broken_found": "\n🔧 Found {} broken mirror(s) (empty shell from previous failed migration).",
        "broken_repairing": "🛠️  Auto-repairing: deleting broken shells for re-migration...",
        "broken_deleted": "   ✖ Deleted broken shell: {}",
        "broken_delete_fail": "   ⚠️  Failed to delete: {}",
        "broken_repaired": "✅ Repaired {} broken mirror(s). They will be re-migrated.",
        "cleanup_shell": "🧹 Cleaned up broken shell left by failed migration: {}",
        "incremental_summary": "\n🧲 Incremental sync: {} on Gitea ({} healthy, {} broken to repair), {} new.",
        "scan_result": "\n📦 Scanned {} repos total (Owned: {}, Org/Collab: {}).",
        "section_owned": "【Your Repositories】",
        "section_other": "\n【Organization & Collaborator Repositories】({} repos)",
        "vis_private": "🔒Private",
        "vis_public": "🌐Public ",
        "ask_include_orgs": "\n👉 Also mirror the {} org/collab repos? (y/N): ",
        "include_orgs_yes": "-> Including ALL {} repos.",
        "include_orgs_no": "-> Only your {} owned repos.",
        "confirm": "\n⚠️  CONFIRM: Use {} workers to create {} pull-mirror(s) on Gitea? (y/N): ",
        "cancelled": "Cancelled.",
        "starting": "\n🚀 Starting concurrent migration ({} workers, {}s timeout per request)...",
        "mirroring": "[{}/{}] {} ... ",
        "success": "✅ Created ({:.1f}s)",
        "already_exists": "⏭️  Exists",
        "blocked": "🚫 Blocked (GitHub denied access)",
        "retry": "⏳ Retry {}/{} in {}s ({})",
        "failed": "❌ FAILED: {}",
        "done": "\n🎉 Migration complete!",
        "report_header": "Execution Report",
        "report_total": "Total repos",
        "report_success": "Successfully created",
        "report_skipped": "Already existed",
        "report_blocked": "Blocked by GitHub",
        "report_failed": "Failed",
        "report_duration": "Total duration",
        "report_avg": "Avg per repo",
        "report_workers": "Concurrent workers",
        "report_timeout": "Request timeout",
        "report_failed_list": "Failed Repositories",
        "report_blocked_list": "Blocked Repositories",
        "report_saved": "📄 Report: {}",
        "dry_run_prefix": "[DRY-RUN] ",
        "env_missing": "❌ Required env var '{}' not set. Configure .env or export it.",
    },
    "cn": {
        "banner": f"""
╔══════════════════════════════════════════════════════════╗
║    Gitea ⇄ GitHub  批量镜像同步工具  v{VERSION}           ║
║    并发执行 • 严格校验 • 结果可靠                       ║
╚══════════════════════════════════════════════════════════╝""",
        "prompt_github_token": "👉 请粘贴 GitHub Token (输入不可见，按回车确认): ",
        "fetching": "\n🔍 正在获取 GitHub 仓库列表...",
        "fetch_fail": "\n❌ 获取 GitHub 仓库失败，请检查 Token。错误: {}",
        "rate_limit_hit": "\n⏳ 触发 GitHub 速率限制，等待至重置时间 {}（{}s）...",
        "rate_limit_skip": "\n⚠️  未获取到重置时间，{}s 后重试...",
        "notify_sent": "\n🔔 已通过 Webhook 发送通知。",
        "notify_failed": "\n⚠️  通知发送失败: {}",
        "dry_run_diff": "\n🧪 演习结果: 发现 {} 个新仓库需要迁移，{} 个已存在将被跳过。",
        "fetching_gitea": "\n🔍 正在获取 Gitea 已存在仓库列表...",
        "fetch_gitea_fail": "\n⚠️  获取 Gitea 仓库失败，继续执行但不做增量过滤。错误: {}",
        "incremental_summary_legacy": "\n🧲 增量同步: Gitea 已存在 {} 个，将跳过 {} 个。",
        "status_header": "\n📋 仓库状态对比 (GitHub 选择范围 vs Gitea)",
        "status_legend": "图例: {} GitHub 独有  {} 两端都有  {} Gitea 独有",
        "status_github_only": "GitHub 独有",
        "status_both": "两端都有 (健康)",
        "status_broken": "🔧 损坏镜像 (空壳)",
        "status_gitea_only": "Gitea 独有",
        "broken_found": "\n🔧 发现 {} 个损坏镜像（上次迁移失败后留下的空壳）。",
        "broken_repairing": "🛠️  自动修复: 删除空壳以便重新迁移...",
        "broken_deleted": "   ✖ 已删除空壳: {}",
        "broken_delete_fail": "   ⚠️  删除失败: {}",
        "broken_repaired": "✅ 已修复 {} 个损坏镜像，它们将被重新迁移。",
        "cleanup_shell": "🧹 已清理迁移失败后留下的空壳: {}",
        "incremental_summary": "\n🧲 增量同步: Gitea 上 {} 个 ({} 个健康, {} 个损坏待修复), {} 个新仓库。",
        "scan_result": "\n📦 共扫描到 {} 个仓库 (个人: {} 个，组织/协作: {} 个)。",
        "section_owned": "【个人所属仓库】",
        "section_other": "\n【组织与协作仓库】(共 {} 个)",
        "vis_private": "🔒私有",
        "vis_public": "🌐公开",
        "ask_include_orgs": "\n👉 是否一并同步上述 {} 个组织/协作仓库？(y/N): ",
        "include_orgs_yes": "-> 合并同步全部 {} 个仓库。",
        "include_orgs_no": "-> 仅同步个人所属的 {} 个仓库。",
        "confirm": "\n⚠️  最终确认: 即将使用 {} 个并发线程创建 {} 个拉取镜像。(y/N): ",
        "cancelled": "已取消。",
        "starting": "\n🚀 开始并发迁移 ({} 个线程, 每请求 {}s 超时)...",
        "mirroring": "[{}/{}] {} ... ",
        "success": "✅ 已创建 ({:.1f}s)",
        "already_exists": "⏭️  已存在",
        "blocked": "🚫 已封锁 (GitHub 拒绝访问)",
        "retry": "⏳ 第 {}/{} 次重试, 等待 {}s ({})",
        "failed": "❌ 失败: {}",
        "done": "\n🎉 迁移完成!",
        "report_header": "执行报告",
        "report_total": "仓库总数",
        "report_success": "成功创建",
        "report_skipped": "已存在(跳过)",
        "report_blocked": "被 GitHub 封锁",
        "report_failed": "失败",
        "report_duration": "总耗时",
        "report_avg": "平均每仓库",
        "report_workers": "并发线程数",
        "report_timeout": "请求超时",
        "report_failed_list": "失败仓库列表",
        "report_blocked_list": "被封锁仓库列表",
        "report_saved": "📄 报告: {}",
        "dry_run_prefix": "[演习] ",
        "env_missing": "❌ 必需的环境变量 '{}' 未设置。请配置 .env 文件。",
    },
}

T = MESSAGES["en"]

# Thread-safe print lock
_print_lock = threading.Lock()
_shutdown_event = threading.Event()

Repo = Dict[str, Any]
Result = Dict[str, Any]


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _colorize(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _format_repo_list(title: str, names: List[str], color_code: str) -> List[str]:
    lines = [title]
    for i, name in enumerate(names, 1):
        label = _colorize(f"{i:03d}. {name}", color_code)
        lines.append(f"  {label}")
    return lines


def _handle_github_rate_limit(resp: urllib.response.addinfourl, logger: logging.Logger) -> None:
    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset_ts = resp.headers.get("X-RateLimit-Reset")
    if remaining is not None and remaining != "0":
        return

    wait_seconds = 60
    if reset_ts:
        try:
            reset_at = int(reset_ts)
            now = int(time.time())
            wait_seconds = max(0, reset_at - now)
            reset_str = datetime.fromtimestamp(reset_at).strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(T["rate_limit_hit"].format(reset_str, wait_seconds))
        except ValueError:
            logger.warning(T["rate_limit_skip"].format(wait_seconds))
    else:
        logger.warning(T["rate_limit_skip"].format(wait_seconds))

    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _split_text(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks


def _build_summary_text(
    all_results: List[Result],
    total_duration: float,
    max_workers: int,
    request_timeout: int,
    lang: str,
    report_file: Path,
) -> str:
    counts = _count_results(all_results)
    failed_list = []
    blocked_list = []
    for r in all_results:
        if r["status"] == "failed":
            failed_list.append((r["name"], r["error"]))
        elif r["status"] == "blocked":
            blocked_list.append((r["name"], r["error"]))

    total_count = len(all_results)
    avg_time = total_duration / max(total_count, 1)
    mins, secs = divmod(int(total_duration), 60)
    hours, mins = divmod(mins, 60)
    dur_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    t = MESSAGES[lang]
    lines = [
        f"{t['report_header']}",
        f"{t['report_total']}: {total_count}",
        f"{t['report_success']}: {counts['success']}",
        f"{t['report_skipped']}: {counts['skipped']}",
        f"{t['report_blocked']}: {counts['blocked']}",
        f"{t['report_failed']}: {counts['failed']}",
        f"{t['report_workers']}: {max_workers}",
        f"{t['report_timeout']}: {request_timeout}s",
        f"{t['report_duration']}: {dur_str}",
        f"{t['report_avg']}: {avg_time:.1f}s",
        f"Report: {report_file.name}",
    ]

    if blocked_list:
        lines.append(f"{t['report_blocked_list']}:")
        for name, err in blocked_list:
            lines.append(f"- {name}: {err}")

    if failed_list:
        lines.append(f"{t['report_failed_list']}:")
        for name, err in failed_list:
            lines.append(f"- {name}: {err}")

    return "\n".join(lines)


def _count_results(all_results: List[Result]) -> Dict[str, int]:
    counts = {"success": 0, "skipped": 0, "blocked": 0, "failed": 0}
    for r in all_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _detect_webhook_type(webhook_url: str, override: str) -> str:
    if override:
        return override.lower()

    lower_url = webhook_url.lower()
    if "api.telegram.org" in lower_url:
        return "telegram"
    if "open.feishu.cn" in lower_url or "open.larksuite.com" in lower_url:
        return "feishu"
    if "oapi.dingtalk.com" in lower_url:
        return "dingtalk"
    if "discord.com/api/webhooks" in lower_url:
        return "discord"
    if "hooks.slack.com" in lower_url:
        return "slack"
    if "office.com/webhook" in lower_url or "outlook.office.com/webhook" in lower_url:
        return "teams"
    return "generic"


def _send_webhook_message(
    webhook_url: str,
    webhook_type: str,
    message: str,
    notify_chat_id: str,
    logger: logging.Logger,
    lang: str,
) -> None:
    payload: Dict[str, Any]
    if webhook_type == "telegram":
        if not notify_chat_id:
            raise ValueError("NOTIFY_CHAT_ID is required for Telegram")
        payload = {"chat_id": notify_chat_id, "text": message}
    elif webhook_type in {"slack", "discord", "teams", "generic"}:
        payload = {"text": message}
    elif webhook_type == "feishu":
        payload = {"msg_type": "text", "content": {"text": message}}
    elif webhook_type == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": message}}
    else:
        payload = {"text": message}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    logger.info(MESSAGES[lang]["notify_sent"])


def _send_webhook_notification(
    webhook_url: str,
    webhook_type: str,
    messages: List[str],
    notify_chat_id: str,
    logger: logging.Logger,
    lang: str,
) -> None:
    for message in messages:
        _send_webhook_message(
            webhook_url,
            webhook_type,
            message,
            notify_chat_id,
            logger,
            lang,
        )


def load_env_file(filepath: Path) -> None:
    """Load .env file into os.environ (does not override existing vars)."""
    if not filepath.is_file():
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value


def setup_logging(level_name: str) -> logging.Logger:
    """Configure dual logging: console + file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"mirror_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("gitea-mirror")
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

    _rotate_files(LOGS_DIR, "mirror_*.log", max_keep=30)
    return logger


def _rotate_files(directory: Path, pattern: str, max_keep: int) -> None:
    """Delete oldest files if count exceeds max_keep."""
    files = sorted(glob.glob(str(directory / pattern)))
    while len(files) > max_keep:
        try:
            os.remove(files.pop(0))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
def fetch_github_repos(token: str, logger: logging.Logger) -> List[Repo]:
    """Fetch all repos the authenticated user has access to (paginated)."""
    repos = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/user/repos"
            f"?affiliation=owner,collaborator,organization_member"
            f"&per_page=100&page={page}"
        )
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                _handle_github_rate_limit(resp, logger)
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                remaining = e.headers.get("X-RateLimit-Remaining")
                reset_ts = e.headers.get("X-RateLimit-Reset")
                if remaining == "0" or e.code == 429:
                    wait_seconds = 60
                    if reset_ts:
                        try:
                            reset_at = int(reset_ts)
                            now = int(time.time())
                            wait_seconds = max(0, reset_at - now)
                            reset_str = datetime.fromtimestamp(reset_at).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            logger.warning(T["rate_limit_hit"].format(reset_str, wait_seconds))
                        except ValueError:
                            logger.warning(T["rate_limit_skip"].format(wait_seconds))
                    else:
                        logger.warning(T["rate_limit_skip"].format(wait_seconds))

                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    continue

            logger.error(T["fetch_fail"].format(e))
            sys.exit(1)
        except Exception as e:
            logger.error(T["fetch_fail"].format(e))
            sys.exit(1)

        if not data:
            break
        repos.extend(data)
        logger.debug(f"GitHub API page {page}: {len(data)} repos (total: {len(repos)})")
        page += 1

    return repos


# ---------------------------------------------------------------------------
# Gitea API
# ---------------------------------------------------------------------------
def fetch_gitea_repos(
    gitea_url: str,
    gitea_token: str,
    logger: logging.Logger,
) -> Dict[str, Dict[str, Any]]:
    """Fetch all repos the Gitea user has access to (personal + orgs) with health metadata.

    Returns dict: {"owner/repo_name": {"mirror": bool, "empty": bool, "original_url": str, "owner": str, "name": str}}
    """
    repos: Dict[str, Dict[str, Any]] = {}

    def _fetch_paginated(api_path: str, context_name: str) -> None:
        page = 1
        total_fetched = 0
        while True:
            url = f"{gitea_url}/api/v1/{api_path}?limit=50&page={page}"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"token {gitea_token}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

            attempt = 1
            max_retries = 3
            data = None
            while attempt <= max_retries:
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        break
                except Exception as e:
                    logger.warning(
                        f"⚠️ Failed to fetch {api_path} (page {page}, attempt {attempt}/{max_retries}): {e}"
                    )
                    if attempt == max_retries:
                        raise RuntimeError(
                            f"Fatal error: Could not fetch {api_path} from Gitea after "
                            f"{max_retries} attempts. Aborting to prevent inconsistent state."
                        )
                    time.sleep(2**attempt)
                    attempt += 1

            if not data:
                break

            for repo in data:
                owner = repo.get("owner", {}).get("login", "")
                name = repo.get("name")
                if owner and name:
                    key = f"{owner.lower()}/{name.lower()}"
                    repos[key] = {
                        "mirror": repo.get("mirror", False),
                        "empty": repo.get("empty", False),
                        "original_url": repo.get("original_url", ""),
                        "owner": owner,
                        "name": name,
                    }
                    total_fetched += 1

            page += 1
        logger.debug(f"Fetched {total_fetched} repos from {context_name}")

    # 1. Fetch personal repos
    _fetch_paginated("user/repos", "personal repos")

    # 2. Fetch user orgs, then repos for each org
    page = 1
    orgs = []
    while True:
        url = f"{gitea_url}/api/v1/user/orgs?limit=50&page={page}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {gitea_token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

        attempt = 1
        max_retries = 3
        data = None
        while attempt <= max_retries:
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to fetch orgs (page {page}, attempt {attempt}/{max_retries}): {e}"
                )
                if attempt == max_retries:
                    raise RuntimeError(
                        f"Fatal error: Could not fetch orgs from Gitea after {max_retries} attempts."
                    )
                time.sleep(2**attempt)
                attempt += 1

        if not data:
            break

        orgs.extend([o.get("username") for o in data if o.get("username")])
        page += 1

    for org in orgs:
        _fetch_paginated(f"orgs/{org}/repos", f"org {org}")

    return repos


def delete_gitea_repo(
    gitea_url: str,
    gitea_token: str,
    gitea_user: str,
    repo_name: str,
    logger: logging.Logger,
    max_retries: int = 3,
) -> bool:
    """Delete a repo from Gitea. Returns True if deleted, False on error."""
    url = f"{gitea_url}/api/v1/repos/{gitea_user}/{repo_name}"

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", f"token {gitea_token}")
        req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True  # Already deleted
            if attempt < max_retries:
                time.sleep(2**attempt)
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2**attempt)
            else:
                logger.debug(f"Failed to delete {repo_name} after {max_retries} attempts: {e}")

    return False


def cleanup_failed_migration(
    gitea_url: str,
    gitea_token: str,
    repo_owner: str,
    repo_name: str,
    logger: logging.Logger,
) -> bool:
    """Layer 2: After migration failure, check if Gitea created a broken shell and delete it."""

    # Wait briefly to let Gitea's internal clone process fail and release locks
    time.sleep(2)

    check_url = f"{gitea_url}/api/v1/repos/{repo_owner}/{repo_name}"
    req = urllib.request.Request(check_url)
    req.add_header("Authorization", f"token {gitea_token}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("empty", False):
            deleted = delete_gitea_repo(gitea_url, gitea_token, repo_owner, repo_name, logger)
            if deleted:
                logger.debug(f"Cleaned up broken shell: {repo_owner}/{repo_name}")
            return deleted
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False  # Not created, nothing to clean
    except Exception:
        pass
    return False


def ensure_gitea_org(
    gitea_url: str,
    gitea_token: str,
    org_name: str,
    logger: logging.Logger,
) -> bool:
    """Ensure a Gitea organization exists."""
    check_url = f"{gitea_url}/api/v1/orgs/{org_name}"
    req = urllib.request.Request(check_url)
    req.add_header("Authorization", f"token {gitea_token}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

    try:
        with urllib.request.urlopen(req, timeout=15):
            return True  # Org already exists
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning(f"Failed to check org {org_name}: {e}")
            return False
    except Exception as e:
        logger.warning(f"Failed to check org {org_name}: {e}")
        return False

    # Create the org
    create_url = f"{gitea_url}/api/v1/orgs"
    payload = {
        "username": org_name,
        "visibility": "public",
    }
    req = urllib.request.Request(
        create_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"token {gitea_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

    try:
        with urllib.request.urlopen(req, timeout=15):
            logger.info(f"Created missing Gitea organization: {org_name}")
            return True
    except urllib.error.HTTPError as e:
        # 422 usually means already exists or invalid name
        if e.code == 422:
            return True
        logger.warning(f"Failed to create org {org_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to create org {org_name}: {e}")

    return False


def _print_repo_status(
    logger: logging.Logger,
    github_repos: List[Repo],
    gitea_repos: Dict[str, Dict[str, Any]],
    broken_names: Set[str],
    lang: str,
    gitea_user: str,
    preserve_orgs: bool,
) -> None:
    """Print a colored status comparison: GitHub vs Gitea."""
    gitea_names = set(gitea_repos.keys())
    if not gitea_names:
        return

    github_names = set()
    for r in github_repos:
        repo_owner = gitea_user
        if preserve_orgs and r.get("owner") and r["owner"].get("type") == "Organization":
            repo_owner = r["owner"]["login"]
        github_names.add(f"{repo_owner.lower()}/{r['name'].lower()}")
    github_only = sorted(github_names - gitea_names)
    both_healthy = sorted((github_names & gitea_names) - broken_names)
    both_broken = sorted(broken_names & github_names)
    gitea_only = sorted(gitea_names - github_names)

    t = MESSAGES[lang]
    legend = t["status_legend"].format(
        _colorize("●", "32"),
        _colorize("●", "33"),
        _colorize("●", "34"),
    )

    logger.info(t["status_header"])
    logger.info(legend)

    if github_only:
        header = f"【{t['status_github_only']}】({len(github_only)})"
        for line in _format_repo_list(header, github_only, "32"):
            logger.info(line)

    if both_broken:
        header = f"【{t['status_broken']}】({len(both_broken)})"
        for line in _format_repo_list(header, both_broken, "31"):
            logger.info(line)

    if both_healthy:
        header = f"【{t['status_both']}】({len(both_healthy)})"
        for line in _format_repo_list(header, both_healthy, "33"):
            logger.info(line)

    if gitea_only:
        header = f"【{t['status_gitea_only']}】({len(gitea_only)})"
        for line in _format_repo_list(header, gitea_only, "34"):
            logger.info(line)


# ---------------------------------------------------------------------------
# Gitea Migration — Strict Synchronous with Retries
# ---------------------------------------------------------------------------
def migrate_single_repo(
    repo: Repo,
    index: int,
    total: int,
    gitea_url: str,
    gitea_token: str,
    gitea_user: str,
    github_token: str,
    mirror_interval: str,
    mirror_lfs: bool,
    mirror_extras: bool,
    preserve_orgs: bool,
    request_timeout: int,
    max_retries: int,
    retry_delay: int,
    dry_run: bool,
    logger: logging.Logger,
) -> Result:
    """
    Migrate a single GitHub repo to Gitea. Blocks until Gitea returns 201 or fails.

    Returns a result dict: {name, status, duration, error}
    - status: 'success' | 'skipped' | 'blocked' | 'failed'
    """
    repo_name = repo["name"]
    clone_url = repo["clone_url"]
    is_private = repo["private"]

    # Determine repo_owner based on PRESERVE_ORGS
    repo_owner = gitea_user
    if preserve_orgs and repo.get("owner") and repo["owner"].get("type") == "Organization":
        repo_owner = repo["owner"]["login"]

    prefix = f"[{index}/{total}] {repo_name}"
    start = time.time()

    if dry_run:
        with _print_lock:
            logger.info(
                f"{prefix} ... {T['dry_run_prefix']}{clone_url} -> {repo_owner}/{repo_name}"
            )
        return {"name": repo_name, "status": "success", "duration": 0, "error": ""}

    payload = {
        "auth_token": github_token,
        "clone_addr": clone_url,
        "mirror": True,
        "repo_name": repo_name,
        "repo_owner": repo_owner,
        "private": is_private,
        "service": "github",
        "wiki": mirror_extras,
        "labels": mirror_extras,
        "issues": mirror_extras,
        "pull_requests": mirror_extras,
        "releases": mirror_extras,
        "milestones": mirror_extras,
    }
    if mirror_interval:
        payload["mirror_interval"] = mirror_interval
    if mirror_lfs:
        payload["lfs"] = True

    last_error = ""

    for attempt in range(1, max_retries + 1):
        if _shutdown_event.is_set():
            with _print_lock:
                logger.warning(f"{prefix} ... ⚠️ Interrupted")
            return {
                "name": repo_name,
                "status": "failed",
                "duration": time.time() - start,
                "error": "Interrupted by user",
            }

        req = urllib.request.Request(
            f"{gitea_url}/api/v1/repos/migrate",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Authorization", f"token {gitea_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")

        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                resp.read()
                elapsed = time.time() - start
                with _print_lock:
                    logger.info(f"{prefix} ... {T['success'].format(elapsed)}")
                return {"name": repo_name, "status": "success", "duration": elapsed, "error": ""}

        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            # --- 409: Already exists → skip (no retry) ---
            if e.code == 409 or "already exists" in err_body.lower():
                with _print_lock:
                    logger.info(f"{prefix} ... {T['already_exists']}")
                return {
                    "name": repo_name,
                    "status": "skipped",
                    "duration": time.time() - start,
                    "error": "",
                }

            # --- Access blocked: body contains 451 or "access blocked" (Gitea may wrap as 500) ---
            is_access_blocked = "access blocked" in err_body.lower() or "451" in err_body
            if is_access_blocked:
                reason = _extract_error_message(err_body)
                with _print_lock:
                    logger.warning(f"{prefix} ... {T['blocked']} — {reason}")

                # Layer 2: Clean up empty shell for blocked repos too
                cleanup_failed_migration(gitea_url, gitea_token, repo_owner, repo_name, logger)

                return {
                    "name": repo_name,
                    "status": "blocked",
                    "duration": time.time() - start,
                    "error": reason,
                }

            # --- 403 without access blocked → auth/permission issue, retry ---
            if e.code == 403:
                last_error = f"HTTP 403: {_extract_error_message(err_body)}"
                if attempt < max_retries:
                    wait = retry_delay * (2 ** (attempt - 1))
                    with _print_lock:
                        logger.warning(
                            f"{prefix} ... {T['retry'].format(attempt, max_retries, wait, last_error)}"
                        )
                    time.sleep(wait)
                    continue
                else:
                    with _print_lock:
                        logger.error(f"{prefix} ... {T['failed'].format(last_error)}")
                    break

            # --- 422: DNS failure (retryable) vs true validation error (hard fail) ---
            if e.code == 422:
                reason = _extract_error_message(err_body)
                if "could not resolve host" in err_body.lower() or "dns" in err_body.lower():
                    last_error = f"HTTP 422 (DNS): {reason}"
                    if attempt < max_retries:
                        wait = retry_delay * (2 ** (attempt - 1))
                        with _print_lock:
                            logger.warning(
                                f"{prefix} ... {T['retry'].format(attempt, max_retries, wait, last_error)}"
                            )
                        time.sleep(wait)
                        continue
                    else:
                        with _print_lock:
                            logger.error(f"{prefix} ... {T['failed'].format(last_error)}")
                        break
                else:
                    last_error = f"HTTP 422: {reason}"
                    with _print_lock:
                        logger.error(f"{prefix} ... {T['failed'].format(last_error)}")
                    cleanup_failed_migration(gitea_url, gitea_token, repo_owner, repo_name, logger)
                    return {
                        "name": repo_name,
                        "status": "failed",
                        "duration": time.time() - start,
                        "error": last_error,
                    }

            # --- 5xx / other: Retry with exponential backoff ---
            last_error = f"HTTP {e.code}: {_extract_error_message(err_body)}"
            if attempt < max_retries:
                wait = retry_delay * (2 ** (attempt - 1))
                with _print_lock:
                    logger.warning(
                        f"{prefix} ... {T['retry'].format(attempt, max_retries, wait, last_error)}"
                    )
                time.sleep(wait)
            else:
                with _print_lock:
                    logger.error(f"{prefix} ... {T['failed'].format(last_error)}")

        except (socket.timeout, urllib.error.URLError) as e:
            last_error = f"Network: {str(e)[:150]}"
            if attempt < max_retries:
                wait = retry_delay * (2 ** (attempt - 1))
                with _print_lock:
                    logger.warning(
                        f"{prefix} ... {T['retry'].format(attempt, max_retries, wait, last_error)}"
                    )
                time.sleep(wait)
            else:
                with _print_lock:
                    logger.error(f"{prefix} ... {T['failed'].format(last_error)}")

        except Exception as e:
            last_error = f"Unexpected: {str(e)[:150]}"
            if attempt < max_retries:
                wait = retry_delay * (2 ** (attempt - 1))
                with _print_lock:
                    logger.warning(
                        f"{prefix} ... {T['retry'].format(attempt, max_retries, wait, last_error)}"
                    )
                time.sleep(wait)
            else:
                with _print_lock:
                    logger.error(f"{prefix} ... {T['failed'].format(last_error)}")

    # Layer 2: Clean up broken shell left by Gitea's create-before-clone behavior
    cleaned = cleanup_failed_migration(gitea_url, gitea_token, repo_owner, repo_name, logger)
    if cleaned:
        with _print_lock:
            logger.info(f"{prefix} ... {T['cleanup_shell'].format(repo_name)}")

    return {
        "name": repo_name,
        "status": "failed",
        "duration": time.time() - start,
        "error": last_error,
    }


def _extract_error_message(body: str) -> str:
    """Try to extract a clean error message from Gitea/GitHub JSON or HTML response."""
    body = body.strip()
    # Try JSON
    try:
        data = json.loads(body)
        return data.get("message", body[:200])
    except (json.JSONDecodeError, AttributeError):
        pass
    # Strip HTML tags for Nginx error pages
    if "<html" in body.lower():
        import re

        text = re.sub(r"<[^>]+>", " ", body)
        text = " ".join(text.split())
        return text[:200]
    return body[:200]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def generate_report(
    all_results: List[Result],
    total_duration: float,
    max_workers: int,
    request_timeout: int,
    lang: str,
    logger: logging.Logger,
) -> Path:
    """Generate a Markdown execution report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = REPORTS_DIR / f"report_{ts}.md"

    counts = {"success": 0, "skipped": 0, "blocked": 0, "failed": 0}
    failed_list = []
    blocked_list = []
    for r in all_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "failed":
            failed_list.append((r["name"], r["error"]))
        elif r["status"] == "blocked":
            blocked_list.append((r["name"], r["error"]))

    total_count = len(all_results)
    avg_time = total_duration / max(total_count, 1)
    mins, secs = divmod(int(total_duration), 60)
    hours, mins = divmod(mins, 60)
    dur_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    t = MESSAGES[lang]

    lines = [
        f"# 📊 {t['report_header']}",
        "",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Version:** v{VERSION}  ",
        "**Mode:** Concurrent (strict synchronous per worker)  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| {t['report_total']} | {total_count} |",
        f"| {t['report_success']} | {counts['success']} |",
        f"| {t['report_skipped']} | {counts['skipped']} |",
        f"| {t['report_blocked']} | {counts['blocked']} |",
        f"| {t['report_failed']} | {counts['failed']} |",
        f"| {t['report_workers']} | {max_workers} |",
        f"| {t['report_timeout']} | {request_timeout}s |",
        f"| {t['report_duration']} | {dur_str} |",
        f"| {t['report_avg']} | {avg_time:.1f}s |",
        "",
    ]

    if blocked_list:
        lines += [f"## 🚫 {t['report_blocked_list']}", ""]
        for name, err in blocked_list:
            lines.append(f"- `{name}`: {err}")
        lines.append("")

    if failed_list:
        lines += [f"## ❌ {t['report_failed_list']}", ""]
        for name, err in failed_list:
            lines.append(f"- `{name}`: {err}")
        lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")

    # Console summary
    logger.info("")
    logger.info("=" * 55)
    logger.info(f"📊 {t['report_header']}")
    logger.info(f"   {t['report_total']}:       {total_count}")
    logger.info(f"   {t['report_success']}:   {counts['success']}")
    logger.info(f"   {t['report_skipped']}:   {counts['skipped']}")
    logger.info(f"   {t['report_blocked']}:  {counts['blocked']}")
    logger.info(f"   {t['report_failed']}:         {counts['failed']}")
    logger.info(f"   {t['report_workers']}:   {max_workers}")
    logger.info(f"   {t['report_timeout']}:    {request_timeout}s")
    logger.info(f"   {t['report_duration']}:      {dur_str}")
    logger.info(f"   {t['report_avg']}:    {avg_time:.1f}s")
    logger.info("=" * 55)

    max_reports = int(os.environ.get("REPORT_MAX_COUNT", "50"))
    _rotate_files(REPORTS_DIR, "report_*.md", max_keep=max_reports)
    logger.info(t["report_saved"].format(report_file))
    return report_file


def trigger_mirror_sync(
    gitea_url: str,
    gitea_token: str,
    repo_owner: str,
    repo_name: str,
    logger: logging.Logger,
) -> None:
    """Trigger an immediate mirror sync for an existing repository."""
    sync_url = f"{gitea_url}/api/v1/repos/{repo_owner}/{repo_name}/mirror-sync"
    req = urllib.request.Request(sync_url, method="POST")
    req.add_header("Authorization", f"token {gitea_token}")
    req.add_header("User-Agent", f"gitea-github-mirror/{VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=15):
            with _print_lock:
                logger.info(
                    f"🔄 Triggered immediate sync for existing mirror: {repo_owner}/{repo_name}"
                )
    except Exception as e:
        with _print_lock:
            logger.warning(f"⚠️ Failed to trigger sync for {repo_owner}/{repo_name}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global T

    load_env_file(ENV_FILE)

    parser = argparse.ArgumentParser(
        description="Bulk mirror all GitHub repos to Gitea (concurrent, strict).",
    )
    parser.add_argument("--lang", choices=["en", "cn"], default=None)
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmations")
    parser.add_argument("--include-orgs", action="store_true", help="Include org/collab repos")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent workers (default: 5)")
    parser.add_argument(
        "--timeout", type=int, default=None, help="Request timeout seconds (default: 600)"
    )
    args = parser.parse_args()

    lang = args.lang or os.environ.get("LANG_MIRROR", "en")
    if lang not in ("en", "cn"):
        lang = "en"
    T = MESSAGES[lang]

    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logger = setup_logging(log_level)
    logger.info(T["banner"])

    # Required env vars
    gitea_url = os.environ.get("GITEA_URL", "").rstrip("/")
    gitea_token = os.environ.get("GITEA_TOKEN", "")
    gitea_user = os.environ.get("GITEA_USER", "")
    github_user = os.environ.get("GITHUB_USER", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    for var, val in [
        ("GITEA_URL", gitea_url),
        ("GITEA_TOKEN", gitea_token),
        ("GITEA_USER", gitea_user),
        ("GITHUB_USER", github_user),
    ]:
        if not val:
            logger.error(T["env_missing"].format(var))
            sys.exit(1)

    if not github_token:
        import getpass

        github_token = getpass.getpass(T["prompt_github_token"])

    # Tuning
    mirror_interval = os.environ.get("MIRROR_INTERVAL", "")
    mirror_lfs = os.environ.get("MIRROR_LFS", "").strip().lower() in {"1", "true", "yes", "on"}
    notify_webhook = os.environ.get("NOTIFY_WEBHOOK", "").strip()
    notify_type = os.environ.get("NOTIFY_TYPE", "").strip().lower()
    notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "").strip()
    notify_only_on_failure = os.environ.get("NOTIFY_ONLY_ON_FAILURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    notify_include_report = os.environ.get("NOTIFY_INCLUDE_REPORT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    request_timeout = args.timeout or int(os.environ.get("REQUEST_TIMEOUT", "600"))
    max_retries = int(os.environ.get("MAX_RETRIES", "3"))
    retry_delay = int(os.environ.get("RETRY_DELAY", "10"))
    max_workers = args.workers or int(os.environ.get("MAX_WORKERS", "5"))
    skip_repos_str = os.environ.get("SKIP_REPOS", "").strip()
    skip_repos = {r.strip() for r in skip_repos_str.split(",")} if skip_repos_str else set()
    mirror_extras = os.environ.get("MIRROR_EXTRAS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    preserve_orgs = os.environ.get("PRESERVE_ORGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    sync_now = os.environ.get("SYNC_NOW", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    force_recreate = os.environ.get("FORCE_RECREATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Phase 1: Fetch
    logger.info(T["fetching"])
    all_repos_raw = fetch_github_repos(github_token, logger)
    all_repos = [r for r in all_repos_raw if r["name"] not in skip_repos]

    if skip_repos:
        skipped_count = len(all_repos_raw) - len(all_repos)
        if skipped_count > 0:
            logger.info(f"Skipped {skipped_count} explicitly ignored repos (SKIP_REPOS).")

    owner_repos = [r for r in all_repos if r["owner"]["login"].lower() == github_user.lower()]
    other_repos = [r for r in all_repos if r["owner"]["login"].lower() != github_user.lower()]

    logger.info(T["scan_result"].format(len(all_repos), len(owner_repos), len(other_repos)))

    # Phase 2: List
    logger.info("-" * 55)
    logger.info(T["section_owned"])
    for i, repo in enumerate(owner_repos, 1):
        vis = T["vis_private"] if repo["private"] else T["vis_public"]
        logger.info(f"  {i:03d}. [{vis}] {repo['name']}")

    if other_repos:
        logger.info(T["section_other"].format(len(other_repos)))
        for i, repo in enumerate(other_repos, 1):
            vis = T["vis_private"] if repo["private"] else T["vis_public"]
            logger.info(f"  {i:03d}. [{vis}] [{repo['owner']['login']}] {repo['name']}")
    logger.info("-" * 55)

    # Phase 3: Filter
    final_repos = list(owner_repos)
    if other_repos and not args.include_orgs:
        if args.yes:
            logger.info(T["include_orgs_no"].format(len(owner_repos)))
        else:
            choice = input(T["ask_include_orgs"].format(len(other_repos)))
            if choice.strip().lower() == "y":
                final_repos = list(all_repos)
                logger.info(T["include_orgs_yes"].format(len(final_repos)))
            else:
                logger.info(T["include_orgs_no"].format(len(final_repos)))
    elif args.include_orgs:
        final_repos = list(all_repos)
        logger.info(T["include_orgs_yes"].format(len(final_repos)))

    # Phase 3.5: Incremental sync with mirror health check
    gitea_repos: Dict[str, Dict[str, Any]] = {}
    try:
        logger.info(T["fetching_gitea"])
        gitea_repos = fetch_gitea_repos(gitea_url, gitea_token, logger)
    except Exception as e:
        logger.warning(T["fetch_gitea_fail"].format(str(e)))

    skipped = 0
    broken_keys: Set[str] = set()
    selected_repos = list(final_repos)

    # Helper to get expected Gitea key for a GitHub repo
    def _get_expected_key(repo: Repo) -> str:
        owner = gitea_user
        if preserve_orgs and repo.get("owner") and repo["owner"].get("type") == "Organization":
            owner = repo["owner"]["login"]
        return f"{owner.lower()}/{repo['name'].lower()}"

    if gitea_repos:
        # Classify existing Gitea repos by health
        healthy_keys: Set[str] = set()
        for key, info in gitea_repos.items():
            if info.get("mirror") and info.get("empty"):
                broken_keys.add(key)
            else:
                healthy_keys.add(key)

        # Handle FORCE_RECREATE
        if force_recreate:
            for repo in final_repos:
                key = _get_expected_key(repo)
                if key in healthy_keys:
                    healthy_keys.remove(key)
                    broken_keys.add(key)

        # Layer 1: Auto-repair broken mirrors (delete empty shells or force recreate)
        if broken_keys:
            logger.info(T["broken_found"].format(len(broken_keys)))
            logger.info(T["broken_repairing"])
            repaired = 0
            for key in sorted(broken_keys):
                repo_info: Optional[Dict[str, Any]] = gitea_repos.get(key)
                owner = repo_info["owner"] if repo_info else key.split("/")[0]
                name = repo_info["name"] if repo_info else key.split("/")[1]
                if delete_gitea_repo(gitea_url, gitea_token, owner, name, logger):
                    logger.info(T["broken_deleted"].format(f"{owner}/{name}"))
                    repaired += 1
                else:
                    logger.warning(T["broken_delete_fail"].format(f"{owner}/{name}"))
            if repaired:
                logger.info(T["broken_repaired"].format(repaired))

        # Filter: skip healthy repos, keep broken ones for re-migration
        new_count = 0
        new_final_repos = []
        sync_tasks = []
        for repo in final_repos:
            key = _get_expected_key(repo)
            if key in healthy_keys:
                if sync_now and not args.dry_run:
                    owner = gitea_repos[key]["owner"]
                    name = gitea_repos[key]["name"]
                    sync_tasks.append((owner, name))
            else:
                new_final_repos.append(repo)
                if key not in broken_keys:
                    new_count += 1

        before_count = len(final_repos)
        final_repos = new_final_repos
        skipped = before_count - len(final_repos)
        logger.info(
            T["incremental_summary"].format(
                len(gitea_repos), len(healthy_keys), len(broken_keys), new_count
            )
        )
        _print_repo_status(
            logger, selected_repos, gitea_repos, broken_keys, lang, gitea_user, preserve_orgs
        )

    if args.dry_run:
        logger.info(T["dry_run_diff"].format(len(final_repos), skipped))

    # Phase 4: Confirm
    if not args.yes:
        confirm = input(T["confirm"].format(max_workers, len(final_repos)))
        if confirm.strip().lower() != "y":
            logger.info(T["cancelled"])
            sys.exit(0)

    # Phase 4.5: Ensure Organizations exist if preserving org structure
    if preserve_orgs and not args.dry_run:
        orgs_to_create = {
            r["owner"]["login"]
            for r in final_repos
            if r.get("owner") and r["owner"].get("type") == "Organization"
        }
        if orgs_to_create:
            logger.info("Ensuring Gitea organizations exist...")
            for org in sorted(orgs_to_create):
                ensure_gitea_org(gitea_url, gitea_token, org, logger)

    # Phase 4.8: Concurrent SYNC_NOW triggers
    if sync_tasks and not args.dry_run:
        logger.info(
            f"🚀 Triggering mirror sync for {len(sync_tasks)} existing repositories concurrently..."
        )
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sync") as pool:
            sync_futures = [
                pool.submit(
                    trigger_mirror_sync,
                    gitea_url,
                    gitea_token,
                    owner,
                    name,
                    logger,
                )
                for owner, name in sync_tasks
            ]
            for future in as_completed(sync_futures):
                pass  # Exceptions are caught inside trigger_mirror_sync
        logger.info("✅ All mirror sync triggers completed.")

    # Phase 5: Concurrent migration
    logger.info(T["starting"].format(max_workers, request_timeout))
    start_time = time.time()
    all_results = []

    # Register graceful shutdown handler (Ctrl+C)
    _original_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(sig, frame):
        _shutdown_event.set()
        with _print_lock:
            logger.warning("\n⚠️  Shutdown signal received, finishing current tasks...")

    signal.signal(signal.SIGINT, _handle_sigint)

    # Assign indices before submitting to thread pool
    indexed_repos = list(enumerate(final_repos, 1))

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mirror") as pool:
        futures = {}
        for idx, repo in indexed_repos:
            if _shutdown_event.is_set():
                break
            future = pool.submit(
                migrate_single_repo,
                repo=repo,
                index=idx,
                total=len(final_repos),
                gitea_url=gitea_url,
                gitea_token=gitea_token,
                gitea_user=gitea_user,
                github_token=github_token,
                mirror_interval=mirror_interval,
                mirror_lfs=mirror_lfs,
                mirror_extras=mirror_extras,
                preserve_orgs=preserve_orgs,
                request_timeout=request_timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
                dry_run=args.dry_run,
                logger=logger,
            )
            futures[future] = repo["name"]

        for future in as_completed(futures):
            try:
                result = future.result()
                all_results.append(result)
            except Exception as e:
                name = futures[future]
                logger.error(f"❌ Unexpected thread error for {name}: {e}")
                all_results.append(
                    {"name": name, "status": "failed", "duration": 0, "error": str(e)}
                )

    # Restore original signal handler
    signal.signal(signal.SIGINT, _original_sigint)

    total_duration = time.time() - start_time

    # Phase 6: Report
    logger.info(T["done"])
    report_file = generate_report(
        all_results, total_duration, max_workers, request_timeout, lang, logger
    )

    if notify_webhook:
        counts = _count_results(all_results)
        if notify_only_on_failure and counts.get("failed", 0) == 0:
            pass
        else:
            summary = _build_summary_text(
                all_results,
                total_duration,
                max_workers,
                request_timeout,
                lang,
                report_file,
            )
            if notify_include_report:
                try:
                    report_text = report_file.read_text(encoding="utf-8")
                    summary = summary + "\n\n---\n\n" + report_text
                except OSError:
                    pass
            messages = _split_text(summary, 3000)
            webhook_type = _detect_webhook_type(notify_webhook, notify_type)
            try:
                _send_webhook_notification(
                    notify_webhook,
                    webhook_type,
                    messages,
                    notify_chat_id,
                    logger,
                    lang,
                )
            except Exception as e:
                logger.warning(T["notify_failed"].format(str(e)))


if __name__ == "__main__":
    main()
