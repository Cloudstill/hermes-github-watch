#!/usr/bin/env python3
"""GitHub change watcher for Hermes no-agent cron jobs.

The script is intentionally stdlib-only. It reads github-watch-config.json
from the same directory, stores state beside it, and prints nothing when
there is nothing new. In Hermes no-agent cron mode, empty stdout means the
tick is silent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - old Python fallback
    ZoneInfo = None  # type: ignore


API_ROOT = "https://api.github.com"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "github-watch-config.json"
STATE_PATH = SCRIPT_DIR / "github-watch-state.json"
DEFAULT_TOKEN_ENV = "GITHUB_WATCH_TOKEN"
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Event types watched for a user by default / in interactive queries.
DEFAULT_USER_EVENTS = [
    "PushEvent",
    "ReleaseEvent",
    "CreateEvent",
    "PullRequestEvent",
    "IssuesEvent",
]
QUERY_USER_EVENTS = DEFAULT_USER_EVENTS + ["WatchEvent", "ForkEvent"]

# Valid watch kinds for a repo target.
VALID_REPO_WATCHES = {"releases", "commits"}

# HTTP retry backoff (seconds) for transient 5xx / network errors.
RETRY_BACKOFFS = [1.0, 2.0]

# Upper bound on the per-owner known-repo baseline retained in state, so the
# union-accumulation in collect_owner_repos cannot grow without limit.
KNOWN_REPOS_CAP = 500


DEFAULT_CONFIG = {
    "enabled": False,
    "timezone": "Asia/Shanghai",
    "notify_first_run": False,
    "notify_errors": True,
    "max_items_per_run": 20,
    "state_max_seen": 5000,
    "github_token_env": DEFAULT_TOKEN_ENV,
    "github_token_file": "",
    "github_token": "",
    "users": [],
    "owners": [],
    "repos": [],
}


EXAMPLE_CONFIG = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "notify_first_run": False,
    "notify_errors": True,
    "max_items_per_run": 20,
    "state_max_seen": 5000,
    "github_token_env": DEFAULT_TOKEN_ENV,
    # Recommended: store the token in a separate ACL-protected file instead of
    # the plaintext github_token field below. Absolute path or relative to this
    # script's directory. e.g. "C:/Users/WIN/.hermes/.github-token"
    "github_token_file": "",
    "github_token": "",
    "users": [
        {
            "login": "torvalds",
            "events": DEFAULT_USER_EVENTS,
            "max_events": 20,
        }
    ],
    "owners": [
        {
            "login": "openai",
            "max_repos": 10,
            "include_forks": False,
        }
    ],
    "repos": [
        {
            "repo": "openai/openai-python",
            "watch": ["releases", "commits"],
            "branch": "",
        }
    ],
}


@dataclass(frozen=True)
class WatchItem:
    key: str
    target: str
    title: str
    url: str
    when: str = ""
    detail: str = ""
    # Raw timestamp string (ISO 8601) used for chronological sorting.
    when_raw: str = ""


class GitHubError(RuntimeError):
    pass


# Per-run ETag cache. Populated only on the check path (so query/dry-run stay
# fresh); None means "do not use conditional requests". Guarded by _ETAG_LOCK
# because collect_all fetches targets concurrently.
_ETAG_CACHE: dict[str, str] | None = None
_ETAG_LOCK = threading.Lock()
_ETAG_TOUCHED: set[str] | None = None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def ensure_config_files() -> bool:
    created = False
    example_path = SCRIPT_DIR / "github-watch-config.example.json"
    if not example_path.exists():
        atomic_write_json(example_path, EXAMPLE_CONFIG)
    if not CONFIG_PATH.exists():
        atomic_write_json(CONFIG_PATH, DEFAULT_CONFIG)
        created = True
    return created


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Invalid JSON in {path}: top-level value must be an object")
    return value


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, DEFAULT_CONFIG))
    return cfg


def load_state() -> dict[str, Any]:
    state = load_json(STATE_PATH, {"seen": {}, "initialized_targets": []})
    if not isinstance(state.get("seen"), dict):
        state["seen"] = {}
    if not isinstance(state.get("initialized_targets"), list):
        state["initialized_targets"] = []
    if not isinstance(state.get("etags"), dict):
        state["etags"] = {}
    if not isinstance(state.get("last_commits"), dict):
        state["last_commits"] = {}
    if not isinstance(state.get("known_repos"), dict):
        state["known_repos"] = {}
    return state


def save_state(state: dict[str, Any], max_seen: int) -> None:
    seen = state.get("seen", {})
    if isinstance(seen, dict) and len(seen) > max_seen:
        ordered = sorted(
            seen.items(),
            key=lambda item: str(item[1].get("seen_at", "")) if isinstance(item[1], dict) else "",
            reverse=True,
        )
        state["seen"] = dict(ordered[:max_seen])
    # Keep the ETag cache tight: only retain entries we touched this run so it
    # does not grow unbounded as targets are added/removed over time.
    etags = state.get("etags")
    if isinstance(etags, dict) and _ETAG_TOUCHED is not None:
        state["etags"] = {k: v for k, v in etags.items() if k in _ETAG_TOUCHED}
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(STATE_PATH, state)


def github_token(config: dict[str, Any]) -> str:
    # Precedence: environment variable (temporary override, highest) >
    # token file (persistent, ACL-protected, cron-restart-safe) >
    # config plaintext field (legacy/convenience, not recommended).
    env_name = str(config.get("github_token_env") or DEFAULT_TOKEN_ENV)
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    token_file = str(config.get("github_token_file") or "").strip()
    if token_file:
        path = Path(token_file)
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        try:
            value = path.read_text(encoding="utf-8", errors="replace").strip()
            if value:
                return value
        except OSError:
            pass  # missing/unreadable -> fall through to config field
    return str(config.get("github_token") or "").strip()


def _build_url(path: str, params: dict[str, Any] | None) -> str:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}
        )
    return f"{API_ROOT}{path}{query}"


def api_get(path: str, config: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
    url = _build_url(path, params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-github-watch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = github_token(config)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Conditional request: reuse the ETag we stored for this exact URL so
    # GitHub can answer 304 (does not count against rate limit).
    etag = None
    if _ETAG_CACHE is not None:
        with _ETAG_LOCK:
            etag = _ETAG_CACHE.get(url)
            if _ETAG_TOUCHED is not None:
                _ETAG_TOUCHED.add(url)
    if etag:
        headers["If-None-Match"] = etag

    last_exc: Exception | None = None
    for attempt in range(len(RETRY_BACKOFFS) + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                new_etag = resp.headers.get("ETag")
                if new_etag and _ETAG_CACHE is not None:
                    with _ETAG_LOCK:
                        _ETAG_CACHE[url] = new_etag
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                # Not modified since last fetch; caller treats None as "no new data".
                return None
            if exc.code >= 500 and attempt < len(RETRY_BACKOFFS):
                last_exc = exc
                time.sleep(RETRY_BACKOFFS[attempt])
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            reset = exc.headers.get("X-RateLimit-Reset")
            rate = ""
            if reset and reset.isdigit():
                rate = f" rate_limit_reset={format_time(int(reset), config)}"
            raise GitHubError(f"GitHub API {exc.code} for {url}.{rate} {body[:300]}") from exc
        except urllib.error.URLError as exc:
            if attempt < len(RETRY_BACKOFFS):
                last_exc = exc
                time.sleep(RETRY_BACKOFFS[attempt])
                continue
            raise GitHubError(f"GitHub request failed for {url}: {exc}") from exc
    # Exhausted retries on a transient error.
    raise GitHubError(f"GitHub request failed for {url} after retries: {last_exc}")


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(value: str | int | float | None, config: dict[str, Any]) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        dt = parse_time(str(value))
        if dt is None:
            return str(value)
    tz_name = str(config.get("timezone") or "Asia/Shanghai")
    if ZoneInfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[: limit - 3] + "..." if len(line) > limit else line


def sort_items_by_time(items: list[WatchItem]) -> list[WatchItem]:
    """Sort newest-first by raw timestamp; items without a parseable time sink to the end."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def key(item: WatchItem) -> datetime:
        dt = parse_time(item.when_raw)
        return dt if dt is not None else epoch

    return sorted(items, key=key, reverse=True)


