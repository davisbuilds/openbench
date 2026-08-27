#!/usr/bin/env python3
"""Derive theoretical API cost for matrix result sets from captured token usage.

Every bench row records a vendor-reported token split (``token_basis`` ==
"vendor_split"): uncached input, cache-read, cache-write, output, and reasoning
tokens. This script multiplies those tiers by a per-token price sheet to produce
a per-arm dollar figure -- the only way to attach a cost to runs that were paid
by subscription (codex-native arms never touch a metered endpoint) or that
recorded tokens but not cost (every arm, today).

Two price regimes, kept honest:

* OpenRouter-routed arms (glm / deepseek / minimax / ox-alpha) -- authoritative
  per-token prices are published at ``/api/v1/models``. A snapshot fetched
  2026-08-27 is embedded below; ``--refresh-prices`` re-pulls it live. Routing
  (which OpenRouter slug, paid vs :free) is taken from ``obench/bridge/config.yaml``,
  NOT the stale registry comments in ``adapters/codex.py``.
* codex-native arms (gpt-5.6-*) -- these run on a ChatGPT subscription and never
  hit a metered API, so there is no external cost to fetch and no price I can
  assert. They are priced ``None`` -> reported as UNKNOWN, never fabricated.
  Supply real per-token rates via ``--codex-prices prices.json`` to fill them.

Cost per row (reasoning billed at the output rate, the vendor convention):

    cost = in_uncached*p_in + cache_read*p_cache_read
         + cache_write*p_cache_write + (output + reasoning)*p_out

Note: results.jsonl holds only the final saved row per cell; throttled/retried
attempts that still burned tokens are overwritten, so this UNDER-counts real
spend. Treat it as a floor, cross-checkable against the provider dashboard.

Usage:
    python experiments/analyze_cost.py                     # all results/*/
    python experiments/analyze_cost.py results/terra-luna-daily-drivers
    python experiments/analyze_cost.py --refresh-prices    # re-pull OR prices
    python experiments/analyze_cost.py --codex-prices oai.json
    python experiments/analyze_cost.py --selftest
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.request
from collections import defaultdict
from typing import Optional

# --- Price sheet -----------------------------------------------------------
# Per-TOKEN USD prices. Keys are the bench `model` field (as it appears in rows).
# OpenRouter snapshot fetched 2026-08-27 from https://openrouter.ai/api/v1/models
# (prompt -> p_in, completion -> p_out, input_cache_read -> p_cache_read).
# p_cache_write falls back to p_in when the endpoint omits it; our rows carry
# cache_write == 0 so it is inert here regardless.
#
# Routing/tier is per obench/bridge/config.yaml:
#   glm-5.3-flash            -> openrouter/z-ai/glm-5.3-flash            (PAID)
#   deepseek-v4-flash-0731   -> openrouter/deepseek/deepseek-v4-flash-0731 (PAID)
#   minimax-m3               -> openrouter/minimax/minimax-m3            (PAID, not :free)
#   ox-alpha                 -> openrouter/stealth/ox-alpha             (FREE preview, retired)
PRICES: dict[str, Optional[dict]] = {
    "glm-5.3-flash":          {"in": 7.5e-8, "cache_read": 1.5e-8, "cache_write": 7.5e-8, "out": 2.5e-7, "src": "openrouter:z-ai/glm-5.3-flash@2026-08-27"},
    "deepseek-v4-flash-0731": {"in": 6.0e-8, "cache_read": 1.2e-8, "cache_write": 6.0e-8, "out": 1.2e-7, "src": "openrouter:deepseek/deepseek-v4-flash-0731@2026-08-27"},
    "minimax-m3":             {"in": 3.0e-7, "cache_read": 6.0e-8, "cache_write": 3.0e-7, "out": 1.2e-6, "src": "openrouter:minimax/minimax-m3@2026-08-27"},
    "ox-alpha":               {"in": 0.0,    "cache_read": 0.0,    "cache_write": 0.0,    "out": 0.0,    "src": "openrouter:stealth/ox-alpha (free preview)"},
    # codex-native arms ran on a ChatGPT subscription (flat fee), but OpenRouter
    # publishes OpenAI's per-token API list price for the SAME base models, so
    # these are the authoritative "what it would have cost on the metered API"
    # figures. The -xhigh/-max suffixes are reasoning-effort settings, not price
    # tiers -- price is per base model (codex ran `-m gpt-5.6-terra`/`-m gpt-5.6-luna`).
    "gpt-5.6-terra-xhigh":    {"in": 2.0e-6, "cache_read": 2.0e-7, "cache_write": 2.0e-6, "out": 1.2e-5, "src": "openrouter:openai/gpt-5.6-terra@2026-08-27 (theoretical API list price)"},
    "gpt-5.6-luna-max":       {"in": 2.0e-7, "cache_read": 2.0e-8, "cache_write": 2.0e-7, "out": 1.2e-6, "src": "openrouter:openai/gpt-5.6-luna@2026-08-27 (theoretical API list price)"},
}

# bench model -> OpenRouter model id, for --refresh-prices (and their tier).
OPENROUTER_SLUG = {
    "glm-5.3-flash":          "z-ai/glm-5.3-flash",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "minimax-m3":             "minimax/minimax-m3",
    # codex-native: OpenRouter lists OpenAI's list price for the same base models.
    "gpt-5.6-terra-xhigh":    "openai/gpt-5.6-terra",
    "gpt-5.6-luna-max":       "openai/gpt-5.6-luna",
    # ox-alpha stealth route is retired/unlisted -> stays free (0).
}


def row_cost(row: dict, price: dict) -> float:
    """Theoretical USD cost of one cell from its token tiers and a price dict."""
    in_uncached = row.get("tokens_input_uncached") or 0
    cache_read = row.get("tokens_cache_read") or 0
    cache_write = row.get("tokens_cache_write") or 0
    out = row.get("tokens_output") or 0
    reasoning = row.get("tokens_reasoning") or 0
    return (
        in_uncached * price["in"]
        + cache_read * price["cache_read"]
        + cache_write * price["cache_write"]
        + (out + reasoning) * price["out"]
    )


def refresh_openrouter_prices(timeout: int = 20) -> dict:
    """Pull live per-token prices from OpenRouter, overlaying PRICES in place."""
    url = "https://openrouter.ai/api/v1/models"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec - public endpoint
        data = json.load(resp)
    by_id = {m["id"]: m for m in data.get("data", [])}
    updated = {}
    for bench_id, or_id in OPENROUTER_SLUG.items():
        m = by_id.get(or_id)
        if not m:
            continue
        p = m.get("pricing", {})
        try:
            p_in = float(p["prompt"])
            p_out = float(p["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        p_cr = p.get("input_cache_read")
        p_cw = p.get("input_cache_write")
        PRICES[bench_id] = {
            "in": p_in,
            "cache_read": float(p_cr) if p_cr not in (None, "") else p_in,
            "cache_write": float(p_cw) if p_cw not in (None, "") else p_in,
            "out": p_out,
            "src": f"openrouter:{or_id}@live",
        }
        updated[bench_id] = or_id
    return updated


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def analyze(paths: list[str]) -> int:
    grand_known = 0.0
    unknown_models: set[str] = set()
    any_rows = False
    for results_path in paths:
        try:
            rows = load_rows(results_path)
        except FileNotFoundError:
            continue
        if not rows:
            continue
        any_rows = True
        name = os.path.basename(os.path.dirname(results_path)) or results_path
        by_model: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_model[r.get("model", "?")].append(r)

        print(f"\n### {name}  ({len(rows)} cells)")
        print(f"    {'model':<26} {'cells':>5} {'in_unc':>9} {'cache_rd':>9} {'output':>8} {'reason':>7}  {'cost':>12}")
        set_total = 0.0
        for model, mr in sorted(by_model.items()):
            in_unc = sum(r.get("tokens_input_uncached") or 0 for r in mr)
            cache_rd = sum(r.get("tokens_cache_read") or 0 for r in mr)
            out = sum(r.get("tokens_output") or 0 for r in mr)
            reason = sum(r.get("tokens_reasoning") or 0 for r in mr)
            price = PRICES.get(model, "MISSING")
            if price is None or price == "MISSING":
                cost_str = "UNKNOWN"
                unknown_models.add(model)
            else:
                c = sum(row_cost(r, price) for r in mr)
                set_total += c
                grand_known += c
                cost_str = f"${c:,.4f}"
            print(f"    {model:<26} {len(mr):>5} {in_unc:>9,} {cache_rd:>9,} {out:>8,} {reason:>7,}  {cost_str:>12}")
        print(f"    {'-'*84}")
        print(f"    set known-priced subtotal: ${set_total:,.4f}")

    if not any_rows:
        print("No result rows found.", file=sys.stderr)
        return 1
    print(f"\n=== GRAND TOTAL (known-priced arms): ${grand_known:,.4f} ===")
    if unknown_models:
        print(f"UNKNOWN-priced (subscription / no rate set): {', '.join(sorted(unknown_models))}")
        print("  -> supply per-token rates via --codex-prices to include these.")
    print("NOTE: results.jsonl keeps only the final saved row per cell, so retried/"
          "throttled attempts are not counted -- this is a FLOOR on real spend.")
    return 0


def _selftest() -> int:
    # Known tiers x known prices -> hand-computed expectation.
    price = {"in": 1e-6, "cache_read": 1e-7, "cache_write": 5e-7, "out": 2e-6}
    row = {"tokens_input_uncached": 1000, "tokens_cache_read": 5000,
           "tokens_cache_write": 0, "tokens_output": 200, "tokens_reasoning": 50}
    got = row_cost(row, price)
    exp = 1000*1e-6 + 5000*1e-7 + 0*5e-7 + (200+50)*2e-6
    assert abs(got - exp) < 1e-12, (got, exp)
    # None-token fields must coerce to 0, not crash.
    assert row_cost({"tokens_output": None}, price) == 0.0
    # reasoning is billed at the output rate.
    r2 = {"tokens_output": 0, "tokens_reasoning": 100}
    assert abs(row_cost(r2, price) - 100*2e-6) < 1e-12
    print("selftest OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="result dirs or results.jsonl files (default: results/*/)")
    ap.add_argument("--refresh-prices", action="store_true", help="re-pull OpenRouter per-token prices live")
    ap.add_argument("--codex-prices", metavar="JSON", help="JSON {model: {in,cache_read,cache_write,out}} for subscription arms")
    ap.add_argument("--selftest", action="store_true", help="run the cost-formula self-check and exit")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.refresh_prices:
        try:
            upd = refresh_openrouter_prices()
            print(f"refreshed OpenRouter prices: {upd}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - best-effort refresh
            print(f"price refresh failed ({e}); using embedded snapshot", file=sys.stderr)

    if args.codex_prices:
        with open(args.codex_prices, encoding="utf-8") as fh:
            overlay = json.load(fh)
        for model, p in overlay.items():
            PRICES[model] = {**p, "src": f"user:{args.codex_prices}"}

    # Resolve inputs to results.jsonl file paths (recursive: catches nested
    # sets like results/gate/solo/).
    inputs = args.paths or sorted(os.path.dirname(p) for p in glob.glob("results/**/results.jsonl", recursive=True))
    resolved = []
    for p in inputs:
        if os.path.isdir(p):
            resolved.append(os.path.join(p, "results.jsonl"))
        elif p.endswith(".jsonl"):
            resolved.append(p)
        else:
            cand = os.path.join("results", p, "results.jsonl")
            resolved.append(cand if os.path.exists(cand) else p)
    return analyze(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
