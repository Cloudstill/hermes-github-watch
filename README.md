# hermes-github-watch

[English](#english) · [中文](#中文)

---

<a id="english"></a>

## English

A **zero-dependency, single-file** GitHub activity monitor designed as a cron plugin for [Hermes](https://github.com/) (and usable standalone). It watches GitHub for new activity and prints a time-sorted digest — empty output means "nothing new", which lets any cron runner stay silent until there's something worth saying.

> Stdlib-only Python 3.9+. No `pip install`. No database. No daemon. Drop the script into your cron and go.

### Why this exists / 为什么做这个

Most GitHub notifier projects focus on **releases only** and ship as a SaaS or a full web app you have to deploy. This plugin takes a different bet:

- **It catches newly-created repositories reliably.** GitHub's public Events API frequently fails to surface `CreateEvent` for repo creation, so "did X create a new repo?" is surprisingly hard to answer. This plugin remembers each owner's known repos and diffs against them — a genuinely new repo is reported exactly once. *(A repo that temporarily falls out of the fetched window and later re-enters is NOT mistaken for new — the known set only grows.)*
- **Three monitor types in one tool**: user public events, owner repos (updates + new repos), and per-repo releases/commits.
- **Zero dependencies.** One `.py` file. Reads JSON config, writes JSON state, prints text. Ideal for restricted environments, embedded bots, and "I just want a cron job" setups.
- **Rate-limit friendly.** ETag conditional requests (304s don't count against your quota), concurrent fetching, transient-error retries, and commit paging that won't silently drop commits on busy repos.

### What it monitors

| Target kind | What you get | Source endpoint |
|-------------|--------------|-----------------|
| `user` | A user's public activity: pushes, releases, creates, PRs, issues, stars, forks | `/users/{login}/events/public` |
| `owner` | Repos under a user/org that got updates, **plus a one-time alert when a brand-new repo appears** | `/users/{login}/repos` |
| `repo` | New releases and/or new commits (per branch) for a specific repo | `/repos/{repo}/releases`, `/repos/{repo}/commits` |

### Quick start (standalone)

```bash
# 1. Create a config (auto-generates a default + example)
python github-watch.py --init-config

# 2. Edit github-watch-config.json: set enabled=true, add targets.
#    Point github_token_file at a file holding your GitHub token
#    (a fine-grained token with read-only public access is enough).

# 3. Query without saving state (results sorted newest-first):
python github-watch.py query torvalds --limit 15
python github-watch.py query Chen-Christins --kind owner --sort created

# 4. Run one stateful check (this is what cron calls):
python github-watch.py check
```

Schedule it with any cron, e.g. every 30 minutes:

```cron
*/30 * * * *  cd /path/to/dir && python github-watch.py check
```

Empty stdout = nothing new = silent tick.

### Quick start (as a Hermes plugin)

1. Copy `github-watch.py` → `<hermes-home>/scripts/github-watch.py`
2. Copy the `plugin/` directory → `<hermes-home>/plugins/github-watch/`
3. Reload Hermes. You now have a `/github` command:

```
/github add owner Chen-Christins
/github add openai/openai-python --watch releases,commits --branch main
/github enable
/github help
```

### Token setup

Token precedence (first non-empty wins): environment variable → token file (recommended) → config plaintext field.

A token is optional but strongly recommended: anonymous requests share a 60/hour IP quota; authenticated requests get 5000/hour. Only public data is read, so a fine-grained token with read-only public-repo access is sufficient.

```powershell
# Windows
Set-Content -Path "$env:USERPROFILE\.hermes\.github-token" -Value "github_pat_..." -Encoding ascii -NoNewline
icacls "$env:USERPROFILE\.hermes\.github-token" /inheritance:r /grant:r "$env:USERNAME:(R)"
```
```bash
# Linux/macOS
(umask 077; printf '%s' "github_pat_..." > ~/.github-token)
```

### Commands

| Command | Purpose |
|---------|---------|
| `query <target> [--kind user\|owner\|repo] [--sort ...] [--limit N]` | One-shot query, no state written. Sorted newest-first. |
| `add <user>` / `add owner <user>` / `add <owner/repo> [--watch ...] [--branch ...]` | Add a monitor target. |
| `remove <user\|owner/repo>` | Remove a target. |
| `list` / `status` | Show targets / show runtime status. |
| `enable` / `disable` | Toggle scheduled monitoring. |
| `check [--show-empty]` | Run one check, update state (what cron calls). |
| `--dry-run` / `--reset-state` / `help` | Utilities. |

See `github-watch-config.example.json` for the full config reference.

### Limitations & honest trade-offs

- **No built-in notification channel.** Output is text on stdout; delivery is the cron runner's job. If you want a batteries-included multi-channel notifier, [iamspido/github-release-monitor](https://github.com/iamspido/github-release-monitor) is excellent.
- **Public data only.** Private repos/events aren't watched.
- **Events API caveats.** User-activity queries come from `/events/public` (~300 events / 90 days, not real-time). For "did X create a repo?", the owner target's new-repo detection is the reliable path.
- **PR/issue titles are blank in user-event output.** The events API payload doesn't include them. Use a `repo` target for full release/commit detail.

---

<a id="中文"></a>

## 中文

一个**零依赖、单文件**的 GitHub 动态监控器，作为 [Hermes](https://github.com/) 的 cron 插件设计（也可独立使用）。它监控 GitHub 新动态并输出按时间排序的摘要——**空输出代表"没有新动态"**，让任何 cron 调度器在无事可报时保持静默。

> 仅依赖 Python 3.9+ 标准库。无需 `pip install`，无需数据库，无需常驻进程。把脚本丢进 cron 即可。

### 为什么做这个

大多数 GitHub 通知项目**只盯 release**，且要么是 SaaS、要么是要部署的完整 Web 应用。本插件走了不同的路线：

- **可靠捕捉"新建仓库"。** GitHub 公开 Events API 经常不收录仓库创建事件，所以"X 是否新建了仓库"出乎意料地难回答。本插件记住每个 owner 已知的仓库并做 diff——真正的新仓库只通知一次。*（短暂跌出抓取窗口后又回归的仓库不会被误判为新建——已知集合只增不减。）*
- **三类监控合一**：用户公开动态、用户名下仓库（更新+新建仓库）、指定仓库的 release/commit。
- **零依赖。** 一个 `.py` 文件，读 JSON 配置、写 JSON 状态、输出文本。适合受限环境、嵌入式 bot、"我就想要个 cron 任务"的场景。
- **对限流友好。** ETag 条件请求（304 不耗配额）、并发抓取、瞬时错误重试、提交分页（忙的仓库不会漏报提交）。

### 监控内容

| 目标类型 | 你能得到什么 | 数据源端点 |
|---------|------------|-----------|
| `user` | 用户公开动态：推送、release、创建、PR、issue、star、fork | `/users/{login}/events/public` |
| `owner` | 用户/组织名下有更新的仓库，**外加新建仓库的一次性提醒** | `/users/{login}/repos` |
| `repo` | 指定仓库的新 release 和/或新提交（按分支） | `/repos/{repo}/releases`、`/repos/{repo}/commits` |

### 快速开始（独立使用）

```bash
# 1. 生成配置（自动生成默认配置 + 示例配置）
python github-watch.py --init-config

# 2. 编辑 github-watch-config.json：设 enabled=true，添加监控对象。
#    把 github_token_file 指向存放 GitHub token 的文件
#    （fine-grained token，只读公开访问即可）。

# 3. 查询（不写状态，结果按时间倒序）：
python github-watch.py query torvalds --limit 15
python github-watch.py query Chen-Christins --kind owner --sort created

# 4. 执行一次有状态检查（cron 调用的就是这个）：
python github-watch.py check
```

用任意 cron 调度，例如每 30 分钟：

```cron
*/30 * * * *  cd /path/to/dir && python github-watch.py check
```

stdout 为空 = 没有新动态 = 静默。

### 快速开始（作为 Hermes 插件）

1. 把 `github-watch.py` 复制到 `<hermes-home>/scripts/github-watch.py`
2. 把 `plugin/` 目录复制到 `<hermes-home>/plugins/github-watch/`
3. 重载 Hermes。即可用 `/github` 命令：

```
/github add owner Chen-Christins
/github add openai/openai-python --watch releases,commits --branch main
/github enable
/github help
```

### Token 配置

token 解析优先级（首个非空者生效）：环境变量 → token 文件（推荐）→ 配置明文字段。

token 可选但强烈建议配置：匿名请求共享 60 次/小时的 IP 配额；认证请求 5000 次/小时。仅读取公开数据，所以 fine-grained token（只读公开仓库）就够了。

```powershell
# Windows
Set-Content -Path "$env:USERPROFILE\.hermes\.github-token" -Value "github_pat_..." -Encoding ascii -NoNewline
icacls "$env:USERPROFILE\.hermes\.github-token" /inheritance:r /grant:r "$env:USERNAME:(R)"
```
```bash
# Linux/macOS
(umask 077; printf '%s' "github_pat_..." > ~/.github-token)
```

### 命令一览

| 命令 | 用途 |
|------|------|
| `query <目标> [--kind user\|owner\|repo] [--sort ...] [--limit N]` | 一次性查询，不写状态，按时间倒序 |
| `add <用户>` / `add owner <用户>` / `add <owner/repo> [--watch ...] [--branch ...]` | 添加监控对象 |
| `remove <用户\|owner/repo>` | 移除监控对象 |
| `list` / `status` | 查看对象 / 查看运行状态 |
| `enable` / `disable` | 启用/禁用定时监控 |
| `check [--show-empty]` | 执行一次检查并更新状态（cron 调用） |
| `--dry-run` / `--reset-state` / `help` | 工具命令 |

完整配置项见 `github-watch-config.example.json`。

### 局限与坦诚的取舍

- **无内置通知渠道。** 输出是 stdout 文本；推送（邮件/Telegram/Discord/…）由 cron 调度器负责。这是 Hermes 插件模型的有意设计——如果你想要开箱即用的多渠道通知器，[iamspido/github-release-monitor](https://github.com/iamspido/github-release-monitor) 很不错。
- **仅公开数据。** 不监控私有仓库/动态。
- **Events API 的坑。** 用户动态查询走 `/events/public`（约 300 条/90 天，非实时）。要回答"X 是否新建了仓库"，请用 owner 目标的新建仓库检测，那条路才可靠。
- **用户动态里 PR/issue 标题为空。** events API 的 payload 不含标题，只显示动作+编号。要完整 release/commit 细节请用 `repo` 目标。

## License

MIT — see [LICENSE](LICENSE).