def collect_repo_releases(repo: str, config: dict[str, Any], per_page: int = 10) -> list[WatchItem]:
    data = api_get(f"/repos/{repo}/releases", config, {"per_page": per_page})
    items: list[WatchItem] = []
    if not isinstance(data, list):
        return items
    target = f"repo:{repo}:releases"
    for rel in data:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        rid = str(rel.get("id") or rel.get("tag_name") or "")
        if not rid:
            continue
        tag = str(rel.get("tag_name") or "")
        name = str(rel.get("name") or tag)
        published = str(rel.get("published_at") or rel.get("created_at") or "")
        pre = " prerelease" if rel.get("prerelease") else ""
        items.append(
            WatchItem(
                key=f"{target}:{rid}",
                target=target,
                title=f"{repo} 发布了 {name or tag}{pre}",
                url=str(rel.get("html_url") or f"https://github.com/{repo}/releases"),
                when=format_time(published, config),
                detail=f"tag: {tag}" if tag else "",
                when_raw=published,
            )
        )
    return items


def collect_repo_commits(
    repo: str,
    branch: str,
    config: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    per_page: int = 30,
    max_pages: int = 3,
) -> list[WatchItem]:
    """Fetch recent commits, paging until we reach the last seen SHA.

    Without this, a busy repo could push >per_page commits between checks and
    the missed ones would never be notified (they age out of the first page
    before we see them). `state` records the newest SHA seen so the next run
    knows where to stop; pass None for a stateless fresh fetch (query/dry-run).
    """
    branch_label = branch or "default"
    target = f"repo:{repo}:commits:{branch_label}"
    last_sha = ""
    if state:
        last_sha = str((state.get("last_commits") or {}).get(target) or "")

    items: list[WatchItem] = []
    newest_sha = ""
    for page in range(1, max_pages + 1):
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if branch:
            params["sha"] = branch
        data = api_get(f"/repos/{repo}/commits", config, params)
        if not isinstance(data, list) or not data:
            break
        if page == 1 and isinstance(data[0], dict):
            newest_sha = str(data[0].get("sha") or "")

        stop = False
        for commit in data:
            if not isinstance(commit, dict):
                continue
            sha = str(commit.get("sha") or "")
            if not sha:
                continue
            if last_sha and sha == last_sha:
                stop = True
                break
            info = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
            author = info.get("author") if isinstance(info.get("author"), dict) else {}
            message = first_line(str(info.get("message") or ""))
            author_name = str(author.get("name") or "")
            when = str(author.get("date") or "")
            detail = message
            if author_name:
                detail = f"{message} - {author_name}" if message else author_name
            items.append(
                WatchItem(
                    key=f"{target}:{sha}",
                    target=target,
                    title=f"{repo} 有新提交 {sha[:7]}",
                    url=str(commit.get("html_url") or f"https://github.com/{repo}/commit/{sha}"),
                    when=format_time(when, config),
                    detail=detail,
                    when_raw=when,
                )
            )
        if stop:
            break
        if len(data) < per_page:
            break  # last page reached

    if state and newest_sha:
        state.setdefault("last_commits", {})[target] = newest_sha
    return items


