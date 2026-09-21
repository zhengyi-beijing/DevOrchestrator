"""Legacy v1 API adapter for backward compatibility.

OWNERSHIP_DOMAIN: Application API Gateway Team / Compatibility Guild
RATIONALE:
- Preserves v1 URL query parameters ('acc_id', 'val', 'cur')
- Maps legacy payloads into v2 canonical format
- Emits deprecation warnings while maintaining zero breaking changes
- Deprecation horizon: scheduled removal in v3.0 (Q4 2027)
"""
from __future__ import annotations

import warnings
from typing import Any
from synth_app.handlers.api import TransactionHandler


class LegacyTransactionAdapter:
    """Adapts legacy v1 requests to current TransactionHandler."""

    def __init__(self, handler: TransactionHandler) -> None:
        self.handler = handler

    def handle_v1_request(self, query_params: dict[str, Any]) -> dict[str, Any]:
        """Map legacy query params to modern transaction payload."""
        warnings.warn(
            "v1 transaction endpoint is deprecated; migrate to v2 payload format",
            DeprecationWarning,
            stacklevel=2,
        )
        # Compatibility mapping:
        # 'acc_id' -> 'account_id'
        # 'val' -> 'amount'
        # 'cur' -> 'curr' (or 'currency')
        account_id = query_params.get("acc_id")
        amount = query_params.get("val")
        currency = query_params.get("cur", "USD")
        v2_payload = {
            "account_id": account_id,
            "amount": amount,
            "curr": currency,
        }
        return self.handler.handle_charge(v2_payload)
