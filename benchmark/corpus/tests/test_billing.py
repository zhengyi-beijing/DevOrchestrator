import unittest
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService

class TestBilling(unittest.TestCase):
    def test_successful_charge(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        res = svc.process_charge("acc_1001", 100.0, "USD")
        self.assertEqual(res["status"], "success")
        self.assertEqual(repo.get_balance("acc_1001"), 1400.0)