def collect_user_events(user_cfg: dict[str, Any], config: dict[str, Any]) -> list[WatchItem]:
    login = str(user_cfg.get("login") or "").strip()
    if not login:
        return []
    max_events = int(user_cfg.get("max_events") or 20)
    allowed = user_cfg.get("events") or DEFAULT_USER_EVENTS
    allowed_set = {str(x) for x in allowed}
    data = api_get(
        f"/users/{urllib.parse.quote(login)}/events/public",
        config,
        {"per_page": max(1, min(max_events, 100))},
    )
    if not isinstance(data, list):
        return []
    target = f"user:{login}:events"
    items: list[WatchItem] = []
    for event in data:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if allowed_set and etype not in allowed_set:
            continue
        eid = str(event.get("id") or "")
        repo = event.get("repo") if isinstance(event.get("repo"), dict) else {}
        repo_name = str(repo.get("name") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        created = str(event.get("created_at") or "")
        title, url, detail = format_event(login, etype, repo_name, payload)
        items.append(
            WatchItem(
                key=f"{target}:{eid}",
                target=target,
                title=title,
                url=url,
                when=format_time(created, config),
                detail=detail,
                when_raw=created,
            )
        )
    return items


# --- event formatting -----------------------------------------------------

def _event_repo_url(repo_name: str, login: str) -> str:
    return f"https://github.com/{repo_name}" if repo_name else f"https://github.com/{login}"


def _evt_push(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []
    messages = [first_line(str(c.get("message") or ""), 80) for c in commits if isinstance(c, dict)]
    ref = str(payload.get("ref") or "").replace("refs/heads/", "")
    title = f"{login} 向 {repo_name} 推送了 {len(commits)} 个提交"
    detail = f"branch: {ref}"
    if messages:
        detail += " | " + "; ".join(messages[:3])
    return title, _event_repo_url(repo_name, login), detail


def _evt_release(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
    tag = str(release.get("tag_name") or "")
    action = str(payload.get("action") or "published")
    url = str(release.get("html_url") or f"{_event_repo_url(repo_name, login)}/releases")
    return f"{login} 在 {repo_name} {action} release {tag}", url, ""


def _evt_create(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    ref_type = str(payload.get("ref_type") or "resource")
    ref = str(payload.get("ref") or "")
    suffix = f" {ref}" if ref else ""
    return f"{login} 在 {repo_name} 创建了 {ref_type}{suffix}", _event_repo_url(repo_name, login), ""


def _evt_pull_request(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    title = first_line(str(pr.get("title") or ""))
    action = str(payload.get("action") or "")
    url = str(pr.get("html_url") or f"{_event_repo_url(repo_name, login)}/pulls")
    return f"{login} 在 {repo_name} {action} PR: {title}", url, ""


def _evt_issues(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    title = first_line(str(issue.get("title") or ""))
    action = str(payload.get("action") or "")
    url = str(issue.get("html_url") or f"{_event_repo_url(repo_name, login)}/issues")
    return f"{login} 在 {repo_name} {action} issue: {title}", url, ""


def _evt_watch(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    return f"{login} star 了 {repo_name}", _event_repo_url(repo_name, login), ""


def _evt_fork(login: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    forkee = payload.get("forkee") if isinstance(payload.get("forkee"), dict) else {}
    url = str(forkee.get("html_url") or _event_repo_url(repo_name, login))
    return f"{login} fork 了 {repo_name}", url, str(forkee.get("full_name") or "")


EVENT_HANDLERS: dict[str, Callable[[str, str, dict[str, Any]], tuple[str, str, str]]] = {
    "PushEvent": _evt_push,
    "ReleaseEvent": _evt_release,
    "CreateEvent": _evt_create,
    "PullRequestEvent": _evt_pull_request,
    "IssuesEvent": _evt_issues,
    "WatchEvent": _evt_watch,
    "ForkEvent": _evt_fork,
}


def format_event(login: str, etype: str, repo_name: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    handler = EVENT_HANDLERS.get(etype)
    if handler:
        return handler(login, repo_name, payload)
    return f"{login} 触发了 {etype} 于 {repo_name}", _event_repo_url(repo_name, login), ""


def collect_owner_repos(
    owner_cfg: dict[str, Any],
    config: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    sort: str = "pushed",
) -> list[WatchItem]:
    """Fetch an owner's repos and detect newly created ones.

    `sort` controls the repos endpoint ordering (pushed|created|updated|...).
    When `state` is supplied we remember which repos we have already seen for
    this owner and emit a distinct "newly created repository" item for any repo
    appearing for the first time. This reliably catches repo creation, which
    the events API frequently fails to surface. First-run suppression is handled
    by filter_new_items via a dedicated `owner:{login}:newrepo` target.
    """
    login = str(owner_cfg.get("login") or "").strip()
    if not login:
        return []
    max_repos = int(owner_cfg.get("max_repos") or 10)
    include_forks = bool(owner_cfg.get("include_forks"))
    data = api_get(
        f"/users/{urllib.parse.quote(login)}/repos",
        config,
        {
            "sort": sort,
            "direction": "desc",
            "per_page": max(1, min(max_repos, 100)),
            "type": "owner",
        },
    )
    if not isinstance(data, list):
        return []
    target = f"owner:{login}:repos"
    newrepo_target = f"owner:{login}:newrepo"
    items: list[WatchItem] = []

    known = state.setdefault("known_repos", {}).setdefault(login, []) if state else None
    known_set = set(known) if known is not None else None
    seen_now: list[str] = []

    for repo in data:
        if not isinstance(repo, dict):
            continue
        if repo.get("fork") and not include_forks:
            continue
        full = str(repo.get("full_name") or "")
        pushed = str(repo.get("pushed_at") or repo.get("updated_at") or "")
        created = str(repo.get("created_at") or "")
        if not full or not pushed:
            continue
        desc = first_line(str(repo.get("description") or ""), 120)
        items.append(
            WatchItem(
                # pushed_at is part of the key on purpose: it only changes when
                # the repo actually receives a new push, so each distinct push
                # notifies exactly once and quiet periods stay quiet.
                key=f"{target}:{full}:{pushed}",
                target=target,
                title=f"{full} 有新的仓库更新",
                url=str(repo.get("html_url") or f"https://github.com/{full}"),
                when=format_time(pushed, config),
                detail=desc,
                when_raw=pushed,
            )
        )
        if known is not None:
            seen_now.append(full)
            # Newly created repo: only flag when we have a prior baseline
            # (known_set non-empty) and this repo was not in it. An empty
            # known_set means first run for this owner — filter_new_items
            # suppresses it via the newrepo target's first-run rule.
            if known_set and full not in known_set:
                items.append(
                    WatchItem(
                        key=f"{newrepo_target}:{full}",
                        target=newrepo_target,
                        title=f"{login} 新建了仓库 {full}",
                        url=str(repo.get("html_url") or f"https://github.com/{full}"),
                        when=format_time(created or pushed, config),
                        detail=desc,
                        when_raw=created or pushed,
                    )
                )

    # Persist the known-repo list as a growing union (old ∪ seen this run).
    # We must NOT replace it with just `seen_now`: owners are fetched with a
    # capped per_page (max_repos, default 10) sorted by pushed_at, so a repo
    # that goes quiet can fall out of the window. If we dropped it from known,
    # its later re-entry (after a new push) would be falsely flagged as newly
    # created. Union-accumulation keeps it recognized. Capped to bound growth.
    if known is not None:
        merged = (known_set or set()) | set(seen_now)
        if len(merged) > KNOWN_REPOS_CAP:
            # Keep the most recently seen plus arbitrary older ones up to cap.
            merged = set(list(merged)[:KNOWN_REPOS_CAP])
        known.clear()
        known.extend(sorted(merged))

    return items


def _collect_repo_target(
    repo_cfg: dict[str, Any], config: dict[str, Any], state: dict[str, Any] | None
) -> list[WatchItem]:
    repo = str(repo_cfg.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return []
    watch = {str(x).lower() for x in (repo_cfg.get("watch") or ["releases"])}
    branch = str(repo_cfg.get("branch") or "").strip()
    items: list[WatchItem] = []
    if "releases" in watch:
        items.extend(collect_repo_releases(repo, config))
    if "commits" in watch:
        items.extend(collect_repo_commits(repo, branch, config, state=state))
    return items


def collect_all(
    config: dict[str, Any], state: dict[str, Any] | None = None
) -> tuple[list[WatchItem], list[str]]:
    items: list[WatchItem] = []
    errors: list[str] = []

    # Build (label, thunk) pairs so targets can be fetched concurrently.
    tasks: list[tuple[str, Callable[[], list[WatchItem]]]] = []
    for user_cfg in config.get("users") or []:
        if not isinstance(user_cfg, dict):
            continue
        label = f"user:{user_cfg.get('login')}"
        tasks.append((label, lambda c=user_cfg: collect_user_events(c, config)))

    for owner_cfg in config.get("owners") or []:
        if not isinstance(owner_cfg, dict):
            continue
        label = f"owner:{owner_cfg.get('login')}"
        tasks.append((label, lambda c=owner_cfg: collect_owner_repos(c, config, state=state)))

    for repo_cfg in config.get("repos") or []:
        if not isinstance(repo_cfg, dict):
            continue
        label = f"repo:{repo_cfg.get('repo')}"
        tasks.append((label, lambda c=repo_cfg: _collect_repo_target(c, config, state)))

    if not tasks:
        return items, errors

    max_workers = min(8, len(tasks))
    if len(tasks) == 1:
        handlers = [(label, fn) for label, fn in tasks]
        results = [(label, _safe_call(fn)) for label, fn in handlers]
    else:
        results: list[tuple[str, tuple[list[WatchItem], Exception | None]]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {ex.submit(fn): label for label, fn in tasks}
            for fut in as_completed(future_map):
                results.append((future_map[fut], _safe_call(fut.result)))

    for label, (batch, exc) in results:
        if exc is not None:
            errors.append(f"{label} target failed: {exc}")
        items.extend(batch)
    return items, errors


def _safe_call(fn: Callable[[], list[WatchItem]]) -> tuple[list[WatchItem], Exception | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - isolate per-target failures
        return [], exc


def target_configured(config: dict[str, Any]) -> bool:
    return bool((config.get("users") or []) or (config.get("owners") or []) or (config.get("repos") or []))


def filter_new_items(
    items: list[WatchItem],
    state: dict[str, Any],
    notify_first_run: bool,
) -> list[WatchItem]:
    seen = state.setdefault("seen", {})
    initialized = set(str(x) for x in state.setdefault("initialized_targets", []))
    new_items: list[WatchItem] = []
    now = datetime.now(timezone.utc).isoformat()

    by_target: dict[str, list[WatchItem]] = {}
    for item in items:
        by_target.setdefault(item.target, []).append(item)

    for target, target_items in by_target.items():
        # The :newrepo target is gated by collect_owner_repos' known_repos
        # baseline (items are only emitted after the owner has run once), so it
        # must NOT be subject to first-run suppression here — otherwise the
        # first genuinely new repo would be silenced.
        first_target_run = target not in initialized and not target.endswith(":newrepo")
        for item in target_items:
            if item.key in seen:
                continue
            seen[item.key] = {"seen_at": now, "target": item.target}
            if notify_first_run or not first_target_run:
                new_items.append(item)
        initialized.add(target)

    state["initialized_targets"] = sorted(initialized)
    return new_items


def render_items(items: list[WatchItem], errors: list[str], config: dict[str, Any]) -> str:
    max_items = int(config.get("max_items_per_run") or 20)
    ordered = sort_items_by_time(items)
    shown = ordered[:max_items]
    lines: list[str] = []

    if shown:
        lines.append(f"GitHub 监控发现 {len(items)} 条新动态（按时间倒序，展示最新 {len(shown)} 条）")
        lines.append("")
        for idx, item in enumerate(shown, start=1):
            lines.append(f"{idx}. {item.title}")
            if item.when:
                lines.append(f"   时间: {item.when}")
            if item.detail:
                lines.append(f"   详情: {item.detail}")
            if item.url:
                lines.append(f"   链接: {item.url}")
        if len(items) > len(shown):
            lines.append("")
            lines.append(f"还有 {len(items) - len(shown)} 条已记录但未展示。")

    if errors:
        if lines:
            lines.append("")
        lines.append("GitHub 监控有错误")
        for err in errors[:10]:
            lines.append(f"- {err}")
        if len(errors) > 10:
            lines.append(f"- 还有 {len(errors) - 10} 个错误未展示")

    return "\n".join(lines).strip()


def save_config(config: dict[str, Any]) -> None:
    atomic_write_json(CONFIG_PATH, config)


def validate_login(login: str) -> str:
    value = login.strip().lstrip("@")
    if not LOGIN_RE.fullmatch(value):
        raise SystemExit(
            "Invalid GitHub username. Use letters, numbers, or hyphens, max 39 chars."
        )
    return value


def validate_repo(repo: str) -> str:
    value = repo.strip()
    if not REPO_RE.fullmatch(value):
        raise SystemExit("Invalid repo. Use owner/repo, for example openai/openai-python.")
    return value


def ensure_list(config: dict[str, Any], key: str) -> list[Any]:
    value = config.get(key)
    if not isinstance(value, list):
        value = []
        config[key] = value
    return value


def command_status(config: dict[str, Any]) -> int:
    state = load_state() if STATE_PATH.exists() else {}
    seen_count = len(state.get("seen") or {})
    etag_count = len(state.get("etags") or {})
    last_run = str(state.get("last_run_at") or "")
    initialized = state.get("initialized_targets") or []
    lines = [
        "GitHub 监控状态",
        f"enabled: {bool(config.get('enabled'))}",
        f"users: {len(config.get('users') or [])}",
        f"owners: {len(config.get('owners') or [])}",
        f"repos: {len(config.get('repos') or [])}",
        f"已记录动态: {seen_count} 条",
        f"已初始化目标: {len(initialized)} 个",
        f"ETag 缓存: {etag_count} 条",
        f"上次运行: {format_time(last_run, config) or '尚未运行'}",
    ]
    print("\n".join(lines))
    return 0


def command_list(config: dict[str, Any]) -> int:
    lines = ["GitHub 监控对象"]
    users = config.get("users") or []
    owners = config.get("owners") or []
    repos = config.get("repos") or []
    if not users and not owners and not repos:
        lines.append("当前没有监控对象。")
    if users:
        lines.append("")
        lines.append("users:")
        for item in users:
            if isinstance(item, dict):
                lines.append(f"- {item.get('login')}")
    if owners:
        lines.append("")
        lines.append("owners:")
        for item in owners:
            if isinstance(item, dict):
                lines.append(f"- {item.get('login')}")
    if repos:
        lines.append("")
        lines.append("repos:")
        for item in repos:
            watch = ",".join(str(x) for x in item.get("watch", []))
            branch = str(item.get("branch") or "")
            suffix = f" [{watch}]" if watch else ""
            if branch:
                suffix += f" branch={branch}"
            lines.append(f"- {item.get('repo')}{suffix}")
    print("\n".join(lines))
    return 0


def command_enable(config: dict[str, Any], enabled: bool) -> int:
    config["enabled"] = enabled
    save_config(config)
    print("GitHub 监控已启用。" if enabled else "GitHub 监控已禁用。")
    return 0


def command_add(config: dict[str, Any], kind: str, target: str, watch: str, branch: str) -> int:
    kind = kind.lower().strip()
    config["enabled"] = True

    if kind == "user":
        login = validate_login(target)
        users = ensure_list(config, "users")
        for item in users:
            if isinstance(item, dict) and str(item.get("login", "")).lower() == login.lower():
                print(f"{login} 已在 users 监控列表中。")
                save_config(config)
                return 0
        users.append(
            {
                "login": login,
                "events": list(DEFAULT_USER_EVENTS),
                "max_events": 20,
            }
        )
        save_config(config)
        print(f"已添加用户动态监控: {login}")
        return 0

    if kind == "owner":
        login = validate_login(target)
        owners = ensure_list(config, "owners")
        for item in owners:
            if isinstance(item, dict) and str(item.get("login", "")).lower() == login.lower():
                print(f"{login} 已在 owners 监控列表中。")
                save_config(config)
                return 0
        owners.append({"login": login, "max_repos": 10, "include_forks": False})
        save_config(config)
        print(f"已添加名下项目更新监控: {login}")
        return 0

    if kind == "repo":
        repo = validate_repo(target)
        watches = [x.strip().lower() for x in watch.split(",") if x.strip()]
        watches = [x for x in watches if x in VALID_REPO_WATCHES]
        if not watches:
            watches = ["releases", "commits"]
        repos = ensure_list(config, "repos")
        for item in repos:
            if isinstance(item, dict) and str(item.get("repo", "")).lower() == repo.lower():
                item["watch"] = watches
                item["branch"] = branch.strip()
                save_config(config)
                print(f"已更新仓库监控: {repo}")
                return 0
        repos.append({"repo": repo, "watch": watches, "branch": branch.strip()})
        save_config(config)
        print(f"已添加仓库监控: {repo}")
        return 0

    raise SystemExit("Unknown kind. Use user, owner, or repo.")


def command_remove(config: dict[str, Any], target: str) -> int:
    raw = target.strip().lstrip("@")
    removed: list[str] = []
    if "/" in raw:
        repo = validate_repo(raw)
        repos = ensure_list(config, "repos")
        kept = []
        for item in repos:
            if isinstance(item, dict) and str(item.get("repo", "")).lower() == repo.lower():
                removed.append(f"repo:{repo}")
            else:
                kept.append(item)
        config["repos"] = kept
    else:
        login = validate_login(raw)
        for key, label in (("users", "user"), ("owners", "owner")):
            entries = ensure_list(config, key)
            kept = []
            for item in entries:
                if isinstance(item, dict) and str(item.get("login", "")).lower() == login.lower():
                    removed.append(f"{label}:{login}")
                else:
                    kept.append(item)
            config[key] = kept

    save_config(config)
    if removed:
        print("已移除: " + ", ".join(removed))
    else:
        print(f"没有找到监控对象: {raw}")
    return 0


def command_query(config: dict[str, Any], target: str, kind: str, limit: int, sort: str = "pushed") -> int:
    raw = target.strip().lstrip("@")
    local_cfg = dict(config)
    local_cfg["max_items_per_run"] = max(1, min(limit, 50))
    errors: list[str] = []
    items: list[WatchItem] = []

    try:
        if kind == "repo" or "/" in raw:
            repo = validate_repo(raw)
            items.extend(collect_repo_releases(repo, local_cfg, per_page=max(1, min(limit, 20))))
            items.extend(
                collect_repo_commits(repo, "", local_cfg, per_page=30, max_pages=2)
            )
        elif kind == "owner":
            login = validate_login(raw)
            items.extend(
                collect_owner_repos(
                    {"login": login, "max_repos": max(1, min(limit, 50)), "include_forks": False},
                    local_cfg,
                    state=None,
                    sort=sort,
                )
            )
        else:
            login = validate_login(raw)
            items.extend(
                collect_user_events(
                    {
                        "login": login,
                        "max_events": max(1, min(limit, 100)),
                        "events": QUERY_USER_EVENTS,
                    },
                    local_cfg,
                )
            )
    except Exception as exc:
        errors.append(str(exc))

    if not items and not errors:
        print(f"没有查到公开 GitHub 动态: {raw}")
        return 0
    # render_items sorts newest-first and truncates to max_items_per_run (=limit).
    print(render_items(items, errors, local_cfg))
    return 0


def command_trending(config: dict[str, Any], limit: int, hide_spam: bool = True) -> int:
    """List repositories created today, sorted by stars (most starred first).

    Uses the search endpoint with created:>YYYY-MM-DD and sort=stars. This is a
    point-in-time snapshot (no state, no dedup) — intended for an LLM-aware
    consumer to digest. All languages, no fork filter applied by the search.
    """
    local_cfg = dict(config)
    limit = max(1, min(limit, 30))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # created:>=today catches repos created since 00:00 UTC today.
    query = f"created:>={today} sort:stars-desc"
    errors: list[str] = []
    # Fetch more than we show so we can de-duplicate same-name clones (a common
    # spam pattern: many accounts upload identical repos on the same day).
    fetch_n = max(limit * 3, limit + 10)
    try:
        data = api_get(
            "/search/repositories",
            local_cfg,
            {"q": query, "sort": "stars", "order": "desc", "per_page": min(fetch_n, 100)},
        )
    except Exception as exc:
        errors.append(str(exc))
        data = None

    raw_items = data.get("items") if isinstance(data, dict) else None
    total = int(data.get("total_count", 0)) if isinstance(data, dict) else 0

    if errors:
        print("GitHub trending 查询失败:")
        for e in errors:
            print(f"- {e}")
        return 0
    if not raw_items:
        print(f"今日（UTC {today}）暂无新建仓库数据。")
        return 0

    # De-duplicate spam waves: the same content often appears under many
    # owner/repo names and slightly varied names (Foo, Foo-Extreme, Foo-Max...)
    # on the same day. Cluster by normalized description, then by repo name,
    # keeping the top-starred representative per cluster and noting how many
    # clones were folded in.
    def _cluster_key(repo: dict[str, Any]) -> str:
        full = str(repo.get("full_name") or "")
        name = full.split("/", 1)[-1].lower() if "/" in full else full.lower()
        # Strip variant/edition words and year/version markers anywhere in the
        # name so spam waves (Foo, Foo-Extreme, Foo-2026-Max ...) collapse to
        # one cluster. Also remove non-alphanumerics for a fuzzier match.
        name = re.sub(r"(extreme|mega|max|elite|plus|pro|ultra|turbo|edition|2026|v\d+)", "", name)
        name = re.sub(r"[^a-z0-9]", "", name)
        desc = first_line(str(repo.get("description") or ""), 160).lower()
        if desc:
            # Same idea for descriptions: drop variant words, then prefix so
            # boilerplate that only differs by an edition word clusters together.
            desc_norm = re.sub(r"(extreme|mega|max|elite|plus|pro|ultra|turbo|edition|2026|v\d+)", "", desc)
            desc_norm = re.sub(r"[^a-z0-9]", "", desc_norm)
            return desc_norm[:50] or name
        return name

    best_by_cluster: dict[str, dict[str, Any]] = {}
    cluster_count: dict[str, int] = {}
    for repo in raw_items:
        if not isinstance(repo, dict):
            continue
        key = _cluster_key(repo)
        cluster_count[key] = cluster_count.get(key, 0) + 1
        prev = best_by_cluster.get(key)
        if prev is None or (repo.get("stargazers_count", 0) > prev.get("stargazers_count", 0)):
            best_by_cluster[key] = repo

    ranked = sorted(
        best_by_cluster.values(),
        key=lambda r: r.get("stargazers_count", 0),
        reverse=True,
    )
    # Optionally hide clusters that look like coordinated spam (many same-content
    # clones uploaded the same day). Threshold: >=5 folded clones.
    SPAM_THRESHOLD = 5
    spam_hidden = 0
    if hide_spam:
        kept = []
        for repo in ranked:
            ckey = _cluster_key(repo)
            if cluster_count.get(ckey, 1) >= SPAM_THRESHOLD:
                spam_hidden += 1
                continue
            kept.append(repo)
        ranked = kept
    items = ranked[:limit]
    deduped = len(items)

    lines = [
        f"今日（UTC {today}）热门新建仓库，共匹配 {total} 个，去重后展示 star 最高的 {deduped} 个（按 star 倒序）：",
    ]
    if hide_spam and spam_hidden:
        lines.append(f"（已隐藏 {spam_hidden} 组疑似刷量仓库，用 --show-spam 查看）")
    lines.append("")
    for idx, repo in enumerate(items, start=1):
        if not isinstance(repo, dict):
            continue
        full = str(repo.get("full_name") or "")
        ckey = _cluster_key(repo)
        stars = repo.get("stargazers_count", 0)
        lang = str(repo.get("language") or "未指定")
        desc = first_line(str(repo.get("description") or ""), 160)
        created = str(repo.get("created_at") or "")
        url = str(repo.get("html_url") or f"https://github.com/{full}")
        clones = cluster_count.get(ckey, 1)
        clone_note = f"  (⚠ 疑似刷量，已折叠 {clones} 个同类)" if clones > 1 else ""
        lines.append(f"{idx}. {full}  ★ {stars}  [{lang}]{clone_note}")
        if desc:
            lines.append(f"   {desc}")
        lines.append(f"   创建: {format_time(created, local_cfg)}")
        lines.append(f"   链接: {url}")
    print("\n".join(lines))
    return 0


def command_analyze(config: dict[str, Any], target: str) -> int:
    """Pull a single repo's metadata + README + latest release + recent commits.

    Output is structured and explicitly LLM-friendly: fenced sections separated
    by clear markers so a downstream agent (Hermes) can split and reason over it.
    No state is written.
    """
    local_cfg = dict(config)
    repo = validate_repo(target.strip())
    errors: list[str] = []

    meta = None
    readme = ""
    releases: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []

    try:
        meta = api_get(f"/repos/{repo}", local_cfg)
    except Exception as exc:
        errors.append(f"meta: {exc}")
    try:
        # README endpoint returns JSON with base64-encoded content under the
        # default Accept header our api_get uses; decode it here.
        rd = api_get(f"/repos/{repo}/readme", local_cfg)
        if isinstance(rd, dict):
            content = str(rd.get("content") or "")
            if content and rd.get("encoding") == "base64":
                import base64
                readme = base64.b64decode(content).decode("utf-8", errors="replace")
            elif content:
                readme = content
        elif isinstance(rd, str):
            readme = rd
        # Truncate to keep payload bounded for an LLM consumer.
        if len(readme) > 6000:
            readme = readme[:6000] + "\n...[README truncated]"
    except Exception as exc:
        errors.append(f"readme: {exc}")
    try:
        rel = api_get(f"/repos/{repo}/releases", local_cfg, {"per_page": 3})
        if isinstance(rel, list):
            releases = [r for r in rel if isinstance(r, dict)]
    except Exception as exc:
        errors.append(f"releases: {exc}")
    try:
        cm = api_get(f"/repos/{repo}/commits", local_cfg, {"per_page": 5})
        if isinstance(cm, list):
            commits = [c for c in cm if isinstance(c, dict)]
    except Exception as exc:
        errors.append(f"commits: {exc}")

    if meta is None and not errors:
        print(f"未找到仓库: {repo}")
        return 0

    lines: list[str] = [f"# 仓库分析：{repo}", ""]
    if isinstance(meta, dict):
        lines.append("## 元信息")
        lines.append(f"- 描述: {first_line(str(meta.get('description') or ''), 200)}")
        lines.append(f"- Stars: {meta.get('stargazers_count', 0)}  Forks: {meta.get('forks_count', 0)}  Watchers: {meta.get('subscribers_count', 0)}")
        lines.append(f"- 语言: {meta.get('language') or '未指定'}  License: {(meta.get('license') or {}).get('spdx_id') or '未指定'}")
        lines.append(f"- 主页: {meta.get('homepage') or '无'}")
        lines.append(f"- 创建于: {format_time(str(meta.get('created_at') or ''), local_cfg)}")
        lines.append(f"- 最近推送: {format_time(str(meta.get('pushed_at') or ''), local_cfg)}")
        topics = meta.get("topics")
        if isinstance(topics, list) and topics:
            lines.append(f"- Topics: {', '.join(str(t) for t in topics[:10])}")
        lines.append(f"- 链接: {meta.get('html_url') or f'https://github.com/{repo}'}")
        lines.append("")

    if releases:
        lines.append("## 最近 Release")
        for rel in releases[:3]:
            tag = rel.get("tag_name") or ""
            name = rel.get("name") or tag
            pub = rel.get("published_at") or ""
            pre = " [prerelease]" if rel.get("prerelease") else ""
            lines.append(f"- {name or tag}{pre} ({format_time(str(pub), local_cfg)}) tag={tag}")
        lines.append("")

    if commits:
        lines.append("## 最近提交")
        for cm in commits[:5]:
            sha = str(cm.get("sha") or "")[:7]
            info = cm.get("commit") if isinstance(cm.get("commit"), dict) else {}
            msg = first_line(str(info.get("message") or ""), 120)
            author = (info.get("author") or {}).get("name") if isinstance(info.get("author"), dict) else ""
            lines.append(f"- {sha} {msg}" + (f" — {author}" if author else ""))
        lines.append("")

    if readme:
        lines.append("## README（节选）")
        lines.append("```")
        lines.append(readme.strip())
        lines.append("```")
        lines.append("")

    if errors:
        lines.append("## 抓取过程中的错误")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("## 分析提示")
    lines.append("以上为仓库结构化信息，可供进一步分析：项目定位、技术栈、活跃度、近期动向等。")
    print("\n".join(lines).strip())
    return 0


def print_dry_run(config: dict[str, Any]) -> int:
    items, errors = collect_all(config)
    ordered = sort_items_by_time(items)
    lines = [
        "GitHub watch dry run",
        f"targets: users={len(config.get('users') or [])}, owners={len(config.get('owners') or [])}, repos={len(config.get('repos') or [])}",
        f"fetched_items: {len(items)}",
    ]
    if ordered:
        lines.append("")
        for idx, item in enumerate(ordered[:20], start=1):
            lines.append(f"{idx}. {item.title}")
            if item.when:
                lines.append(f"   time: {item.when}")
            if item.detail:
                lines.append(f"   detail: {item.detail}")
            if item.url:
                lines.append(f"   url: {item.url}")
    if errors:
        lines.append("")
        lines.append("errors:")
        lines.extend(f"- {e}" for e in errors)
    print("\n".join(lines))
    return 1 if errors else 0


def command_help() -> int:
    print(
        "\n".join(
            [
                "GitHub 监控 - 指令帮助",
                "",
                "查询类（不修改配置、不写入状态，结果按时间倒序）：",
                "  /github query <用户名|owner/repo>           查询最新公开动态（自动识别用户/仓库）",
                "  /github query <用户名> --kind owner --sort created  列出某用户名下仓库（按创建时间）",
                "  /github query <用户名> --limit 20          指定展示条数（最新 N 条）",
                "  /github trending [--limit N] [--show-spam]  今日新建的热门仓库（按 star 倒序，默认隐藏刷量，供 LLM 分析）",
                "  /github analyze <owner/repo>               拉取单仓库元信息/README/release/commit（供 LLM 分析）",
                "  /github list                               查看当前已配置的监控对象",
                "  /github status                             查看监控状态（含已记录动态数、上次运行时间）",
                "",
                "管理类（修改配置）：",
                "  /github add <用户名>                        添加用户动态监控",
                "  /github add owner <用户名>                  添加用户名下仓库更新监控（含新建仓库提醒）",
                "  /github add <owner/repo>                    添加仓库监控（默认 releases+commits）",
                "  /github add <owner/repo> --watch releases   仅监控 release",
                "  /github add <owner/repo> --branch main      指定分支监控提交",
                "  /github remove <用户名|owner/repo>          移除监控对象",
                "  /github enable                             启用定时监控",
                "  /github disable                            禁用定时监控",
                "",
                "运行类：",
                "  /github check                              立即执行一次监控并更新状态（cron 自动调用）",
                "  /github check --show-empty                 没有新动态时也给出提示",
                "",
                "说明：",
                "- 监控对象分三类：users（用户公开动态）、owners（名下仓库更新+新建仓库）、repos（指定仓库的 release/commit）",
                "- 首次添加某对象时不会刷屏，仅建立基线；之后只通知新增动态",
                "- token 建议放在配置文件指向的 token 文件中，而非明文写入配置",
                "  更多细节见 github-watch-config.example.json",
            ]
        )
    )
    return 0


def main(argv: list[str]) -> int:
    # Force UTF-8 stdout/stderr so emoji/CJK from GitHub payloads (e.g. READMEs)
    # don't crash on Windows' default GBK/cp936 console encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="Watch GitHub updates for Hermes cron.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print latest items without updating state.")
    parser.add_argument("--init-config", action="store_true", help="Create default config files and exit.")
    parser.add_argument("--reset-state", action="store_true", help="Delete saved state and exit.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("help", help="Show usage for all commands.")
    subparsers.add_parser("status", help="Show monitor status.")
    subparsers.add_parser("list", help="List configured targets.")
    subparsers.add_parser("enable", help="Enable monitoring.")
    subparsers.add_parser("disable", help="Disable monitoring.")

    add_parser = subparsers.add_parser("add", help="Add a target.")
    add_parser.add_argument(
        "kind_or_target",
        help="Target username by default, or kind: user|owner|repo.",
    )
    add_parser.add_argument("target", nargs="?", help="Target when kind is supplied.")
    add_parser.add_argument(
        "--watch",
        default="releases,commits",
        help="Repo watch list: releases, commits, or releases,commits.",
    )
    add_parser.add_argument("--branch", default="", help="Repo branch for commit checks.")

    remove_parser = subparsers.add_parser("remove", help="Remove a target.")
    remove_parser.add_argument("target", help="Username or owner/repo.")

    query_parser = subparsers.add_parser("query", help="Query latest public GitHub data without saving state. Results are sorted newest-first.")
    query_parser.add_argument("target", help="Username, owner, or owner/repo.")
    query_parser.add_argument(
        "--kind",
        choices=["user", "owner", "repo"],
        default="user",
        help="Query type. owner/repo targets are auto-detected as repo.",
    )
    query_parser.add_argument("--limit", type=int, default=10, help="Maximum items to show (latest N).")
    query_parser.add_argument(
        "--sort",
        choices=["pushed", "created", "updated", "full_name"],
        default="pushed",
        help="Repo ordering for --kind owner. Use 'created' to see newest repositories first.",
    )

    check_parser = subparsers.add_parser("check", help="Run one monitor check and update state.")
    check_parser.add_argument("--show-empty", action="store_true", help="Print a message even when there are no new items.")

    trending_parser = subparsers.add_parser("trending", help="List repositories created today, sorted by stars (LLM-friendly snapshot).")
    trending_parser.add_argument("--limit", type=int, default=10, help="Maximum repos to show (latest N by stars, max 30).")
    trending_parser.add_argument(
        "--show-spam",
        action="store_true",
        help="Show suspected spam clusters (>=5 same-content clones) instead of hiding them.",
    )

    analyze_parser = subparsers.add_parser("analyze", help="Pull a repo's metadata + README + releases + commits as LLM-friendly structured text.")
    analyze_parser.add_argument("target", help="owner/repo to analyze.")

    args = parser.parse_args(argv)

    created = ensure_config_files()
    if args.init_config:
        print("Config ready.")
        print("Example config ready.")
        return 0
    if args.reset_state:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        print("State reset.")
        return 0

    config = load_config()
    if created and args.command is None:
        print("Created config.")
        print("Edit it, set enabled=true, then add users/owners/repos.")
        print("Example config ready.")
        return 0

    if args.dry_run:
        return print_dry_run(config)

    if args.command == "help":
        return command_help()
    if args.command == "status":
        return command_status(config)
    if args.command == "list":
        return command_list(config)
    if args.command == "enable":
        return command_enable(config, True)
    if args.command == "disable":
        return command_enable(config, False)
    if args.command == "add":
        if args.target:
            kind = str(args.kind_or_target).lower().strip()
            target = str(args.target)
        else:
            kind = "repo" if "/" in str(args.kind_or_target) else "user"
            target = str(args.kind_or_target)
        return command_add(config, kind, target, args.watch, args.branch)
    if args.command == "remove":
        return command_remove(config, args.target)
    if args.command == "query":
        return command_query(config, args.target, args.kind, max(1, args.limit), args.sort)
    if args.command == "trending":
        return command_trending(config, max(1, args.limit), hide_spam=not args.show_spam)
    if args.command == "analyze":
        return command_analyze(config, args.target)

    if not config.get("enabled") and args.command != "check":
        return 0

    if not target_configured(config):
        print("No GitHub targets configured.")
        return 0

    # Activate ETag caching only for the stateful check path so query/dry-run
    # always return fresh data.
    global _ETAG_CACHE, _ETAG_TOUCHED
    state = load_state()
    _ETAG_CACHE = state.setdefault("etags", {})
    _ETAG_TOUCHED = set()

    items, errors = collect_all(config, state=state)
    new_items = filter_new_items(
        items=items,
        state=state,
        notify_first_run=bool(config.get("notify_first_run")),
    )
    save_state(state, int(config.get("state_max_seen") or 5000))

    # Reset run-scoped globals so a later in-process query stays fresh.
    _ETAG_CACHE = None
    _ETAG_TOUCHED = None

    if not config.get("notify_errors"):
        errors = []

    output = render_items(new_items, errors, config)
    if output:
        print(output)
    elif args.command == "check" and args.show_empty:
        print("没有新的 GitHub 动态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
