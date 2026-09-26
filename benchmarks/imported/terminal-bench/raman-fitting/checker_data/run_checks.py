#!/usr/bin/env python3
"""Stdlib checker for raman-fitting."""
import hashlib
import json
import math
import os
import pathlib
import sys

TASK_DIR = pathlib.Path(os.environ["TASK_DIR"])
DATA = TASK_DIR / "checker_data"
CWD = pathlib.Path.cwd()


def fail(message):
    print(f"FAIL: {message}")
    return 1


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def finite_number(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} is not numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} is not finite")
    return value


def main():
    spectrum = CWD / "graphene.dat"
    if not spectrum.is_file():
        return fail("graphene.dat is missing")
    expected_hashes = json.loads((DATA / "input_hashes.json").read_text())
    if sha256(spectrum) != expected_hashes["graphene.dat"]:
        return fail("graphene.dat hash does not match the original spectrum")

    result_path = CWD / "results.json"
    if not result_path.exists():
        return fail("results.json does not exist")
    try:
        actual = json.loads(result_path.read_text())
    except Exception as exc:
        return fail(f"results.json is not valid JSON: {exc}")
    if not isinstance(actual, dict):
        return fail("results.json must contain a JSON object")

    oracle = json.loads((DATA / "expected_params.json").read_text())
    tol = oracle["tolerances"]
    for peak in ("G", "2D"):
        if peak not in actual or not isinstance(actual[peak], dict):
            return fail(f"missing object for peak {peak}")
        for field in ("x0", "gamma", "amplitude", "offset"):
            if field not in actual[peak]:
                return fail(f"missing {peak}.{field}")
        try:
            x0 = finite_number(actual[peak]["x0"], f"{peak}.x0")
            gamma = finite_number(actual[peak]["gamma"], f"{peak}.gamma")
            amp = finite_number(actual[peak]["amplitude"], f"{peak}.amplitude")
            offset = finite_number(actual[peak]["offset"], f"{peak}.offset")
        except (TypeError, ValueError) as exc:
            return fail(str(exc))

        exp = oracle[peak]
        failures = []
        if abs(x0 - exp["x0"]) >= tol["x0_abs"]:
            failures.append("x0")
        if abs(gamma - exp["gamma"]) >= tol["gamma_abs"]:
            failures.append("gamma")
        if abs(1 - amp / exp["amplitude"]) >= tol["amplitude_rel"]:
            failures.append("amplitude")
        if abs(1 - offset / exp["offset"]) >= tol["offset_rel"]:
            failures.append("offset")
        if failures:
            return fail(f"{peak} parameters outside tolerance: {', '.join(failures)}")

    print("PASS: results.json matches checker-owned Raman fit parameters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
