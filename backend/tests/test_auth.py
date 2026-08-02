"""The local-dev auth bypass, and the condition that must keep it shut.

`AUTH_DISABLED` exists so the API is runnable before Clerk is configured —
`/docs`, curl, and the router tests all need a user id without a browser
session. It is deliberately not sufficient on its own: the bypass also
requires `ENV != production`, so setting the flag on a production deployment
does nothing.

That second condition is one `and` in one expression, which is precisely the
kind of thing a later refactor drops while "simplifying" — and dropping it
would silently make every protected route anonymous in production. Hence
these tests.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.core.auth import current_user_id


def _settings(**overrides: object) -> Settings:
    # _env_file=None: these assertions are about the flag, not about whichever
    # .env happens to sit next to the checkout.
    return Settings(_env_file=None, **overrides)


async def test_bypass_returns_the_dev_user_when_enabled_outside_production() -> None:
    user_id = await current_user_id(
        authorization=None,
        settings=_settings(AUTH_DISABLED=True, ENV="development", AUTH_DEV_USER_ID="dev-user"),
    )
    assert user_id == "dev-user"


async def test_bypass_is_ignored_in_production_even_when_the_flag_is_set() -> None:
    """The security property worth pinning: flag on, production on, still 401.

    A deployment that ships with AUTH_DISABLED=true by accident must not be
    open. `ENV=production` is what makes the flag inert.
    """
    with pytest.raises(HTTPException) as exc:
        await current_user_id(
            authorization=None,
            settings=_settings(AUTH_DISABLED=True, ENV="production"),
        )
    assert exc.value.status_code == 401


async def test_missing_bearer_token_is_rejected_when_the_bypass_is_off() -> None:
    with pytest.raises(HTTPException) as exc:
        await current_user_id(authorization=None, settings=_settings(AUTH_DISABLED=False))
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "",                       # empty
        "abc.def.ghi",            # a token, but no scheme
        "Basic dXNlcjpwYXNz",     # wrong scheme
        # Lowercase is rejected because the check is a literal startswith.
        # RFC 7235 makes the scheme case-insensitive, so this is stricter than
        # the spec; it's pinned as current behaviour, not defended as correct.
        # Clerk always sends "Bearer", so no real client is affected.
        "bearer abc.def.ghi",
    ],
)
async def test_malformed_authorization_headers_are_rejected(header: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await current_user_id(authorization=header, settings=_settings(AUTH_DISABLED=False))
    assert exc.value.status_code == 401


async def test_a_garbage_bearer_token_is_rejected_without_reaching_the_network() -> None:
    """An unparseable token must fail at the decode step, not at a JWKS fetch —
    otherwise every junk request costs an outbound call to Clerk."""
    with pytest.raises(HTTPException) as exc:
        await current_user_id(
            authorization="Bearer not-a-jwt", settings=_settings(AUTH_DISABLED=False)
        )
    assert exc.value.status_code == 401
