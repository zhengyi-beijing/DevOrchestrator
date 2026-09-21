import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.mobile.authorizer import MobileDevicePrincipal


class TestP15MobileIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tmp.name)
        self.security = ControlSecurity(self.runtime_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pairing_and_bearer_validation(self):
        # Create pairing
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        self.assertIn("pairing_id", pairing)
        self.assertIn("code", pairing)
        self.assertGreaterEqual(len(pairing["code"]), 8)

        # Redeem pairing
        redeemed = self.security.redeem_mobile_pairing(
            pairing["pairing_id"], pairing["code"], "Pixel Test"
        )
        self.assertTrue(redeemed["device_id"].startswith("dev-"))
        self.assertTrue(len(redeemed["token"]) > 20)
        self.assertEqual(redeemed["scope"], "mobile_device")

        # Validate with raw token
        ok, reason, principal = self.security.validate_mobile_bearer(redeemed["token"])
        self.assertTrue(ok)
        self.assertIsInstance(principal, MobileDevicePrincipal)
        self.assertEqual(principal.device_id, redeemed["device_id"])
        self.assertFalse(principal.revoked)

        # Validate with "Bearer <token>" header
        ok, reason, principal = self.security.validate_mobile_bearer(f"Bearer {redeemed['token']}")
        self.assertTrue(ok)
        self.assertEqual(principal.device_id, redeemed["device_id"])

        # Invalid token fails
        ok, reason, principal = self.security.validate_mobile_bearer("bad_token_123")
        self.assertFalse(ok)
        self.assertIsNone(principal)

    def test_tokenless_device_lookup(self):
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        redeemed = self.security.redeem_mobile_pairing(
            pairing["pairing_id"], pairing["code"], "Test Phone"
        )
        dev_id = redeemed["device_id"]

        # Lookup without token
        ok, reason, principal = self.security.lookup_mobile_device(dev_id)
        self.assertTrue(ok)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.device_id, dev_id)
        self.assertFalse(principal.revoked)

        # Lookup unknown device
        ok, reason, principal = self.security.lookup_mobile_device("dev_unknown_000")
        self.assertFalse(ok)
        self.assertIn("not found", reason.lower())

    def test_single_and_global_revocation_with_monotonic_generation(self):
        gen0 = self.security.mobile_revocation_generation()
        self.assertEqual(gen0, 0)

        # Pair two devices
        p1 = self.security.create_mobile_pairing()
        r1 = self.security.redeem_mobile_pairing(p1["pairing_id"], p1["code"], "Dev 1")
        p2 = self.security.create_mobile_pairing()
        r2 = self.security.redeem_mobile_pairing(p2["pairing_id"], p2["code"], "Dev 2")

        # Revoke dev 1
        res = self.security.revoke_mobile_device(r1["device_id"])
        self.assertTrue(res["revoked"])
        gen1 = self.security.mobile_revocation_generation()
        self.assertGreater(gen1, gen0)

        # Dev 1 bearer and lookup fail
        ok1, reason1, _ = self.security.validate_mobile_bearer(r1["token"])
        self.assertFalse(ok1)
        self.assertIn("revoked", reason1.lower())
        ok_l1, reason_l1, _ = self.security.lookup_mobile_device(r1["device_id"])
        self.assertFalse(ok_l1)
        self.assertIn("revoked", reason_l1.lower())

        # Dev 2 is still valid
        ok2, _, _ = self.security.validate_mobile_bearer(r2["token"])
        self.assertTrue(ok2)

        # Revoke all
        res_all = self.security.revoke_all_mobile_devices()
        self.assertTrue(res_all["revoked"])
        gen2 = self.security.mobile_revocation_generation()
        self.assertGreater(gen2, gen1)

        # Dev 2 is now also revoked
        ok2_after, _, _ = self.security.validate_mobile_bearer(r2["token"])
        self.assertFalse(ok2_after)

    def test_restart_persistence(self):
        pairing = self.security.create_mobile_pairing()
        redeemed = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Persistent")
        gen_before = self.security.mobile_revocation_generation()

        # Instantiate fresh ControlSecurity pointing at same runtime_root
        fresh_security = ControlSecurity(self.runtime_root)
        self.assertEqual(fresh_security.mobile_revocation_generation(), gen_before)

        ok, _, principal = fresh_security.validate_mobile_bearer(redeemed["token"])
        self.assertTrue(ok)
        self.assertEqual(principal.device_id, redeemed["device_id"])

        ok_l, _, _ = fresh_security.lookup_mobile_device(redeemed["device_id"])
        self.assertTrue(ok_l)

    def test_fail_closed_on_corrupt_canonical_state(self):
        # Create valid device first
        p = self.security.create_mobile_pairing()
        r = self.security.redeem_mobile_pairing(p["pairing_id"], p["code"], "Device")
        token = r["token"]
        dev_id = r["device_id"]

        cap_file = self.runtime_root / "control" / "adapter-capabilities.json"

        # 1. Truncated / malformed JSON
        cap_file.write_text("{ incomplete json ...", encoding="utf-8")
        ok, reason, _ = self.security.validate_mobile_bearer(token)
        self.assertFalse(ok)
        self.assertTrue("unreadable" in reason.lower() or "corrupt" in reason.lower() or "malformed" in reason.lower())

        ok_l, reason_l, _ = self.security.lookup_mobile_device(dev_id)
        self.assertFalse(ok_l)
        self.assertTrue("unreadable" in reason_l.lower() or "corrupt" in reason_l.lower() or "malformed" in reason_l.lower())

        # 2. Empty file
        cap_file.write_text("", encoding="utf-8")
        ok, reason, _ = self.security.validate_mobile_bearer(token)
        self.assertFalse(ok)

        # 3. Unsupported schema version
        cap_file.write_text(json.dumps({"schema_version": 999, "mobile_devices": {}}), encoding="utf-8")
        ok, reason, _ = self.security.validate_mobile_bearer(token)
        self.assertFalse(ok)
        self.assertIn("unsupported", reason.lower())

    def test_device_present_only_in_telemetry_is_denied(self):
        # Write device entry into non-authoritative telemetry file only
        telemetry_path = self.runtime_root / "mobile" / "devices-telemetry.json"
        telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        telemetry_path.write_text(
            json.dumps({"fake_dev": {"app_version": "1.0", "last_seen": "now"}}),
            encoding="utf-8"
        )

        ok, _, _ = self.security.lookup_mobile_device("fake_dev")
        self.assertFalse(ok)
        ok_b, _, _ = self.security.validate_mobile_bearer("fake_dev")
        self.assertFalse(ok_b)


if __name__ == "__main__":
    unittest.main()
