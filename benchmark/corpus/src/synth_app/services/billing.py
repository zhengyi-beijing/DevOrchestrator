"""Billing and invoicing business service.

OWNERSHIP_DOMAIN: Core Business Logic Team
RESPONSIBILITIES:
- Order invoice generation
- Transaction coordination
- Interaction with AccountRepository
"""
from __future__ import annotations

from typing import Any
from synth_app.repository.account_repo import AccountRepository, CurrencyConversionError


class BillingService:
    """Coordinates account billing transactions."""

    def __init__(self, account_repo: AccountRepository) -> None:
        self.account_repo = account_repo

    def process_charge(self, account_id: str, amount: float, currency: str) -> dict[str, Any]:
        """Process a debit charge against an account balance."""
        if amount <= 0:
            raise ValueError("charge amount must be positive")
        # Line 24: delegates directly to account repository
        tx_id = self.account_repo.charge_account(account_id, amount, currency)
        return {
            "status": "success",
            "account_id": account_id,
            "transaction_id": tx_id,
            "amount": amount,
            "currency": currency,
        }
