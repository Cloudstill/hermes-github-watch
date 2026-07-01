#!/usr/bin/env python3
"""Cron wrapper: run one github-watch check tick (stateful, no_agent-style).

Runs ``github-watch.py check`` and forwards its stdout verbatim. Empty stdout
(no new activity) means a silent tick — nothing is delivered. Non-empty stdout
(the new-activity digest) is forwarded for delivery.

This is the stateful monitor path: it reads/writes github-watch-state.json and
only reports genuinely new items (first run establishes a baseline without
spamming). Pair with a recurring cron schedule (e.g. every 2h).

Encoding mirrors github-trending-cron.py: emit in the system ANSI codepage so
Hermes' `text=True` capture decodes cleanly on Windows.
"""

from __future__ import annotations

import ctypes
import locale
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "github-watch.py"


def _run_once() -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "check"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


def _emit(text: str) -> None:
    enc = locale.getpreferredencoding(False) or "utf-8"
    if sys.platform == "win32":
        try:
            acp = ctypes.windll.kernel32.GetACP()
            enc = f"cp{acp}"
        except Exception:
            pass
    data = text.encode(enc, errors="replace")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        print(data.decode(enc, errors="replace"))


def main() -> int:
    if not SCRIPT.exists():
        _emit("GitHub watch 脚本缺失: github-watch.py")
        return 1
    rc, out, err = _run_once()
    if rc != 0:
        rc, out, err = _run_once()
    if out:
        _emit(out)
        return 0
    if rc != 0 or err:
        _emit(f"[github watch 监控失败] rc={rc}\n{err or '(无 stderr 输出)'}")
        return rc or 1
    # Empty stdout = no new activity = silent tick (intended).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
