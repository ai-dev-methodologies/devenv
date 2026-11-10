#!/usr/bin/env python3
"""context_budget.py 단위 검사. 임시 HOME·cwd로 경계·부재·기록·형식·슬러그 6건을 확인한다. 인자는 받지 않는다."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="backslashreplace")

TOOL = Path(__file__).resolve().parent / "context_budget.py"
LINE_RE = re.compile(
    r"^context-budget \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \S+ total=\d+ cap=\d+ "
    r"status=(ok|over) files=\d+ missing=\d+$"
)


def slug_of(cwd):
    return str(cwd).replace("/", "-")


def make_env(root, cwd_size, with_local=False):
    home = root / "home"
    cwd = root / "proj"
    (home / ".claude").mkdir(parents=True)
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_bytes(b"x" * cwd_size)
    if with_local:
        (cwd / "CLAUDE.local.md").write_bytes(b"y")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return home, cwd, env


def run(args, env):
    p = subprocess.run(
        [sys.executable, str(TOOL)] + args,
        env=env, capture_output=True, text=True, encoding="utf-8",
    )
    return p.returncode, p.stdout, p.stderr


def case_cap_ok():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 49152)
        rc, out, _ = run(["--cwd", str(cwd)], env)
        return rc == 0 and "total=49152" in out and "status=ok" in out, f"rc={rc} out={out.strip()}"


def case_cap_over():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 49153)
        rc, out, _ = run(["--cwd", str(cwd)], env)
        return rc == 2 and "total=49153" in out and "status=over" in out, f"rc={rc} out={out.strip()}"


def case_missing():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 10, with_local=False)
        rc, out, _ = run(["--cwd", str(cwd), "--table"], env)
        missing_rows = [ln for ln in out.splitlines() if ln.startswith("없음\t")]
        local_missing = any(ln.endswith("CLAUDE.local.md") for ln in missing_rows)
        ok = rc == 0 and local_missing and "10\t합계" in out
        return ok, f"rc={rc} 없음행={len(missing_rows)}"


def case_record_over():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 49153)
        log = home / ".claude" / "logs" / "context-budget.log"
        rc, out, _ = run(["--cwd", str(cwd), "--record"], env)
        run(["--cwd", str(cwd), "--record"], env)
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        ok = rc == 2 and len(lines) == 2 and all(LINE_RE.match(ln) for ln in lines)
        return ok, f"rc={rc} 로그줄={len(lines)}"


def case_format():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 100)
        rc, out, _ = run(["--cwd", str(cwd), "--line"], env)
        return bool(LINE_RE.match(out.strip())) and len(out.strip().splitlines()) == 1, out.strip()


def case_slug_dot_underscore():
    with tempfile.TemporaryDirectory() as t:
        home, cwd, env = make_env(Path(t), 10)
        dotted = Path(t) / "my.proj_x"
        dotted.mkdir()
        resolved = dotted.resolve()
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(resolved))
        mem = home / ".claude" / "projects" / slug / "memory" / "MEMORY.md"
        mem.parent.mkdir(parents=True)
        mem.write_bytes(b"z" * 7)
        rc, out, _ = run(["--cwd", str(dotted), "--table"], env)
        rows = [ln for ln in out.splitlines() if ln.endswith("MEMORY.md")]
        ok = rc == 0 and len(rows) == 1 and rows[0].startswith("7\t")
        return ok, f"rc={rc} 색인행={rows}"


CASES = [
    ("경계 49152 -> 종료코드 0", case_cap_ok),
    ("경계 49153 -> 종료코드 2", case_cap_over),
    ("대상 부재 -> 없음 행·합 제외", case_missing),
    ("--record 종료코드 2에서도 기록", case_record_over),
    ("표 1줄 형식 정규식", case_format),
    (". _ 든 cwd -> 메모리 색인 바이트", case_slug_dot_underscore),
]


def main():
    if len(sys.argv) > 1:
        print("[거부] 인자를 받지 않는다")
        return 2
    if not TOOL.exists():
        print(f"[FAIL] context_budget.py 부재: {TOOL}")
        print(f"요약: {len(CASES)}건 중 0 PASS / {len(CASES)} FAIL")
        return 1
    passed = 0
    for name, fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"예외 {e!r}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
        passed += 1 if ok else 0
    failed = len(CASES) - passed
    print(f"요약: {len(CASES)}건 중 {passed} PASS / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
