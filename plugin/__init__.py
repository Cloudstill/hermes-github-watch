"""Hermes plugin for /github watch management."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home


USAGE = """GitHub 监控命令（输入 /github help 查看完整说明）：

查询（不改配置、不写状态，结果按时间倒序）：
  /github query <用户名|owner/repo>            查询最新公开动态
  /github query <用户名> --kind owner --sort created   列出某用户名下仓库（按创建时间）
  /github query <用户名> --limit 20            指定展示最新 N 条
  /github get <用户名> [--limit N]             获取公开仓库概况、Star、Fork 和最后更新时间
  /github trending [--limit N] [--show-spam]   今日新建热门仓库（按 star 倒序，默认隐藏刷量，供 LLM 分析）
  /github analyze <owner/repo 或 GitHub URL>   拉取单仓库元信息/README/release/commit（供 LLM 分析）
  /github list                                 查看已配置监控对象
  /github status                               查看监控状态（含已记录动态数、上次运行时间）

管理（修改配置）：
  /github add <用户名>                          添加用户动态监控
  /github add owner <用户名>                    添加名下仓库监控（含新建仓库提醒）
  /github add <owner/repo> [--watch releases,commits] [--branch main]   添加仓库监控
  /github remove <用户名|owner/repo>            移除监控对象
  /github enable                               启用定时监控
  /github disable                              禁用定时监控

运行：
  /github check [--show-empty]                 立即执行一次监控并更新状态
  /github reset-state                          清空已记录状态
  /github help                                 查看完整帮助
"""


def _script_path() -> Path:
    return get_hermes_home() / "scripts" / "github-watch.py"


def _handle_github(raw_args: str) -> str:
    text = (raw_args or "").strip()
    if not text or text in {"help", "-h", "--help"}:
        return USAGE.strip()

    try:
        args = shlex.split(text)
    except ValueError as exc:
        return f"参数解析失败: {exc}"

    if not args:
        return USAGE.strip()
    if args[0] in {"reset-state", "reset"}:
        args = ["--reset-state"]

    script = _script_path()
    if not script.exists():
        return f"找不到脚本: {script}"

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=28,
        )
    except subprocess.TimeoutExpired:
        return "GitHub 命令超时。"
    except Exception as exc:
        return f"GitHub 命令失败: {exc}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    output = stdout or stderr
    if proc.returncode != 0:
        return output or f"GitHub 命令失败，退出码 {proc.returncode}。"
    return output or "没有输出。"


def register(ctx) -> None:
    ctx.register_command(
        "github",
        handler=_handle_github,
        description="Manage GitHub watch targets and query public GitHub activity.",
        args_hint="add|query|list|remove|status ...",
    )
