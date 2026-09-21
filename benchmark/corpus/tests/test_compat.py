import unittest
import warnings
from synth_app.repository.account_repo import AccountRepository
from synth_app.services.billing import BillingService
from synth_app.handlers.api import TransactionHandler
from synth_app.compat.legacy_api import LegacyTransactionAdapter

class TestCompat(unittest.TestCase):
    def test_v1_compatibility_wrapper(self):
        repo = AccountRepository()
        svc = BillingService(repo)
        handler = TransactionHandler(svc)
        adapter = LegacyTransactionAdapter(handler)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            res = adapter.handle_v1_request({"acc_id": "acc_1001", "val": 25.0, "cur": "USD"})
            self.assertEqual(res["status"], "ok")
            self.assertTrue(any(issubclass(item.category, DeprecationWarning) for item in w))
