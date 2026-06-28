# hermes-github-watch

> Zero-dependency, single-file GitHub activity monitor. Drop it into a cron job — it stays silent until there's actually something new to tell you.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![No dependencies](https://img.shields.io/badge/dependencies-zero-success.svg)](#)
[![Platform](https://img.shields.io/badge/platform-win%20%7C%20linux%20%7C%20mac-lightgrey.svg)](#)

**English** · [中文](#中文)

---

## English

A GitHub activity monitor designed as a cron plugin for [Hermes](https://github.com/) (and usable standalone with any cron). It watches GitHub for new activity and prints a **time-sorted digest** — empty stdout means "nothing new", so a cron runner stays silent until there's something worth saying.

> Stdlib-only Python 3.9+. No `pip install`. No database. No daemon.

### What makes it different

Most GitHub notifier projects watch **releases only** and ship as a SaaS or a full web app you must deploy. This one takes a different bet:

| | hermes-github-watch | gitpunch | github-release-monitor | github-release-notifier |
|---|---|---|---|---|
| New **repo creation** alerts | ✅ reliable | ❌ | ❌ | ❌ |
| User activity (events) | ✅ | ❌ | ❌ | ❌ |
| Owner repo updates | ✅ | ❌ | ❌ | ❌ |
| Releases + commits | ✅ | releases | releases | releases |
| Dependencies | **zero** (stdlib) | SaaS | Node/TS app | Python pkgs |
| Notification channels | stdout (cron delivers) | email | email + Apprise | webhook |
| Self-hosted, no deploy | ✅ one file | ❌ | ❌ | ✅ |

The headline feature: **it reliably catches newly-created repositories.** GitHub's public Events API frequently omits the `CreateEvent` for repo creation, so "did X create a new repo?" is surprisingly hard to answer. This plugin remembers each owner's known repos and diffs against them — a genuinely new repo is reported exactly once, and a repo that temporarily falls out of the fetched window then re-enters is *not* mistaken for new (the known set only grows).

### What it monitors

| Target | What you get | Source |
|--------|--------------|--------|
| `user` | A user's public activity: pushes, releases, creates, PRs, issues, stars, forks | `/users/{login}/events/public` |
| `owner` | Repos under a user/org that got updates, **plus a one-time alert when a brand-new repo appears** | `/users/{login}/repos` |
| `repo` | New releases and/or new commits (per branch) for a specific repo | `/repos/{repo}/releases`, `/repos/{repo}/commits` |

### Example output

```
GitHub 监控发现 3 条新动态（按时间倒序，展示最新 3 条）

1. Chen-Christins 新建了仓库 Chen-Christins/QQBot
   时间: 2026-06-27 22:59
   详情: QQ Webhook 消息推送
   链接: https://github.com/Chen-Christins/QQBot
2. openai/openai-python 发布了 v1.50.0
   时间: 2026-06-25 10:00
   详情: tag: v1.50.0
   链接: https://github.com/openai/openai-python/releases/tag/v1.50.0
3. torvalds 向 torvalds/linux 推送了 3 个提交
   时间: 2026-06-24 18:32
   详情: branch: master | merge: fix mmap; ...
   链接: https://github.com/torvalds/linux/commit/abc1234
```

Empty output → nothing new → the cron tick is silent.

### Quick start (standalone)

```bash
python github-watch.py --init-config     # generates config + example
# edit github-watch-config.json: enabled=true, add targets, point github_token_file at a token file
python github-watch.py query torvalds --limit 15        # one-shot query, newest-first
python github-watch.py check                            # stateful check (what cron runs)
```

Cron it every 30 minutes:

```cron
*/30 * * * *  cd /path/to/dir && python github-watch.py check
```

### Quick start (Hermes plugin)

1. Copy `github-watch.py` → `<hermes-home>/scripts/github-watch.py`
2. Copy `plugin/` → `<hermes-home>/plugins/github-watch/`
3. Reload Hermes, then:

```
/github add owner Chen-Christins
/github add openai/openai-python --watch releases,commits --branch main
/github enable
/github help
```

### Token setup

Optional but strongly recommended — anonymous requests share a 60/hour IP quota; authenticated get 5000/hour. Only public data is read, so a fine-grained token with read-only public-repo access is enough.

Precedence (first non-empty wins): **environment variable → token file (recommended) → config plaintext field**.

```powershell
# Windows — write token file and lock to your user
Set-Content -Path "$env:USERPROFILE\.github-token" -Value "github_pat_..." -Encoding ascii -NoNewline
icacls "$env:USERPROFILE\.github-token" /inheritance:r /grant:r "$env:USERNAME:(R)"
```
```bash
# Linux/macOS
(umask 077; printf '%s' "github_pat_..." > ~/.github-token)
```
Then set `"github_token_file"` to its path (absolute, or relative to the script).

### Commands

| Command | Purpose |
|---------|---------|
| `query <target> [--kind user\|owner\|repo] [--sort pushed\|created\|updated] [--limit N]` | One-shot query, no state written, newest-first. |
| `trending [--limit N] [--show-spam]` | Repositories created **today**, sorted by stars. De-duplicates spam clone-waves; `--show-spam` reveals them. LLM-friendly snapshot. |
| `analyze <owner/repo>` | Pull a repo's metadata + README + latest releases + recent commits as structured, LLM-friendly text. No state written. |
| `add <user>` / `add owner <user>` / `add <owner/repo> [--watch ...] [--branch ...]` | Add a monitor target. |
| `remove <user\|owner/repo>` | Remove a target. |
| `list` / `status` | Show targets / show runtime status (recorded items, last run, ETag cache). |
| `enable` / `disable` | Toggle scheduled monitoring. |
| `check [--show-empty]` | Run one check and update state (what cron calls). |
| `--dry-run` / `--reset-state` / `help` | Utilities. |

Full config reference: `github-watch-config.example.json`.

### LLM integration (trending & analyze)

`trending` and `analyze` are designed to feed an LLM, not call one — the script stays zero-dependency and single-responsibility (fetch + structure), and a downstream agent does the reasoning. Recommended flow in a Hermes agent:

1. `github-watch.py trending --limit 10` → capture stdout
2. Prompt the LLM: *"Here are today's top new GitHub repos. Pick the 2 most genuinely interesting (ignore game-cheat/trainer spam), and for each give a one-paragraph takeaway."*
3. For any pick worth a deeper look: `github-watch.py analyze <owner/repo>` → feed that structured output back to the LLM for a full brief (positioning, tech stack, activity, recent direction).

`analyze` output is sectioned with `## ` headers (`元信息` / `最近 Release` / `最近提交` / `README（节选）`) so an agent can split and cite. README is capped at 6000 chars to bound token cost.

### How dedup works (so you trust the silence)

Every item gets a stable key: releases use id/tag, commits use full SHA, owner-repo updates use `name + pushed_at` (the timestamp changes only on a real push, so each push notifies once and quiet periods stay quiet), new-repo alerts use `name`. Keys persist in `github-watch-state.json`. **The first run never spams** — it records a baseline without reporting (unless `notify_first_run: true`). State is LRU-capped (`state_max_seen`, default 5000) and the ETag cache is pruned each run, so it stays bounded.

### Limitations & honest trade-offs

- **No built-in notification channel.** Output is stdout text; delivery (email/Telegram/Discord/…) is the cron runner's job. Intentional for the plugin model — for a batteries-included multi-channel notifier, [iamspido/github-release-monitor](https://github.com/iamspido/github-release-monitor) is excellent.
- **Public data only.** Private repos/events aren't watched.
- **Events API caveats.** User-activity queries come from `/events/public` (~300 events / 90 days, not real-time). For "did X create a repo?", use the owner target — its new-repo detection is the reliable path.
- **PR/issue titles are blank in user-event output.** The events API payload omits them (action + number is shown). Use a `repo` target for full release/commit detail.

---

<a id="中文"></a>

## 中文

> 零依赖、单文件的 GitHub 动态监控器。丢进 cron，没有新动态时它一言不发，有事才报。

一个 GitHub 动态监控器，作为 [Hermes](https://github.com/) 的 cron 插件设计（也可配合任意 cron 独立使用）。它监控 GitHub 新动态并输出**按时间排序的摘要**——stdout 为空代表"没有新动态"，让 cron 调度器在无事可报时保持静默。

> 仅依赖 Python 3.9+ 标准库。无需 `pip install`，无需数据库，无需常驻进程。

### 它有什么不同

大多数 GitHub 通知项目**只盯 release**，且要么是 SaaS、要么是要部署的完整 Web 应用。本插件走了不同的路线：

| | hermes-github-watch | gitpunch | github-release-monitor | github-release-notifier |
|---|---|---|---|---|
| 新建**仓库**提醒 | ✅ 可靠 | ❌ | ❌ | ❌ |
| 用户动态 (events) | ✅ | ❌ | ❌ | ❌ |
| 名下仓库更新 | ✅ | ❌ | ❌ | ❌ |
| Release + commit | ✅ | release | release | release |
| 依赖 | **零**（标准库） | SaaS | Node/TS 应用 | Python 包 |
| 通知渠道 | stdout（cron 推送） | 邮件 | 邮件 + Apprise | webhook |
| 自托管、免部署 | ✅ 单文件 | ❌ | ❌ | ✅ |

招牌功能：**可靠捕捉"新建仓库"。** GitHub 公开 Events API 经常不收录仓库创建事件，所以"X 是否新建了仓库"出乎意料地难回答。本插件记住每个 owner 已知的仓库并做 diff——真正的新仓库只通知一次；短暂跌出抓取窗口后又回归的仓库*不会*被误判为新建（已知集合只增不减）。

### 监控内容

| 目标 | 你能得到什么 | 数据源 |
|------|------------|--------|
| `user` | 用户公开动态：推送、release、创建、PR、issue、star、fork | `/users/{login}/events/public` |
| `owner` | 用户/组织名下有更新的仓库，**外加新建仓库的一次性提醒** | `/users/{login}/repos` |
| `repo` | 指定仓库的新 release 和/或新提交（按分支） | `/repos/{repo}/releases`、`/repos/{repo}/commits` |

### 输出示例

```
GitHub 监控发现 3 条新动态（按时间倒序，展示最新 3 条）

1. Chen-Christins 新建了仓库 Chen-Christins/QQBot
   时间: 2026-06-27 22:59
   详情: QQ Webhook 消息推送
   链接: https://github.com/Chen-Christins/QQBot
2. openai/openai-python 发布了 v1.50.0
   时间: 2026-06-25 10:00
   详情: tag: v1.50.0
   链接: https://github.com/openai/openai-python/releases/tag/v1.50.0
3. torvalds 向 torvalds/linux 推送了 3 个提交
   时间: 2026-06-24 18:32
   详情: branch: master | merge: fix mmap; ...
   链接: https://github.com/torvalds/linux/commit/abc1234
```

输出为空 → 没有新动态 → 这一 tick 静默。

### 快速开始（独立使用）

```bash
python github-watch.py --init-config     # 生成配置 + 示例
# 编辑 github-watch-config.json：enabled=true，添加监控对象，把 github_token_file 指向 token 文件
python github-watch.py query torvalds --limit 15        # 一次性查询，按时间倒序
python github-watch.py check                            # 有状态检查（cron 调用）
```

每 30 分钟跑一次：

```cron
*/30 * * * *  cd /path/to/dir && python github-watch.py check
```

### 快速开始（Hermes 插件）

1. 把 `github-watch.py` 复制到 `<hermes-home>/scripts/github-watch.py`
2. 把 `plugin/` 复制到 `<hermes-home>/plugins/github-watch/`
3. 重载 Hermes，然后：

```
/github add owner Chen-Christins
/github add openai/openai-python --watch releases,commits --branch main
/github enable
/github help
```

### Token 配置

可选但强烈建议配置——匿名请求共享 60 次/小时的 IP 配额；认证请求 5000 次/小时。仅读取公开数据，所以 fine-grained token（只读公开仓库）就够了。

优先级（首个非空者生效）：**环境变量 → token 文件（推荐）→ 配置明文字段**。

```powershell
# Windows — 写 token 文件并仅限本人可读
Set-Content -Path "$env:USERPROFILE\.github-token" -Value "github_pat_..." -Encoding ascii -NoNewline
icacls "$env:USERPROFILE\.github-token" /inheritance:r /grant:r "$env:USERNAME:(R)"
```
```bash
# Linux/macOS
(umask 077; printf '%s' "github_pat_..." > ~/.github-token)
```
然后把 `"github_token_file"` 指向该文件（绝对路径，或相对脚本目录）。

### 命令一览

| 命令 | 用途 |
|------|------|
| `query <目标> [--kind user\|owner\|repo] [--sort pushed\|created\|updated] [--limit N]` | 一次性查询，不写状态，按时间倒序 |
| `trending [--limit N] [--show-spam]` | **今日新建**仓库，按 star 倒序。自动去重刷量克隆波；`--show-spam` 可查看。供 LLM 分析 |
| `analyze <owner/repo>` | 拉取单仓库元信息+README+近期 release+commit，输出结构化、LLM 友好的文本。不写状态 |
| `add <用户>` / `add owner <用户>` / `add <owner/repo> [--watch ...] [--branch ...]` | 添加监控对象 |
| `remove <用户\|owner/repo>` | 移除监控对象 |
| `list` / `status` | 查看对象 / 查看运行状态（已记录动态、上次运行、ETag 缓存） |
| `enable` / `disable` | 启用/禁用定时监控 |
| `check [--show-empty]` | 执行一次检查并更新状态（cron 调用） |
| `--dry-run` / `--reset-state` / `help` | 工具命令 |

完整配置项见 `github-watch-config.example.json`。

### LLM 集成（trending 与 analyze）

`trending` 和 `analyze` 设计为**给 LLM 喂数据，而非自己调 LLM**——脚本保持零依赖、单一职责（取数+结构化），由下游 agent 做推理。在 Hermes agent 中的推荐流程：

1. `github-watch.py trending --limit 10` → 捕获 stdout
2. 提示 LLM：*"以下是今日 GitHub 最火的新建仓库。挑出 2 个真正有价值的（忽略游戏作弊器/训练器刷量），各给一段一句话点评。"*
3. 对值得深挖的：`github-watch.py analyze <owner/repo>` → 把结构化输出喂回 LLM 做完整简报（项目定位、技术栈、活跃度、近期动向）。

`analyze` 输出用 `## ` 标题分段（`元信息` / `最近 Release` / `最近提交` / `README（节选）`），方便 agent 切分与引用。README 截断在 6000 字符以控制 token 成本。

### 去重机制（让你信任它的静默）

每条动态有稳定 key：release 用 id/tag，commit 用完整 SHA，名下仓库更新用 `name + pushed_at`（时间戳只在真有新 push 时变化，所以每次 push 只通知一次，安静期不扰民），新建仓库用 `name`。key 持久化在 `github-watch-state.json`。**首次运行绝不刷屏**——只记录基线不通知（除非 `notify_first_run: true`）。状态按 LRU 封顶（`state_max_seen`，默认 5000），ETag 缓存每次运行裁剪，保持有界。

### 局限与坦诚的取舍

- **无内置通知渠道。** 输出是 stdout 文本；推送（邮件/Telegram/Discord/…）由 cron 调度器负责。这是插件模型的有意设计——若要开箱即用的多渠道通知器，[iamspido/github-release-monitor](https://github.com/iamspido/github-release-monitor) 很不错。
- **仅公开数据。** 不监控私有仓库/动态。
- **Events API 的坑。** 用户动态查询走 `/events/public`（约 300 条/90 天，非实时）。要回答"X 是否新建了仓库"，请用 owner 目标——它的新建仓库检测才可靠。
- **用户动态里 PR/issue 标题为空。** events API 的 payload 不含标题（只显示动作+编号）。要完整 release/commit 细节请用 `repo` 目标。

## License

MIT — see [LICENSE](LICENSE).
