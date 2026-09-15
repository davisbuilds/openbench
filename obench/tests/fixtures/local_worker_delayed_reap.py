"""Linux descendant cleanup probe with an explicitly delayed orphan reaper.

The isolated process adopts its worker's orphaned child, as init normally would.
This controls the gap between process exit and reaping without changing the real
supervisor, signals, worker, or group-existence detector.
"""

import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from obench import run

PR_SET_CHILD_SUBREAPER = 36


def process_state(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]), "state": fields[0]}


def main(reap_delay):
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER affects only this disposable fixture process.
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        adapters = root / "adapters"
        adapters.mkdir()
        pidfile = root / "child.pid"
        expected = {"completed": True, "tokens": 17}
        (adapters / "pi.py").write_text(
            "import pathlib, subprocess\n"
            "def run(*args):\n"
            "    child = subprocess.Popen(['sleep', '30'])\n"
            f"    pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
            f"    return {expected!r}\n"
        )
        observations = []
        reaper_errors = []
        cleanup_returned = threading.Event()

        def reap():
            try:
                deadline = time.monotonic() + 5
                pid = None
                while time.monotonic() < deadline:
                    if pid is None:
                        try:
                            pid = int(pidfile.read_text())
                        except (FileNotFoundError, ValueError):
                            time.sleep(0.001)
                            continue
                    found = process_state(pid)
                    if not observations:
                        observations.append(found)
                    if found["ppid"] == os.getpid() and found["state"] == "Z":
                        observations.append(found)
                        if reap_delay is None:
                            # Refusal control: keep the zombie until the
                            # bounded cleanup has returned, without a timing race.
                            if not cleanup_returned.wait(timeout=5):
                                raise RuntimeError("cleanup did not respect its bound")
                        else:
                            # Model an init/reaper scheduling gap after exit.
                            time.sleep(reap_delay)
                        os.waitpid(pid, 0)
                        observations.append({"pid": pid, "reaped": True})
                        return
                    time.sleep(0.001)
                raise RuntimeError("descendant never became an adopted zombie")
            except BaseException as exc:
                reaper_errors.append(repr(exc))

        thread = threading.Thread(target=reap)
        thread.start()
        started = time.monotonic()
        error = None
        result = None
        try:
            result = run._run_local_adapter_supervised(
                "pi", "finish", temp, "fixture", 5, str(adapters), None, {},
                {"activate": lambda: None})
        except RuntimeError as exc:
            error = str(exc)
        elapsed = time.monotonic() - started
        try:
            state_at_return = process_state(int(pidfile.read_text()))
        except FileNotFoundError:
            state_at_return = None
        cleanup_returned.set()
        thread.join(timeout=6)
        assert not thread.is_alive(), "reaper thread did not finish"
        assert not reaper_errors, reaper_errors
        assert observations[-1].get("reaped") is True, observations
        print(json.dumps({"result": result, "error": error, "elapsed_s": elapsed,
                          "states": observations, "state_at_return": state_at_return}), flush=True)
        if error:
            return 1
        assert result == expected
        assert state_at_return is None, "supervisor returned before descendant was reaped"
        return 0


if __name__ == "__main__":
    delay = None if sys.argv[1:] == ["hold"] else 0.08
    raise SystemExit(main(delay))
