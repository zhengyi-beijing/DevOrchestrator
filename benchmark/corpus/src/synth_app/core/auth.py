"""Authentication and authorization module.

OWNERSHIP_DOMAIN: Security Infrastructure Team
RESPONSIBILITIES:
- Token validation and JWT issuance
- Role-based access control (RBAC)
- Identity verification
"""
from __future__ import annotations

from typing import Any


class AuthenticationError(Exception):
    """Raised when authentication credentials are invalid or missing."""


class TokenManager:
    """Manages secure access tokens."""

    def __init__(self, secret_key: str = "synth-secret") -> None:
        self.secret_key = secret_key
        self._tokens: dict[str, str] = {}

    def issue_token(self, user_id: str, role: str) -> str:
        token = f"tok_{user_id}_{role}_{len(self._tokens)}"
        self._tokens[token] = user_id
        return token

    def validate_token(self, token: str) -> str:
        if token not in self._tokens:
            raise AuthenticationError(f"invalid token: {token}")
        return self._tokens[token]


class Authenticator:
    """RBAC authentication gate."""

    def __init__(self, token_manager: TokenManager) -> None:
        self.token_manager = token_manager
        self.allowed_roles: set[str] = {"admin", "operator", "user"}

    def authenticate_request(self, auth_header: str | None) -> str:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationError("missing or malformed Authorization header")
        token = auth_header[7:].strip()
        return self.token_manager.validate_token(token)
