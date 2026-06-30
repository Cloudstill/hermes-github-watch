# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.1] - 2026-07-01

### Changed
- **`analyze` no longer dumps the full README.** Previously it emitted up to
  6000 chars of raw README markdown (badges, code fences, HTML, link URLs and
  all). It now emits a compact ~400-char cleaned summary: strips badge images,
  HTML, code fences, link URLs, heading markers, list bullets, and emoji;
  flattens to prose. Output dropped from 6KB+ to ~2KB.
- Removed the agent-directed "analysis task" prompt section — `analyze` is a
  plugin command whose return value is delivered verbatim (the Hermes agent
  does not take a second reasoning pass on it), so that prompt was noise for
  human readers. The command now returns clean structured data only.

## [1.2.0] - 2026-06-28

### Added
- **`trending` command**: lists repositories created today, sorted by stars.
  De-duplicates coordinated spam clone-waves (same content under many
  owner/repo names and edition variants) and hides clusters with ≥5 clones by
  default (`--show-spam` to reveal). LLM-friendly point-in-time snapshot, no
  state written.
- **`analyze <owner/repo>` command**: pulls a repo's metadata + README (base64
  decoded, capped at 6000 chars) + latest releases + recent commits as
  sectioned, LLM-friendly structured text. No state written.
- **GitHub URL acceptance**: every repo-taking command (`analyze`, `query`,
  `add`, `remove`) now accepts full GitHub URLs and SSH strings in addition to
  bare `owner/repo` — e.g. `analyze https://github.com/o/r/pulls`,
  `git@github.com:o/r.git`. Trailing path/query/fragment/`.git` are stripped.
- **LLM integration pattern** documented: `trending`/`analyze` feed a downstream
  agent rather than calling an LLM in-process, keeping the script zero-dependency.
- UTF-8 stdout/stderr reconfiguration so emoji/CJK in GitHub payloads (e.g.
  READMEs) no longer crash on Windows' default GBK console.

## [1.1.0] - 2026-06-28

### Added
- **New-repo detection.** `owner` targets now reliably surface newly-created
  repositories by diffing against a persisted `known_repos` baseline — works
  around the GitHub Events API frequently omitting repo-creation events.
  First run establishes a baseline without spamming.
- **ETag conditional requests** (`If-None-Match` / 304) on the stateful check
  path, cutting rate-limit consumption. Cache is per-URL and pruned each run.
- **Concurrent target fetching** via `ThreadPoolExecutor` (up to 8 workers).
- **Transient-error retry** with exponential backoff for 5xx and network errors.
- **Commit paging with `last_sha` tracking** so busy repos no longer drop
  commits between checks.
- **Token file support** (`github_token_file`); precedence is env var > token
  file > config plaintext field.
- **Time-sorted output** (newest-first) across `query`, `check`, and `--dry-run`,
  with a "latest N" cap and clear truncation notice.
- **`/github help` command** and refreshed plugin USAGE.
- **`status` runtime info**: recorded item count, initialized target count,
  ETag cache size, last run time.
- **`query --kind owner --sort created|pushed|updated|full_name`**.
- Example config now documents the recommended token-file workflow.

### Fixed
- `known_repos` no longer replaces the baseline each run. It now accumulates as
  a union, so a repo that falls out of the fetched `max_repos` window and later
  re-enters is no longer falsely reported as newly created. Capped at 500 per
  owner to bound growth.
- Removed unreachable duplicate `return` in `collect_owner_repos`.

### Changed
- Refactored `format_event` from a long if/elif chain to a handler dispatch
  table (`EVENT_HANDLERS`).
- Deduplicated event-type and repo-watch constants into module-level names.
- Bumped plugin version to 1.1.0.

## [1.0.0] - 2026-06-25

### Added
- Initial release: stdlib-only GitHub watcher with `user`/`owner`/`repo`
  targets, JSON state dedup, first-run suppression, `/github` command plugin.
