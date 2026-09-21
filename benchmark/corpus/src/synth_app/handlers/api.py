"""HTTP API endpoint handlers for account services.

OWNERSHIP_DOMAIN: Application API Gateway Team
RESPONSIBILITIES:
- Request payload deserialization and validation
- Invocation of billing service
- Error envelope mapping
"""
from __future__ import annotations

from typing import Any
from synth_app.services.billing import BillingService


class TransactionHandler:
    """Handles external transaction requests."""

    def __init__(self, billing_service: BillingService) -> None:
        self.billing_service = billing_service

    def handle_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle incoming charge request payload.

        Root cause defect origin:
        Payload key 'currency' is expected by clients, but code reads 'curr'.
        When 'curr' is missing, it evaluates to None, passing None into BillingService.
        """
        account_id = payload.get("account_id")
        if not account_id:
            return {"status": "error", "message": "missing account_id"}
        amount = payload.get("amount")
        if amount is None or amount <= 0:
            return {"status": "error", "message": "invalid amount"}
        # Line 32: BUG ORIGIN: payload.get('curr') instead of payload.get('currency', 'USD')
        currency = payload.get("curr")
        try:
            result = self.billing_service.process_charge(account_id, float(amount), currency)  # type: ignore[arg-type]
            return {"status": "ok", "data": result}
        except Exception as exc:
            return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
