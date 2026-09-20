"""Local JWT issuance/verification for the Action layer's three-role RBAC.

Replaces comparing two raw shared-secret strings (the old AGENT_DEMO_TOKEN /
AGENT_APPROVAL_TOKEN model, which could only ever represent exactly one user
and one reviewer) with signed, expiring tokens carrying a real subject and
role. This is still a single-node, shared-secret scheme — one HS256 key,
no revocation list, no external IdP — not production OAuth/OIDC.
"""
import os
import time
from typing import Dict

import jwt

ROLES = {"user", "reviewer", "admin"}
ALGORITHM = "HS256"


class AuthError(Exception):
    """Any token problem: missing secret, bad signature, expired, bad claims."""


def _secret() -> str:
    secret = os.getenv("AGENT_JWT_SECRET", "")
    if not secret:
        raise AuthError("AGENT_JWT_SECRET not configured")
    return secret


def mint_token(subject: str, role: str, ttl_seconds: int = 3600) -> str:
    if role not in ROLES:
        raise AuthError("Unknown role: " + str(role))
    if not subject or not isinstance(subject, str):
        raise AuthError("subject required")
    if not isinstance(ttl_seconds, int) or not (0 < ttl_seconds <= 86400):
        raise AuthError("ttl_seconds must be between 1 and 86400")
    now = int(time.time())
    payload = {"sub": subject, "role": role, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> Dict[str, object]:
    if not token:
        raise AuthError("Missing token")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as ex:
        raise AuthError("Token expired") from ex
    except jwt.InvalidTokenError as ex:
        raise AuthError("Invalid token") from ex
    if payload.get("role") not in ROLES or not payload.get("sub"):
        raise AuthError("Malformed token claims")
    return payload


def subject_with_role(authorization: str, *allowed_roles: str) -> str:
    """Parse ``Authorization: Bearer <jwt>``, verify signature/expiry/claims,
    and check the role. Returns the subject on success."""
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise AuthError("Missing bearer token")
    payload = decode_token(authorization[len(prefix):])
    if payload["role"] not in allowed_roles:
        raise AuthError("Role not permitted for this route")
    return str(payload["sub"])
