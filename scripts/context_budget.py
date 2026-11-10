#!/usr/bin/env python3
"""세션 시작 때 로드되는 지시 파일의 바이트 표를 낸다. 합이 기준 바이트보다 크면 종료코드 2.

대상: ~/.claude/CLAUDE.md · ~/.claude/rules/*.md · <cwd>/CLAUDE.md · <cwd>/CLAUDE.local.md ·
~/.claude/projects/<슬러그>/memory/MEMORY.md (슬러그 = cwd 절대 경로에서 영숫자 밖 글자를 - 로 바꾼 글자).
--record는 종료코드와 무관하게 ~/.claude/logs/context-budget.log에 표 1줄을 덧붙인다.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="backslashreplace")

DEFAULT_CAP = 49152  # 48 * 1024


def home_dir():
    return Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home())


def targets(cwd, home=None):
    home = Path(home) if home is not None else home_dir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    rules = sorted((home / ".claude" / "rules").glob("*.md"), key=lambda p: p.name)
    return (
        [home / ".claude" / "CLAUDE.md"]
        + rules
        + [cwd / "CLAUDE.md", cwd / "CLAUDE.local.md"]
        + [home / ".claude" / "projects" / slug / "memory" / "MEMORY.md"]
    )


def measure(cwd, home=None):
    rows = []
    for p in targets(cwd, home):
        rows.append((p, p.stat().st_size if p.is_file() else None))
    return rows


def build_line(cwd, cap, rows):
    total = sum(s for _, s in rows if s is not None)
    files = sum(1 for _, s in rows if s is not None)
    missing = len(rows) - files
    status = "over" if total > cap else "ok"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"context-budget {ts} {cwd} total={total} cap={cap} "
        f"status={status} files={files} missing={missing}"
    )
    return line, total


def selfcheck_settings():
    path = home_dir() / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for entry in data.get("hooks", {}).get("SessionStart", []):
        for h in entry.get("hooks", []):
            if "context_budget.py" in h.get("command", ""):
                n += 1
    print(f"SessionStart context_budget.py 항목 {n}건")
    return 0 if n == 1 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--line", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--selfcheck-settings", action="store_true")
    a = ap.parse_args()
    if a.selfcheck_settings:
        return selfcheck_settings()
    cwd = Path(a.cwd).resolve() if a.cwd else Path.cwd().resolve()
    rows = measure(cwd)
    line, total = build_line(cwd, a.cap, rows)
    if a.table:
        for p, s in rows:
            print(f"{s}\t{p}" if s is not None else f"없음\t{p}")
        print(f"{total}\t합계 (cap={a.cap})")
    if a.record:
        log = home_dir() / ".claude" / "logs" / "context-budget.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    if not a.table or a.record or a.line:
        print(line)
    return 2 if total > a.cap else 0


if __name__ == "__main__":
    sys.exit(main())
