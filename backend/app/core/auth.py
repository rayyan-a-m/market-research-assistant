"""Clerk authentication.

Clerk issues a session JWT to the signed-in frontend; the frontend sends
it as `Authorization: Bearer <jwt>`. We verify the signature against
Clerk's published JWKS (the `iss` claim tells us where to fetch it) and
return the `sub` claim — the Clerk user id used for ownership everywhere.

JWKS-based verification is deliberate: it depends only on the standard
JWT/JWKS contract, not on a specific Clerk SDK version, and needs no
secret on the backend (the signing keys are public). The keys are cached
per issuer so we don't refetch on every request.

Local-dev bypass: when `AUTH_DISABLED=true` AND `ENV != production`, the
dependency returns a fixed dev user id without a token — so the app is
runnable locally before Clerk is configured. The bypass is hard-disabled
in production regardless of the flag.
"""

from __future__ import annotations

import jwt
from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings

# One PyJWKClient per issuer, cached across requests (each holds its own
# short-lived key cache).
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _jwks_client_for(issuer: str) -> jwt.PyJWKClient:
    client = _jwks_clients.get(issuer)
    if client is None:
        client = jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json")
        _jwks_clients[issuer] = client
    return client


def _verify_clerk_jwt(token: str) -> str:
    """Verify a Clerk session JWT and return its `sub` (user id).
    Raises 401 on any failure."""
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        issuer = unverified.get("iss")
        if not issuer:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing issuer")

        signing_key = _jwks_client_for(issuer).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            # Clerk session tokens don't set a fixed `aud`; issuer + signature
            # + expiry are the checks that matter here.
            options={"verify_aud": False},
        )
    except HTTPException:
        raise
    except Exception as exc:  # jwt.* errors, network, malformed token
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing subject")
    return str(sub)


async def current_user_id(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency: resolves the authenticated Clerk user id, or
    401s. Returns the dev user id when the local bypass is active."""
    if settings.auth_disabled and settings.env != "production":
        return settings.auth_dev_user_id

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    return _verify_clerk_jwt(token)
