"""Stand-in for the handoff checker. FAKE_CHECKER_MODE picks the behavior."""
import os
import sys

mode = os.environ.get("FAKE_CHECKER_MODE", "exit0")
err = sys.stderr.buffer

if mode == "exit0":
    print("OK")
    sys.exit(0)
if mode == "exit1":
    print("[FAIL] 1 sample finding(s):")
    print("  - sample finding")
    sys.exit(1)
if mode == "crash1":  # an uncaught exception also exits 1, with no [FAIL] line
    err.write("Traceback (most recent call last):\nRuntimeError: fake checker bug\n".encode("utf-8"))
    sys.exit(1)
if mode == "argv":
    print("[PASS] argv=" + " ".join(sys.argv[1:]))
    sys.exit(0)
if mode == "env":  # report the caller marker the parent set on this subprocess
    print("[PASS] caller=" + os.environ.get("HANDOFF_CALLER", "unset"))
    sys.exit(0)
if mode == "sizewarn":  # the size warning, unindented like the real checker prints it
    print("[PASS] all checks passed")
    print("WARNING: handoff file is 20 KiB (over 16 KiB)")
    sys.exit(0)
if mode == "sizewarn_cap":  # warning plus exactly CHECK_CAP result lines
    print("WARNING: handoff file is 20 KiB (over 16 KiB)")
    print("[FAIL] 8 finding(s):")
    for i in range(7):
        print(f"  - finding {i}")
    sys.exit(1)
if mode == "exit2":
    err.write("fake checker crashed\n".encode("utf-8"))
    sys.exit(2)
if mode == "lines":
    for i in range(100):
        err.write(f"LINE-{i:03d} noise\n".encode("utf-8"))
    sys.exit(2)
if mode == "chars":
    for _ in range(5):
        err.write(("ζ" * 1000 + "\n").encode("utf-8"))
    sys.exit(2)
sys.exit(3)
