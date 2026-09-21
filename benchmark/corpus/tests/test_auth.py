import unittest
from synth_app.core.auth import TokenManager, Authenticator, AuthenticationError

class TestAuth(unittest.TestCase):
    def test_token_issue_and_validation(self):
        mgr = TokenManager()
        tok = mgr.issue_token("u123", "admin")
        self.assertEqual(mgr.validate_token(tok), "u123")

    def test_invalid_token_fails(self):
        mgr = TokenManager()
        with self.assertRaises(AuthenticationError):
            mgr.validate_token("nonexistent")
