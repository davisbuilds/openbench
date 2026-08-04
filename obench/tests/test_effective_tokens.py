#!/usr/bin/env python3
"""Unit tests for stats.effective_tokens / proxy_fresh_tokens."""

import unittest

from obench import stats


class TestProxyFreshTokens(unittest.TestCase):
    def test_sums_uncached_and_output(self):
        row = {
            "tokens_proxy_input_uncached": 100,
            "tokens_proxy_output": 50,
            "tokens_proxy_cache_read": 9999,
            "tokens_proxy_cache_write": 12,
            "tokens_proxy_reasoning": 7,
        }
        self.assertEqual(stats.proxy_fresh_tokens(row), 150.0)

    def test_requires_both_legs(self):
        self.assertIsNone(stats.proxy_fresh_tokens({
            "tokens_proxy_input_uncached": 100,
            "tokens_proxy_output": None,
        }))
        self.assertIsNone(stats.proxy_fresh_tokens({}))


class TestEffectiveTokens(unittest.TestCase):
    def test_self_reported_wins(self):
        value, basis = stats.effective_tokens({
            "tokens": 200,
            "token_basis": "vendor_split",
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 1000,
            "tokens_proxy_output": 1000,
            "tokens_proxy_cache_read": 50000,
        })
        self.assertEqual(value, 200.0)
        self.assertEqual(basis, stats.TOKEN_BASIS_SELF)

    def test_proxy_when_tokens_null(self):
        value, basis = stats.effective_tokens({
            "tokens": None,
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 40,
            "tokens_proxy_output": 10,
            "tokens_proxy_cache_read": 8000,
        })
        self.assertEqual(value, 50.0)
        self.assertEqual(basis, stats.TOKEN_BASIS_PROXY)

    def test_unmetered(self):
        value, basis = stats.effective_tokens({
            "tokens": None,
            "token_basis": "unmetered",
        })
        self.assertIsNone(value)
        self.assertEqual(basis, stats.TOKEN_BASIS_UNMETERED)

    def test_older_row_without_proxy(self):
        value, basis = stats.effective_tokens({"tokens": 1234})
        self.assertEqual(value, 1234.0)
        self.assertEqual(basis, stats.TOKEN_BASIS_SELF)

    def test_missing_everything(self):
        value, basis = stats.effective_tokens({"tokens": None})
        self.assertIsNone(value)
        self.assertIsNone(basis)

    def test_proxy_flag_without_numbers_is_none(self):
        value, basis = stats.effective_tokens({
            "tokens": None,
            "token_basis_proxy": "proxy_measured",
        })
        self.assertIsNone(value)
        self.assertIsNone(basis)

    def test_total_tokens_uses_effective(self):
        self.assertEqual(stats.total_tokens({
            "tokens": None,
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 3,
            "tokens_proxy_output": 4,
            "tokens_proxy_cache_read": 100,
        }), 7.0)

    def test_total_tokens_tokens_total_still_wins(self):
        self.assertEqual(stats.total_tokens({
            "tokens_total": 99,
            "tokens": 1,
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 3,
            "tokens_proxy_output": 4,
        }), 99)

    def test_display_token_basis_normalizes(self):
        self.assertEqual(
            stats.display_token_basis({"token_basis": "vendor_split", "tokens": 1}),
            stats.TOKEN_BASIS_SELF,
        )
        self.assertEqual(
            stats.display_token_basis({
                "tokens": None,
                "token_basis_proxy": "proxy_measured",
                "tokens_proxy_input_uncached": 1,
                "tokens_proxy_output": 1,
            }),
            stats.TOKEN_BASIS_PROXY,
        )
        self.assertEqual(
            stats.display_token_basis({"token_basis": "unmetered"}),
            stats.TOKEN_BASIS_UNMETERED,
        )

    def test_proxy_mismatch_excludes_all_usage_metrics(self):
        row = {
            "model": "model-x",
            "tokens": 200,
            "tokens_input_uncached": 150,
            "tokens_output": 50,
            "tokens_proxy_input_uncached": 151,
            "tokens_proxy_output": 50,
            "token_basis_proxy": "proxy_measured",
            "usage_evidence_grade": "harbor_reported_proxy_mismatch",
            "usage_ranking_eligible": False,
            "usage_ranking_exclusion_reason": "proxy_mismatch",
        }
        self.assertEqual(stats.effective_tokens(row), (None, None))
        self.assertIsNone(stats.input_tokens(row))
        self.assertIsNone(stats.output_tokens(row))
        self.assertIsNone(
            stats.row_cost(
                row,
                {
                    "model-x": {
                        "input_per_mtok": 1.0,
                        "output_per_mtok": 2.0,
                    }
                },
            )
        )
        self.assertEqual(
            stats.display_token_basis(row), "Harbor/proxy mismatch"
        )


if __name__ == "__main__":
    unittest.main()
