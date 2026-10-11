#!/usr/bin/env python3
"""
prompt_tokens must include the cached input, because `claude --print` puts
almost all of a real prompt there.

Measured on 2026-10-11 for a 13.5k-char prompt:
    cache_creation_input_tokens: 37795
    cache_read_input_tokens:     10468
    input_tokens:                    2

The non-stream reply read only `input_tokens`, so it reported prompt_tokens=2
for every request regardless of size -- including a verified 181,816-byte,
183-message conversation whose history DID reach the model (it recalled a
codeword from message 1). Clients that size their context off usage were being
handed a number four orders of magnitude too small.
"""
import unittest

import server


class PromptTokensTests(unittest.TestCase):
    def test_sums_cache_fields_not_just_input_tokens(self):
        usage = {
            "input_tokens": 2,
            "cache_read_input_tokens": 10468,
            "cache_creation_input_tokens": 37795,
            "output_tokens": 10,
        }
        self.assertEqual(server.prompt_tokens_of(usage), 48265)

    def test_absent_cache_fields_fall_back_to_input_tokens(self):
        """A provider that does not cache must still report its real input."""
        self.assertEqual(server.prompt_tokens_of({"input_tokens": 1234}), 1234)

    def test_empty_usage_is_zero_not_an_error(self):
        self.assertEqual(server.prompt_tokens_of({}), 0)

    def test_cache_only_usage_is_not_reported_as_zero(self):
        """The exact shape that produced prompt_tokens=2 in production."""
        usage = {"input_tokens": 2, "cache_read_input_tokens": 52000}
        got = server.prompt_tokens_of(usage)
        self.assertEqual(got, 52002)
        self.assertGreater(got, 1000, "a cached prompt must not look empty")


if __name__ == "__main__":
    unittest.main()
