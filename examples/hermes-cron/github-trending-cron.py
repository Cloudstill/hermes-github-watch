#!/usr/bin/env python3
"""Cron wrapper: fetch today's trending GitHub repos for the LLM to digest.

Hermes cron's `--script` field takes a bare script path (no CLI args), so this
wrapper invokes github-watch.py with the desired trending args and forwards
stdout. Empty stdout (nothing new / all spam hidden) means a silent tick.

Failure handling: if github-watch.py exits non-zero, we surface its stderr on
stdout (prefixed) so the failure is visible to the LLM/user instead of being
silently swallowed as "no output". A single retry absorbs transient API blips.

Encoding: Hermes' script runner reads child stdout with `text=True` and no
explicit encoding, so on Windows it decodes with the system codepage (GBK).
We therefore emit using the system ANSI codepage, replacing any chars it can't
represent (★/⚠ etc. become '?') so CJK survives instead of crashing the capture.
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
        [sys.executable, str(SCRIPT), "trending", "--limit", "10"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


def _emit(text: str) -> None:
    """Write text to stdout using the system ANSI codepage (GBK on CN Windows).

    Hermes decodes child stdout with the locale codepage and no explicit
    encoding, so we must match it. Unrepresentable chars are replaced.
    """
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
    # One retry on failure to absorb transient API/network blips.
    if rc != 0:
        rc, out, err = _run_once()

    if out:
        _emit(out)
        return 0
    if rc != 0 or err:
        # Surface the failure instead of going silent ("no output" is
        # indistinguishable from "nothing new" and hides broken runs).
        _emit(f"[github trending 采集失败] rc={rc}\n{err or '(无 stderr 输出)'}")
        return rc or 1
    # Genuinely empty (no new repos / all spam hidden) -> silent tick.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
