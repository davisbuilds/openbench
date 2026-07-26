#!/usr/bin/env python3
"""Model context/output limits must be pinned, sourced, and never guessed.

The pi provider config openbench generates requires contextWindow and maxTokens
per model. Those were hand-written with a silent fallback of 128000/8192, and
five of seven open models inherited it. Nothing failed, because the numbers look
reasonable -- meanwhile deepseek-v4-flash ran with an 8x understated context and
a 48x understated output cap against its real 1048576 / 393216.

That is not cosmetic. pi clamps every request to
``min(maxTokens, max(1, contextWindow - promptTokens - 4096))`` with
MIN_MAX_TOKENS = 1, so an understated context window drives the reply budget to
1 token on long conversations and the model is then scored as answering wrong.

Hand-setting is not the fix either: laguna-s-2.1 was hand-set to 262144/32768,
which is the ``poolside/laguna-s-2.1:free`` row -- the free tier, not the model
we call. These tests pin the properties that stop both failure modes.
"""

import json
import os
import pathlib
import unittest

from obench import fetch_model_limits as fml
from obench.adapters import pi
from obench.adapters.pi import OPEN_MODELS


class PinnedLimitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pinned = fml.load_pinned()

    def test_every_open_model_has_pinned_limits(self):
        missing = sorted(set(OPEN_MODELS) - set(self.pinned))
        self.assertEqual(
            missing, [],
            f"open models with no pinned limits: {missing}. Run "
            f"`python3 -m obench.fetch_model_limits --fetched-at <date> --write`; "
            f"do NOT hand-write numbers into the adapter.")

    def test_every_entry_carries_provenance(self):
        for key, rec in sorted(self.pinned.items()):
            self.assertTrue(rec.get("sources"), f"{key}: no source recorded")
            self.assertTrue(rec.get("fetched_at"), f"{key}: no fetch date")
            self.assertTrue(rec.get("note"), f"{key}: no note explaining the values")

    def test_limits_are_plausible_and_ordered(self):
        for key, rec in sorted(self.pinned.items()):
            ctx, cap = rec["context_window"], rec["max_tokens"]
            self.assertGreater(ctx, 0, key)
            self.assertGreater(cap, 0, key)
            self.assertLessEqual(
                cap, ctx,
                f"{key}: output cap {cap} exceeds context window {ctx}")

    def test_no_entry_still_carries_the_old_fallback_pair(self):
        # 128000/8192 was the silent fallback. If a model resolves to exactly
        # that pair again, it is far more likely the fallback leaked back in
        # than that the real numbers coincide.
        for key, rec in sorted(self.pinned.items()):
            self.assertNotEqual(
                (rec["context_window"], rec["max_tokens"]), (128000, 8192),
                f"{key}: matches the historic silent fallback exactly; verify "
                f"against the provider rather than trusting it")

    def test_openrouter_ids_are_exact_not_tier_variants(self):
        # laguna was mis-set from the ':free' row. Tier suffixes carry different
        # limits, so a pinned id must never be a variant of a declared model.
        for key, spec in sorted(fml.LIMIT_SOURCES.items()):
            or_id = spec["openrouter_id"]
            self.assertNotIn(
                ":", or_id,
                f"{key}: {or_id!r} looks like a tier variant; pin the exact "
                f"model id we send")

    def test_declared_sources_cover_every_open_model(self):
        missing = sorted(set(OPEN_MODELS) - set(fml.LIMIT_SOURCES))
        self.assertEqual(
            missing, [],
            f"models with no declared limit source: {missing}. Add a "
            f"LIMIT_SOURCES entry naming where its limits come from.")

    def test_a_model_without_a_published_cap_must_state_its_choice(self):
        for key, spec in sorted(fml.LIMIT_SOURCES.items()):
            if "max_tokens" in spec:
                self.assertTrue(
                    spec.get("max_tokens_note"),
                    f"{key}: hard-codes max_tokens without max_tokens_note; "
                    f"state why, since no provider publishes one")


