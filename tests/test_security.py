import unittest

from app.security import create_token, decode_token, hash_password, verify_password


class TestSecurity(unittest.TestCase):
    def test_password_hashing(self):
        pw = "supersecret123"
        hashed = hash_password(pw)
        self.assertNotEqual(pw, hashed)
        self.assertTrue(verify_password(pw, hashed))
        self.assertFalse(verify_password("wrongpassword", hashed))

    def test_token_creation_and_decoding(self):
        token = create_token("admin_user", "admin")
        self.assertIsInstance(token, str)
        decoded = decode_token(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], "admin_user")
        self.assertEqual(decoded["role"], "admin")

    def test_invalid_token(self):
        self.assertIsNone(decode_token("invalid.token.payload"))


if __name__ == "__main__":
    unittest.main()
