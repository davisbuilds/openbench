#!/usr/bin/env python3
"""Fetch real per-model context/output limits and pin them to a data file.

Why this exists
---------------
The pi provider config openbench generates REQUIRES a ``contextWindow`` and
``maxTokens`` per model (pi's ``registerProvider`` declares both non-optional).
Those numbers used to be hand-written with a silent fallback of 128000/8192,
and five of seven open models inherited that fallback. It stayed invisible for
weeks because the numbers look plausible.

The real values are far larger. deepseek-v4-flash is 1048576 / 393216, so it ran
with an 8x understated context and a 48x understated output cap. That matters
because pi clamps every request to

    min(maxTokens, max(1, contextWindow - promptTokens - 4096))

(``clampMaxTokensToContext``, MIN_MAX_TOKENS = 1). An understated context window
therefore collapses the reply budget toward 1 token as a conversation grows, and
the model is then scored as answering wrong. Observed on one deepseek cell as
8192 -> 8173 -> 4998 -> ... -> 1258 -> 1.

Hand-setting is not a fix either: laguna-s-2.1 was hand-set to 262144/32768,
which is the ``poolside/laguna-s-2.1:free`` row -- the free tier, not the model
we actually call. Match on the exact id, from a named source, or fail.

Provider coverage is uneven, which is why each model declares its own source:

    OpenRouter  context_length + top_provider.max_completion_tokens (may be null)
    Moonshot    context_length only
    DeepSeek    neither (id/object/owned_by only)
    Z.ai        neither

For direct-endpoint models whose vendor publishes nothing, we record OpenRouter's
listing of the same model and say so in the note -- that is routing metadata for
the same weights, not the vendor's own statement, and a reader can see that.

This is a deliberate, occasional command. It is NEVER called at run time: runs
read the committed JSON so they stay reproducible and need no network.

Usage:
    python3 -m obench.fetch_model_limits --fetched-at 2026-07-26
    python3 -m obench.fetch_model_limits --fetched-at 2026-07-26 --write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from .paths import SOURCE_ROOT

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
LIMITS_PATH = os.path.join(SOURCE_ROOT, "data", "model_limits.json")

# Our model key -> where its limits come from.
#   openrouter_id: the EXACT id to match in OpenRouter's catalog. Tier variants
#     (":free") carry different limits, so this is never fuzzy-matched.
#   vendor_ctx: when the model's own provider publishes a context length, it is
#     fetched and cross-checked against OpenRouter. A mismatch is reported, not
#     silently resolved.
#   max_tokens_note: required when no source publishes an output cap. State the
#     choice and the reason; there is deliberately no default, because pi's own
#     fallback for this case is 4096, which is smaller than anything we have run
#     and would make the starvation worse.
LIMIT_SOURCES = {
    "laguna-s-2.1": {"openrouter_id": "poolside/laguna-s-2.1"},
    "inkling": {
        "openrouter_id": "thinkingmachines/inkling",
        "max_tokens": 131072,
        "max_tokens_note": "no provider publishes an output cap; set to the cap "
                           "of poolside/laguna-s-2.1, the same 1048576-context class",
    },
    "deepseek-v4-flash": {
        "openrouter_id": "deepseek/deepseek-v4-flash",
        # Traffic now goes through OpenRouter too, so the limits describe the
        # exact route we call. Previously this was a documented proxy: we read
        # OpenRouter's catalog while calling api.deepseek.com, which publishes
        # no limit fields at all.
    },
    "kimi-k3": {
        "openrouter_id": "moonshotai/kimi-k3",
        "vendor_ctx": ("https://api.moonshot.ai/v1/models", "MOONSHOT_API_KEY", "kimi-k3"),
        "max_tokens": 131072,
        "max_tokens_note": "no provider publishes an output cap; set to the cap "
                           "of the same 1048576-context class",
    },
    "kimi-k2.7-code": {
        "openrouter_id": "moonshotai/kimi-k2.7-code",
        "vendor_ctx": ("https://api.moonshot.ai/v1/models", "MOONSHOT_API_KEY", "kimi-k2.7-code"),
    },
    "glm-5.2": {
        "openrouter_id": "z-ai/glm-5.2",
        "direct_endpoint_note": "api.z.ai /paas/v4/models publishes no limit "
                                "fields; values are OpenRouter's listing of the "
                                "same model",
    },
    "glm-4.7-flash": {
        "openrouter_id": "z-ai/glm-4.7-flash",
        "direct_endpoint_note": "api.z.ai /paas/v4/models publishes no limit "
                                "fields; values are OpenRouter's listing of the "
                                "same model",
    },
}


def _get_json(url, bearer=None, timeout=30):
    req = urllib.request.Request(url)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def fetch_openrouter(timeout=30):
    payload = _get_json(OPENROUTER_MODELS_URL, timeout=timeout)
    return {m["id"]: m for m in payload.get("data") or [] if m.get("id")}


def fetch_vendor_ctx(url, env_key, model_id, timeout=20):
    """Context length straight from the model's own provider, or None."""
    token = os.environ.get(env_key)
    if not token:
        return None
    try:
        payload = _get_json(url, bearer=token, timeout=timeout)
    except Exception:
        return None
    for entry in payload.get("data") or []:
        if entry.get("id") == model_id:
            return entry.get("context_length")
    return None


