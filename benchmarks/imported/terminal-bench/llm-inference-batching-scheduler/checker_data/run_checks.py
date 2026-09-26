#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'llm-inference-batching-scheduler' tests.

cwd = a fresh copy of the workspace. Reads the (immutable) request buckets from
./task_file/input_data/ and the agent's plans from ./task_file/output_data/, and
enforces the same checks as the upstream tests/test_outputs.py: output existence,
input-data integrity (anti-cheat SHA256), schema/alignment/shape-cap, per-batch
consistency & feasibility, exact coverage without duplicates, and the absolute
performance thresholds. Uses the upstream cost model (cost_model_for_tests.py,
pure stdlib) shipped alongside this script. Exit 0 iff every check passes.
"""
import hashlib
import json
import os
import sys

HERE = os.path.join(os.environ["TASK_DIR"], "checker_data")
sys.path.insert(0, HERE)
from cost_model_for_tests import HEADS, HIDDEN, CostModel, align  # noqa: E402

IN = "task_file/input_data"
OUT = "task_file/output_data"

EXPECTED_INPUT_HASHES = {
    "requests_bucket_1.jsonl":
        "8b3cf1e2b06074162038064dd2c114abe308acd32cec7d365ee63ee81978f7de",
    "requests_bucket_2.jsonl":
        "e15e2d4c4d55f875c42a6042610d6282f6c0657c39d3e0d0f087268252525cb5",
}

# Thresholds copied verbatim from the upstream test_performance_thresholds.
THRESHOLDS = {
    1: dict(cost=3.0e11, pad_ratio=0.055, p95=2.1e6, seq=2.7e8),
    2: dict(cost=4.8e10, pad_ratio=0.15, p95=2.1e5, seq=3.2e7),
}


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_requests(bucket):
    return load_jsonl(os.path.join(IN, f"requests_bucket_{bucket}.jsonl"))


def load_plan(bucket):
    return load_jsonl(os.path.join(OUT, f"plan_b{bucket}.jsonl"))


def check_outputs_exist():
    for b in (1, 2):
        p = os.path.join(OUT, f"plan_b{b}.jsonl")
        if not os.path.exists(p):
            fail(f"missing required output file: {p}")


def check_input_integrity():
    for name, expected in EXPECTED_INPUT_HASHES.items():
        p = os.path.join(IN, name)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            fail(f"input file {name} was modified (hash mismatch); not allowed")


def check_schema_and_coverage():
    for b in (1, 2):
        reqs = load_requests(b)
        plan = load_plan(b)
        ids_req = {r["request_id"] for r in reqs}
        ids_plan = [p["request_id"] for p in plan]
        if set(ids_plan) != ids_req:
            fail(f"bucket {b}: request_id coverage mismatch "
                 f"(req={len(ids_req)} plan={len(set(ids_plan))})")
        if len(ids_plan) != len(set(ids_plan)):
            fail(f"bucket {b}: duplicate request_ids in plan")
        shapes = set()
        for p in plan:
            sh = p["shape"]
            if sh["seq_align"] % 64 != 0:
                fail(f"bucket {b}: seq_align {sh['seq_align']} not aligned to 64")
            if sh["heads_align"] != HEADS:
                fail(f"bucket {b}: heads_align {sh['heads_align']} != {HEADS}")
            if sh["hidden_align"] != HIDDEN:
                fail(f"bucket {b}: hidden_align {sh['hidden_align']} != {HIDDEN}")
            shapes.add((sh["seq_align"], sh["heads_align"], sh["hidden_align"]))
        if len(shapes) > 8:
            fail(f"bucket {b}: {len(shapes)} shapes exceeds limit of 8")


def check_feasibility_and_batches():
    gran = 64
    for b in (1, 2):
        reqs = load_requests(b)
        req_idx = {r["request_id"]: r for r in reqs}
        plan = load_plan(b)
        for p in plan:
            r = req_idx[p["request_id"]]
            s_aligned = align(r["prompt_len"], gran)
            if p["shape"]["seq_align"] < s_aligned:
                fail(f"bucket {b}: shape seq_align {p['shape']['seq_align']} "
                     f"< aligned prompt {s_aligned} for {p['request_id']}")
        batches = {}
        for p in plan:
            batches.setdefault(p["batch_id"], []).append(p)
        for bid, entries in batches.items():
            sh0 = entries[0]["shape"]
            for p in entries:
                if p["shape"] != sh0:
                    fail(f"bucket {b}: batch {bid} has inconsistent shapes")
            max_s = max(align(req_idx[p["request_id"]]["prompt_len"], gran)
                        for p in entries)
            if sh0["seq_align"] < max_s:
                fail(f"bucket {b}: batch {bid} seq_align {sh0['seq_align']} "
                     f"< max aligned prompt {max_s}")


def plan_metrics(bucket):
    cm = CostModel(granularity=64)
    reqs = load_requests(bucket)
    req_idx = {r["request_id"]: r for r in reqs}
    plan = load_plan(bucket)
    return cm.plan_metrics(req_idx, plan)


def check_performance():
    combined_shapes = set()
    for b in (1, 2):
        m = plan_metrics(b)
        t = THRESHOLDS[b]
        if m["cost"] > t["cost"]:
            fail(f"bucket {b}: cost {m['cost']:.4g} > {t['cost']:.4g}")
        if m["pad_ratio"] > t["pad_ratio"]:
            fail(f"bucket {b}: pad_ratio {m['pad_ratio']:.4g} > {t['pad_ratio']}")
        if m["p95_latency_ms"] > t["p95"]:
            fail(f"bucket {b}: p95 {m['p95_latency_ms']:.4g} > {t['p95']:.4g}")
        if m["sequential_timecost"] > t["seq"]:
            fail(f"bucket {b}: sequential_timecost "
                 f"{m['sequential_timecost']:.4g} > {t['seq']:.4g}")
        for p in load_plan(b):
            sh = p.get("shape", {})
            if isinstance(sh, dict) and sh:
                combined_shapes.add((int(sh["seq_align"]), int(sh["heads_align"]),
                                     int(sh["hidden_align"])))
    if len(combined_shapes) > 8:
        fail(f"combined unique shapes {len(combined_shapes)} exceed cap of 8")


def main():
    check_outputs_exist()
    check_input_integrity()
    check_schema_and_coverage()
    check_feasibility_and_batches()
    check_performance()
    print("PASS: plans satisfy all schema, coverage, and performance checks")
    sys.exit(0)


if __name__ == "__main__":
    main()
