# Hermes cron wrappers

Two thin wrapper scripts for running `github-watch.py` under [Hermes](https://github.com/) cron. They exist to work around two Hermes-specific gotchas; you only need them if you schedule github-watch as a Hermes cron job.

## Why wrappers are needed

### 1. `--script` takes a bare path, no CLI args

Hermes cron's `--script` field resolves the *entire string* as a file path under `~/.hermes/scripts/`. So `--script "github-watch.py trending --limit 10"` fails with `Script not found: …/github-watch.py trending --limit 10` — the args are treated as part of the filename. The wrapper invokes github-watch.py with the desired args itself, and cron points at the wrapper.

### 2. Windows GBK codepage crash

Hermes' script runner captures child stdout with `subprocess.run(text=True)` and **no explicit encoding**, so on a Chinese Windows it decodes as cp936/GBK. `github-watch.py` emits UTF-8 (CJK + symbols like ★/⚠), which GBK cannot decode → `UnicodeDecodeError` in the capture thread → silently empty output → the job reports "no output" and skips delivery. The wrappers emit in the system ANSI codepage (`GetACP()`), replacing unrepresentable chars, so CJK survives.

On Linux/macOS (UTF-8 locale) the wrappers are lossless and harmless.

## Files

- `github-trending-cron.py` — runs `github-watch.py trending --limit 10`. Pair with an **LLM** cron job (`no_agent=False`, the default): script stdout is injected into the agent's prompt so it can pick the genuinely interesting repos and write a digest.
- `github-watch-check-cron.py` — runs `github-watch.py check` (stateful monitor tick). Pair with a **no-agent** cron job (`--no-agent`): script stdout is delivered verbatim. Empty stdout = nothing new = silent tick (the watchdog pattern).

Both surface failures instead of going silent: if github-watch.py exits non-zero, the wrapper prints the error on stdout so you see it, with one retry for transient API blips.

## Install

Copy both wrappers next to `github-watch.py` under `~/.hermes/scripts/`:

```bash
cp examples/hermes-cron/*.py ~/.hermes/scripts/
```

## Schedule

```bash
# Daily trending digest at 09:00, LLM-picked, fanned out to all channels
hermes cron create "0 9 * * *" \
  --name github-trending-daily \
  --script github-trending-cron.py \
  --deliver all \
  "Below is today's trending new GitHub repos (script output). Pick 2-3 genuinely valuable ones (ignore game-cheat/trainer spam) and write a short Chinese note for each."

# Stateful monitor every 2h, delivered verbatim (no LLM), to a specific group
hermes cron create "0 */2 * * *" \
  --name github-watch-check \
  --script github-watch-check-cron.py \
  --no-agent \
  --deliver "qqbot:<your-group-channel-id>"
```

`cron_mode` must be `allow` (or `manual`) in `config.yaml` under `approvals:` for jobs to fire.

## Finding your channel id

`deliver=all` fans out to every connected channel. To target one specific chat, find its channel id: send any message to that chat from the target platform, then grep the latest `logs/gateway.log` for `inbound message: platform=… chat=<ID>`. Use `platform:<id>` as `--deliver`.