def resolve(model_key, catalog, warnings):
    """Resolve one model to a pinned record, or raise. No silent fallbacks."""
    spec = LIMIT_SOURCES.get(model_key)
    if spec is None:
        raise KeyError(
            f"{model_key!r} has no entry in LIMIT_SOURCES; declare where its "
            f"limits come from (there is no default)")
    or_id = spec["openrouter_id"]
    entry = catalog.get(or_id)
    if entry is None:
        raise KeyError(f"{model_key!r}: {or_id!r} not found in the OpenRouter catalog")

    ctx = entry.get("context_length")
    if not ctx:
        raise KeyError(f"{model_key!r}: {or_id!r} reports no context_length")

    sources = [OPENROUTER_MODELS_URL]
    if spec.get("vendor_ctx"):
        url, env_key, vendor_id = spec["vendor_ctx"]
        vendor = fetch_vendor_ctx(url, env_key, vendor_id)
        if vendor is None:
            warnings.append(f"{model_key}: vendor context unavailable ({url}); "
                            f"using OpenRouter only")
        else:
            sources.append(url)
            if vendor != ctx:
                warnings.append(
                    f"{model_key}: context MISMATCH -- vendor {url} says {vendor}, "
                    f"OpenRouter says {ctx}; using the smaller ({min(vendor, ctx)})")
                ctx = min(vendor, ctx)

    cap = (entry.get("top_provider") or {}).get("max_completion_tokens")
    note = "top_provider.max_completion_tokens"
    if not cap:
        if "max_tokens" not in spec:
            raise KeyError(
                f"{model_key!r}: no source publishes an output cap and no "
                f"'max_tokens' is declared in LIMIT_SOURCES. Choose one and "
                f"state why in 'max_tokens_note' -- pi's own fallback here is "
                f"4096, which is smaller than anything we have run.")
        cap = spec["max_tokens"]
        note = spec["max_tokens_note"]
    if spec.get("direct_endpoint_note"):
        note = f"{note}; {spec['direct_endpoint_note']}"
    return {"context_window": int(ctx), "max_tokens": int(cap),
            "openrouter_id": or_id, "sources": sources, "note": note}


def load_pinned(path=LIMITS_PATH):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="obench fetch-model-limits", description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="write data/model_limits.json (default: show a diff)")
    ap.add_argument("--fetched-at", required=True,
                    help="ISO date to stamp; passed in rather than read from the "
                         "clock so the output is reproducible")
    args = ap.parse_args(argv)

    from .adapters.pi import OPEN_MODELS
    catalog = fetch_openrouter()
    warnings, errors, fresh = [], [], {}
    for model_key in sorted(OPEN_MODELS):
        try:
            rec = resolve(model_key, catalog, warnings)
        except KeyError as exc:
            errors.append(str(exc).strip('"'))
            continue
        rec["fetched_at"] = args.fetched_at
        fresh[model_key] = rec

    pinned = load_pinned()
    for key in sorted(set(pinned) | set(fresh)):
        old, new = pinned.get(key), fresh.get(key)
        if old is None:
            print(f"  + {key}: ctx={new['context_window']} max={new['max_tokens']}")
        elif new is None:
            print(f"  ? {key}: pinned but unresolvable now (kept)")
        elif (old["context_window"], old["max_tokens"]) != (new["context_window"], new["max_tokens"]):
            print(f"  ~ {key}: ctx {old['context_window']}->{new['context_window']}"
                  f"  max {old['max_tokens']}->{new['max_tokens']}")
    for w in warnings:
        print(f"  ! {w}")
    for e in errors:
        print(f"  x {e}", file=sys.stderr)

    if args.write and not errors:
        merged = dict(pinned)
        merged.update(fresh)
        os.makedirs(os.path.dirname(LIMITS_PATH), exist_ok=True)
        with open(LIMITS_PATH, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\nwrote {LIMITS_PATH} ({len(merged)} models)")
    elif args.write:
        print("\nNOT written: unresolved models above must be fixed first",
              file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
