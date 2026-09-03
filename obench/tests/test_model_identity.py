#!/usr/bin/env python3
"""canonical_model + reasoning_effort must be split out of the glued model name.

The matrix runner and each results row see only the glued arm name
(``gpt-5.6-terra-xhigh``); the real model id and the reasoning effort are the
adapter's knowledge. Downstream frontier tooling was left to strip the effort
suffix heuristically to price/classify an arm -- a guess. The adapter owns the
mapping (codex ``MODELS`` / ``_EFFORT`` for subscription arms, ``OPEN_MODELS``
for bridge arms), so the row must carry its decomposition as explicit fields.
"""

import unittest

from obench.adapters import codex
from obench import run


class CodexModelIdentityTests(unittest.TestCase):
    def test_subscription_model_splits_effort_suffix(self):
        info = codex.model_identity("gpt-5.6-terra-xhigh")
        self.assertEqual(info["canonical_model"], "gpt-5.6-terra")
        self.assertEqual(info["reasoning_effort"], "xhigh")
        self.assertIs(info["is_open"], False)  # native subscription arm

    def test_luna_max_splits(self):
        info = codex.model_identity("gpt-5.6-luna-max")
        self.assertEqual(info["canonical_model"], "gpt-5.6-luna")
        self.assertEqual(info["reasoning_effort"], "max")
        self.assertIs(info["is_open"], False)

    def test_bare_subscription_model_is_medium(self):
        info = codex.model_identity("gpt-5.6-terra")
        self.assertEqual(info["canonical_model"], "gpt-5.6-terra")
        self.assertEqual(info["reasoning_effort"], "medium")
        self.assertIs(info["is_open"], False)

    def test_open_model_keeps_id_and_bridge_default_effort(self):
        info = codex.model_identity("minimax-m3")
        self.assertEqual(info["canonical_model"], "minimax-m3")
        self.assertEqual(info["reasoning_effort"], "medium")
        self.assertIs(info["is_open"], True)  # bridge-routed open arm

    def test_unknown_model_passes_through_with_no_effort_claim(self):
        info = codex.model_identity("totally-made-up-model")
        self.assertEqual(info["canonical_model"], "totally-made-up-model")
        self.assertIsNone(info["reasoning_effort"])
        self.assertIsNone(info["is_open"])  # honest unknown, not a guess


class ResolveModelIdentityTests(unittest.TestCase):
    def test_reads_the_adapters_decomposition(self):
        canonical, effort, is_open = run._resolve_model_identity(
            "codex", "gpt-5.6-terra-xhigh", None)
        self.assertEqual(canonical, "gpt-5.6-terra")
        self.assertEqual(effort, "xhigh")
        self.assertIs(is_open, False)

    def test_open_arm_marked_open(self):
        canonical, effort, is_open = run._resolve_model_identity(
            "codex", "minimax-m3", None)
        self.assertEqual(canonical, "minimax-m3")
        self.assertIs(is_open, True)

    def test_unknown_harness_falls_back_to_model_and_none(self):
        # An adapter that cannot load must never break a row: the honest answer
        # is the glued name and no effort/open claim, not a raised exception.
        canonical, effort, is_open = run._resolve_model_identity(
            "no-such-harness", "some-model", None)
        self.assertEqual(canonical, "some-model")
        self.assertIsNone(effort)
        self.assertIsNone(is_open)


if __name__ == "__main__":
    unittest.main()