class ResolverTests(unittest.TestCase):
    """The resolver must refuse to guess."""

    CATALOG = {"vendor/model": {"context_length": 1000,
                                "top_provider": {"max_completion_tokens": 100}}}

    def test_unknown_model_raises_rather_than_defaulting(self):
        with self.assertRaises(KeyError):
            fml.resolve("not-a-model", self.CATALOG, [])

    def test_missing_openrouter_entry_raises(self):
        fml.LIMIT_SOURCES["__probe__"] = {"openrouter_id": "absent/model"}
        try:
            with self.assertRaises(KeyError):
                fml.resolve("__probe__", self.CATALOG, [])
        finally:
            del fml.LIMIT_SOURCES["__probe__"]

    def test_null_output_cap_without_a_declared_choice_raises(self):
        catalog = {"vendor/model": {"context_length": 1000,
                                    "top_provider": {"max_completion_tokens": None}}}
        fml.LIMIT_SOURCES["__probe__"] = {"openrouter_id": "vendor/model"}
        try:
            with self.assertRaises(KeyError):
                fml.resolve("__probe__", catalog, [])
        finally:
            del fml.LIMIT_SOURCES["__probe__"]

    def test_declared_choice_is_used_and_noted(self):
        catalog = {"vendor/model": {"context_length": 1000,
                                    "top_provider": {"max_completion_tokens": None}}}
        fml.LIMIT_SOURCES["__probe__"] = {"openrouter_id": "vendor/model",
                                          "max_tokens": 256,
                                          "max_tokens_note": "because reasons"}
        try:
            rec = fml.resolve("__probe__", catalog, [])
            self.assertEqual(rec["max_tokens"], 256)
            self.assertIn("because reasons", rec["note"])
        finally:
            del fml.LIMIT_SOURCES["__probe__"]


class NoFallbackTests(unittest.TestCase):
    """The adapter must not be able to invent limits again."""

    def test_provider_template_has_no_literal_limit_defaults(self):
        """No `.get(<key>, <number>)` fallback for a limit anywhere in pi.py.

        The bug was `spec.get('context_window', 128000)` /
        `spec.get('max_tokens', 8192)`. Checked over the AST, not the text: a
        substring search also matches the docstrings that describe the bug,
        which is how the first version of this test failed on prose.
        """
        import ast
        tree = ast.parse(pathlib.Path(pi.__file__).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "get"
                    and len(node.args) == 2):
                continue
            key, default = node.args
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and key.value in ("context_window", "max_tokens")
                    and isinstance(default, ast.Constant)
                    and isinstance(default.value, int)):
                offenders.append(f"line {node.lineno}: "
                                 f"{key.value} defaulting to {default.value}")
        self.assertEqual(
            offenders, [],
            f"limit fallbacks are back in pi.py: {offenders}. Limits must come "
            f"from data/model_limits.json or raise.")

    def test_missing_model_raises_rather_than_defaulting(self):
        with self.assertRaises(RuntimeError):
            pi._model_limits("definitely-not-a-model")

    def test_generated_config_uses_the_pinned_values(self):
        for key in ("deepseek-v4-flash", "laguna-s-2.1"):
            js = pi._pi_provider_ext(pi.OPEN_MODELS[key], key)
            rec = fml.load_pinned()[key]
            self.assertIn(f"contextWindow: {rec['context_window']}", js)
            self.assertIn(f"maxTokens: {rec['max_tokens']}", js)

    def test_limits_file_is_mounted_into_the_container(self):
        # pi.py reads this INSIDE the container. An adapter dependency that is
        # not mounted has killed every cell for its harness three times before
        # (router_spec, candidates, gateway_spec).
        from obench import docker_exec
        src = pathlib.Path(docker_exec.__file__).read_text(encoding="utf-8")
        self.assertIn("/bench/model_limits.json", src,
                      "model_limits.json is not mounted into /bench")
        self.assertTrue(os.path.isfile(docker_exec.MODEL_LIMITS_PATH),
                        f"{docker_exec.MODEL_LIMITS_PATH} does not exist")


if __name__ == "__main__":
    unittest.main()
