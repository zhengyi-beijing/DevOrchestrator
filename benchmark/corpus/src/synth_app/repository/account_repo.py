"""Account data repository with persistent storage abstractions.

OWNERSHIP_DOMAIN: Data Layer Team
RESPONSIBILITIES:
- Account balance persistence
- Transaction journal records
- Currency validation
"""
from __future__ import annotations

from typing import Any
import uuid


class CurrencyConversionError(Exception):
    """Raised when currency conversion fails or unsupported currency is passed."""


class AccountRepository:
    """In-memory account repository."""

    def __init__(self) -> None:
        self.balances: dict[str, float] = {
            "acc_1001": 1500.0,
            "acc_1002": 500.0,
            "acc_1003": 25.0,
        }
        self.transactions: list[dict[str, Any]] = []

    def get_balance(self, account_id: str) -> float:
        if account_id not in self.balances:
            raise KeyError(f"unknown account: {account_id}")
        return self.balances[account_id]

    def charge_account(self, account_id: str, amount: float, currency: str) -> str:
        """Debit the account balance in specified currency.

        Cross-file defect location:
        Fails if currency is not string or unsupported.
        """
        if account_id not in self.balances:
            raise KeyError(f"unknown account: {account_id}")
        if currency != "USD":
            # Line 44: defect triggered when currency is None or non-USD
            raise CurrencyConversionError(f"unsupported currency {currency!r}")
        if self.balances[account_id] < amount:
            raise ValueError(f"insufficient funds in account {account_id}")
        self.balances[account_id] -= amount
        tx_id = f"tx_{uuid.uuid4().hex[:8]}"
        self.transactions.append({
            "tx_id": tx_id,
            "account_id": account_id,
            "amount": amount,
            "currency": currency,
        })
        return tx_id
