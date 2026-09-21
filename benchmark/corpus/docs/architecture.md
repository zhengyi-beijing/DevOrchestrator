# SynthApp Architecture Ownership and Dependency Boundaries

## Ownership Matrix

| Domain | Responsible Team | Files |
| --- | --- | --- |
| Security & Auth | Security Infrastructure Team | `src/synth_app/core/auth.py` |
| Performance / Caching | Platform Performance Team | `src/synth_app/core/cache.py` |
| Billing Logic | Core Business Logic Team | `src/synth_app/services/billing.py` |
| Data Layer | Data Layer Team | `src/synth_app/repository/account_repo.py` |
| API Gateway | Application API Gateway Team | `src/synth_app/handlers/api.py` |
| Backward Compatibility | Compatibility Guild | `src/synth_app/compat/legacy_api.py` |

## Dependency Flow
`handlers/api.py` -> `services/billing.py` -> `repository/account_repo.py`
`compat/legacy_api.py` wraps `handlers/api.py`.
`core/auth.py` and `core/cache.py` provide cross-cutting foundation without downstream dependencies.
