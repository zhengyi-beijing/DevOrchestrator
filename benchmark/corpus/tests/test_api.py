import unittest
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService
from synth_app.handlers.api import TransactionHandler

class TestAPI(unittest.TestCase):
    def test_charge_success_with_curr(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        # When 'curr' is explicitly supplied:
        res = handler.handle_charge({"account_id": "acc_1001", "amount": 50.0, "curr": "USD"})
        self.assertEqual(res["status"], "ok")

    def test_reproduce_cross_file_currency_defect(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        # Normal client passes 'currency': 'USD', but handler looks for 'curr',
        # causing currency=None, triggering CurrencyConversionError in account_repo
        res = handler.handle_charge({"account_id": "acc_1001", "amount": 50.0, "currency": "USD"})
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_type"], "CurrencyConversionError")
