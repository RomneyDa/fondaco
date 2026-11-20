"""Claim UI bind policy."""

import unittest

from bind_policy import resolve_ui_bind, token_matches


class BindPolicyTest(unittest.TestCase):
    def test_defaults_to_loopback(self):
        host, token = resolve_ui_bind(None, None)
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(token, "")

    def test_public_bind_requires_a_token(self):
        with self.assertRaises(ValueError):
            resolve_ui_bind("0.0.0.0", None)
        with self.assertRaises(ValueError):
            resolve_ui_bind("0.0.0.0", "  ")
        host, token = resolve_ui_bind("0.0.0.0", "s3cret")
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(token, "s3cret")

    def test_token_compare(self):
        self.assertTrue(token_matches("abc", "abc"))
        self.assertFalse(token_matches("abc", "abd"))
        self.assertFalse(token_matches("abc", "abcd"))
        self.assertFalse(token_matches("", ""))


if __name__ == "__main__":
    unittest.main()
