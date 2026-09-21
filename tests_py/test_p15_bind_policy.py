import sys
import unittest
from unittest.mock import patch

SRC = Path = __import__("pathlib").Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.mobile.bind_policy import verify_tailscale_bind_address


class TestP15BindPolicy(unittest.TestCase):
    def test_refuse_wildcard_addresses(self):
        for addr in ("0.0.0.0", "::"):
            ok, reason = verify_tailscale_bind_address(addr)
            self.assertFalse(ok)
            self.assertIn("wildcard address", reason.lower())

    def test_refuse_loopback_addresses(self):
        for addr in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            ok, reason = verify_tailscale_bind_address(addr)
            self.assertFalse(ok)
            self.assertTrue("loopback" in reason.lower() or "invalid" in reason.lower())

    def test_refuse_rfc1918_private_lan_addresses(self):
        for addr in ("10.0.1.5", "172.16.0.1", "192.168.1.100"):
            ok, reason = verify_tailscale_bind_address(addr)
            self.assertFalse(ok)
            self.assertIn("rfc1918", reason.lower())

    def test_refuse_public_and_non_tailscale_addresses(self):
        for addr in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            ok, reason = verify_tailscale_bind_address(addr)
            self.assertFalse(ok)
            self.assertIn("tailscale networks", reason.lower())

    def test_refuse_unassigned_tailscale_addresses(self):
        # A valid Tailscale CGNAT IP (100.64.0.0/10) that is not assigned to this machine
        ok, reason = verify_tailscale_bind_address("100.100.100.100")
        self.assertFalse(ok)
        self.assertIn("not assigned to a local interface", reason.lower())

    def test_admit_corroborated_local_tailscale_address(self):
        # When socket.bind succeeds on a 100.x.y.z IP, it is accepted
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = mock_sock_cls.return_value
            mock_sock.bind.return_value = None  # Simulates successful bind
            ok, reason = verify_tailscale_bind_address("100.64.1.2")
            self.assertTrue(ok)
            self.assertIn("verified", reason.lower())


if __name__ == "__main__":
    unittest.main()
