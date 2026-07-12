#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'schemelike-metacircular-eval' test.

cwd = a fresh copy of the workspace; the agent's deliverable is eval.scm (a
metacircular evaluator). The ONLY workspace artifact the checker uses is
eval.scm: the reference interpreter (interp_ref.py) and every test program are
CHECKER-OWNED copies under checker_data/ (read-only task dir), so doctoring the
workspace interp.py or test/ programs cannot influence grading. (The workspace
still ships interp.py + test/ + shadow_test/ for the agent's own iteration.)

For every checker-owned test program we compare:
  (a) output of  python3 interp_ref.py <prog>                      (ground truth)
  (b) output of  python3 interp_ref.py eval.scm  with stdin=<prog> (via eval.scm)
and, for a few programs, also
  (c) output of eval.scm interpreting eval.scm interpreting <prog> (self-host).

All must match (after stripping a trailing "True" line that eval.scm emits).
Exit 0 iff every program passes. This mirrors tests/test_outputs.py without pytest.
"""
import atexit
import os
import stat
import subprocess
import sys
import tempfile

CHECKER_DATA = os.path.join(os.environ["TASK_DIR"], "checker_data")
INTERP_REF = os.path.join(CHECKER_DATA, "interp_ref.py")

DIRECT_TIMEOUT = 60
EVAL_TIMEOUT = 90
META_PROGRAMS = ("05-simple", "calculator.scm", "closures.scm")
MAX_EVAL_BYTES = 2 * 1024 * 1024


def run_direct(prog, input_data):
    try:
        r = subprocess.run(["python3", INTERP_REF, prog], capture_output=True,
                           text=True, input=input_data, timeout=DIRECT_TIMEOUT)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1


def run_through_eval(prog, input_data, selfhost_source=None):
    eval_input = f"{selfhost_source}\n" if selfhost_source else ""
    eval_input += f"{prog}\n"
    if input_data:
        eval_input += input_data
    try:
        r = subprocess.run(["python3", INTERP_REF, "eval.scm"], capture_output=True,
                           text=True, input=eval_input, timeout=EVAL_TIMEOUT)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1


def normalize(output):
    output = output.strip()
    lines = output.split("\n")
    if lines and lines[-1] == "True":
        lines = lines[:-1]
        output = "\n".join(lines)
    return output


def detect_input(prog):
    try:
        with open(prog, "r") as f:
            content = f.read()
    except OSError:
        return None
    if "(read)" in content:
        low = prog.lower()
        if "test_read" in low:
            return "hello\n"
        if "calculator" in low:
            return "(+ 100 23)\n"
        if "interactive-io" in low:
            return "100\n23\n+\n"
        return None
    return None


def find_test_files():
    files = []
    for d in ("test", "shadow_test"):
        full = os.path.join(CHECKER_DATA, d)
        if os.path.isdir(full):
            for name in sorted(os.listdir(full)):
                if name.endswith(".scm"):
                    files.append(os.path.join(full, name))
    return sorted(files)


def copy_eval_for_selfhost():
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open("eval.scm", flags)
    except OSError as exc:
        print(f"FAIL: could not open eval.scm safely: {exc}")
        sys.exit(1)

    selfhost_path = None
    try:
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            print("FAIL: eval.scm must be a regular file")
            sys.exit(1)
        if st.st_size > MAX_EVAL_BYTES:
            print(f"FAIL: eval.scm is too large ({st.st_size} bytes)")
            sys.exit(1)

        dst_fd, selfhost_path = tempfile.mkstemp(prefix="selfhost_", suffix=".scm", dir=os.getcwd())
        try:
            total = 0
            while True:
                chunk = os.read(src_fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_EVAL_BYTES:
                    print(f"FAIL: eval.scm is too large (>{MAX_EVAL_BYTES} bytes)")
                    sys.exit(1)
                written = 0
                while written < len(chunk):
                    written += os.write(dst_fd, chunk[written:])
            os.fchmod(dst_fd, 0o444)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)

    atexit.register(lambda: os.path.exists(selfhost_path) and os.unlink(selfhost_path))
    return os.path.basename(selfhost_path)


def main():
    if not os.path.exists("eval.scm"):
        print("FAIL: eval.scm does not exist")
        sys.exit(1)

    progs = find_test_files()
    if not progs:
        print("FAIL: no test programs found")
        sys.exit(1)

    # Harden the self-hosting path against solutions that special-case the
    # literal deliverable filename (eval.scm) and transparently pass through
    # instead of interpreting themselves.  The outer interpreter still starts
    # from eval.scm, but the nested evaluator is a randomized copy, so checks
    # like (eq? source-name 'eval.scm) no longer identify the self-host case.
    selfhost_source = copy_eval_for_selfhost()

    passed = 0
    for prog in progs:
        inp = detect_input(prog)
        d_out, d_err, d_code = run_direct(prog, inp)
        if d_err == "TIMEOUT":
            print(f"FAIL: direct interp.py timed out on {prog}")
            sys.exit(1)
        if d_code not in (0, -1):
            print(f"FAIL: direct interp.py failed ({d_code}) on {prog}: {d_err[-300:]}")
            sys.exit(1)

        e_out, e_err, _ = run_through_eval(prog, inp)
        if e_err == "TIMEOUT":
            print(f"FAIL: eval.scm timed out on {prog}")
            sys.exit(1)

        if normalize(d_out) != normalize(e_out):
            print(f"FAIL: {prog}: eval.scm output != interp.py output")
            print(f"  direct:    {normalize(d_out)!r}")
            print(f"  via eval:  {normalize(e_out)!r}")
            sys.exit(1)

        if any(tag in prog for tag in META_PROGRAMS):
            m_out, m_err, _ = run_through_eval(prog, inp, selfhost_source=selfhost_source)
            if m_err == "TIMEOUT":
                print(f"FAIL: self-hosted eval.scm timed out on {prog}")
                sys.exit(1)
            if normalize(d_out) != normalize(m_out):
                print(f"FAIL: {prog}: self-hosted eval.scm output != interp.py output")
                print(f"  direct:      {normalize(d_out)!r}")
                print(f"  via meta:    {normalize(m_out)!r}")
                sys.exit(1)
        passed += 1

    print(f"PASS: all {passed} scheme programs match via eval.scm")
    sys.exit(0)


if __name__ == "__main__":
    main()
