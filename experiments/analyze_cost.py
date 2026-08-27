#!/usr/bin/env python3
"""Derive per-arm USD cost for matrix result sets, preferring captured cost.

Proxy-routed rows now carry an authoritative ``cost_usd`` captured at request
time (OpenRouter's own ``usage.cost``, or the LiteLLM bridge's
``x-litellm-response-cost`` header -- see ``obench/proxy.extract_cost``). This
script uses that figure whenever a row has one (``effective_row_cost``), and
falls back to deriving cost from the row's vendor-reported token split
(``token_basis`` == "vendor_split": uncached input, cache-read, cache-write,
output, reasoning) x a per-token price sheet only when it doesn't -- which is
always the case for codex-native arms, since they run on a ChatGPT
subscription and never touch the proxy or a metered endpoint.

Two price regimes for the price-sheet fallback, kept honest:

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


def effective_row_cost(row: dict, price: Optional[dict]) -> Optional[float]:
    """One row's USD cost: captured (authoritative) cost wins over derivation.

    Proxy-routed arms now carry a real per-call ``cost_usd`` captured at
    request time (OpenRouter's own ``usage.cost``, or the LiteLLM bridge's
    ``x-litellm-response-cost`` header) -- that figure is preferred whenever
    present, since it is what the vendor actually billed rather than a
    token-count x price-sheet estimate. Falls back to ``row_cost`` (and its
    price-sheet assumptions) only when the row has no captured cost, which is
    the normal case for codex-native subscription arms that never touch the
    proxy. Returns ``None`` (never a fabricated number) when neither a
    captured cost nor a price-sheet entry is available.
    """
    captured = row.get("cost_usd")
    if isinstance(captured, (int, float)) and not isinstance(captured, bool):
        return float(captured)
    if price is None:
        return None
    return row_cost(row, price)


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
            price = PRICES.get(model)
            price_dict = price if isinstance(price, dict) else None
            row_costs = [effective_row_cost(r, price_dict) for r in mr]
            captured = sum(
                1 for r in mr
                if isinstance(r.get("cost_usd"), (int, float)) and not isinstance(r.get("cost_usd"), bool)
            )
            if all(v is None for v in row_costs):
                # No captured cost on any row, and no price-sheet entry to
                # derive from -- genuinely unknown, never fabricated.
                cost_str = "UNKNOWN"
                unknown_models.add(model)
            else:
                c = sum(v for v in row_costs if v is not None)
                set_total += c
                grand_known += c
                cost_str = f"${c:,.4f}"
                if any(v is None for v in row_costs):
                    cost_str += "*"  # partial: some cells neither captured nor priced
                elif captured:
                    cost_str += f" ({captured} captured)"
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
    print("NOTE: '(N captured)' means N of that model's cells used the row's own "
          "authoritative cost_usd (captured at request time) instead of price-sheet "
          "derivation; '*' means some cells had neither a captured cost nor a price.")
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

    # A row's captured (authoritative) cost_usd wins over price-sheet
    # derivation, even when the derived figure would differ.
    captured_row = {"tokens_input_uncached": 1000, "tokens_output": 200, "cost_usd": 0.0042}
    assert effective_row_cost(captured_row, price) == 0.0042
    # cost_usd == 0.0 is a legitimate captured value (e.g. a free-tier
    # route) and must not be treated as falsy/absent.
    free_row = {"tokens_input_uncached": 1000, "tokens_output": 200, "cost_usd": 0.0}
    assert effective_row_cost(free_row, price) == 0.0
    # Absent cost_usd falls back to price-sheet derivation.
    derived_row = {"tokens_input_uncached": 1000, "tokens_output": 200}
    assert abs(effective_row_cost(derived_row, price) - row_cost(derived_row, price)) < 1e-12
    # No captured cost and no price-sheet entry -> None (UNKNOWN), never fabricated.
    assert effective_row_cost(derived_row, None) is None

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
