# Backward Compatibility and Migration Rationale

## Rationale
V1 client integrations use query parameter notation (`acc_id`, `val`, `cur`).
To avoid breaking customer automations, `LegacyTransactionAdapter` maps these fields
to the canonical v2 schema and emits runtime deprecation warnings.
Removal is scheduled for v3.0 (Q4 2027).
