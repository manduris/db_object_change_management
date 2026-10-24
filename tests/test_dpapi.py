import unittest

from app.dpapi import protect_text, unprotect_text


class DpapiTests(unittest.TestCase):
    def test_roundtrip(self):
        secret = "한글 비밀번호 !@# q'[;]'"
        hidden = protect_text(secret)
        self.assertNotIn(secret, hidden)
        self.assertTrue(hidden.startswith("dpapi:v1:"))
        self.assertEqual(unprotect_text(hidden), secret)
        self.assertNotEqual(protect_text(secret), hidden)

    def test_rejects_plain_text(self):
        with self.assertRaises(ValueError):
            unprotect_text("plain-password")


if __name__ == "__main__":
    unittest.main()
