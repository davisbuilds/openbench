"""Run ONE case against the agent's solver, in an isolated subprocess.

Invoked by run_score.py as::

    python3 run_tier.py <workspace_dir> <common_path> <seed> <n> <mode>

It generates the case input with the checker-owned generator (loaded by
absolute path so the workspace cannot shadow it), imports the agent's
``solver.core.count_smaller_after`` from the workspace, times ONLY that call
with ``time.process_time()`` (CPU time -- stable under machine load and
unrewarding of multi-core tricks), and prints two lines::

    TIME: <cpu_seconds>
    DIGEST: <sha256 hex>

Any import/runtime error is reported as ``ERROR: ...`` with a nonzero exit so
the parent counts the case as failed. The parent enforces a wall-clock kill so
a runaway (e.g. the naive O(n^2) on a huge tier) never blocks grading.
"""
import importlib.util
import sys
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    workspace_dir, common_path, seed, n, mode = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])

    common = load(common_path, "_checker_common")
    values = common.generate(seed, n, mode)

    # Import the agent's solver from the workspace ONLY. Do not let workspace
    # files shadow the checker's own modules: workspace is added last and the
    # checker modules were already loaded by absolute path above.
    sys.path.append(workspace_dir)
    from solver.core import count_smaller_after

    t0 = time.process_time()
    res = count_smaller_after(values)
    elapsed = time.process_time() - t0

    print("TIME: %.6f" % elapsed)
    print("DIGEST: %s" % common.digest(res))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- report any failure to the parent
        print("ERROR: %r" % (exc,))
        sys.exit(1)
