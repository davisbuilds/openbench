"""Score three independent buckets; absent/skipped tests never count as passing."""
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE / "tests.json").read_text())
passed = 0
for group, names in SPEC.items():
    try:
        proc = subprocess.run([sys.executable, "-I", str(HERE / "oracle.py"), group],
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(group + ": FAIL (timeout)")
        continue
    print(proc.stderr, file=sys.stderr, end="")
    reports = [line.removeprefix("ORACLE_RESULT:") for line in proc.stdout.splitlines()
               if line.startswith("ORACLE_RESULT:")]
    try:
        report = json.loads(reports[0]) if len(reports) == 1 else {}
        ok = (bool(names) and len(names) == 4 and proc.returncode == 0
              and report.get("count") == len(names) and report.get("skipped") == 0
              and report.get("success") is True
              and sorted(report.get("executed", [])) == sorted(names))
    except (ValueError, TypeError):
        ok = False
    passed += bool(ok)
    print(group + ": " + ("PASS" if ok else "FAIL"))
print("SCORE: %.4f" % (passed / 3))
raise SystemExit(0 if passed == 3 else 1)
