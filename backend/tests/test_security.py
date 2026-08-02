from __future__ import annotations

import socket

import pytest

from app.core.security import is_https_url, is_safe_url


@pytest.mark.parametrize(
    "url,expected_reason_substring",
    [
        ("https://127.0.0.1/", "loopback"),
        ("https://169.254.169.254/metadata/identity/oauth2/token", "link-local"),
        ("https://10.0.0.5/", "private"),
        ("https://172.16.0.1/", "private"),
        ("https://192.168.1.1/", "private"),
        ("https://[::1]/", "loopback"),
    ],
)
def test_blocks_dangerous_ip_ranges(url: str, expected_reason_substring: str) -> None:
    safe, reason = is_safe_url(url)
    assert safe is False
    assert reason is not None
    assert expected_reason_substring in reason


def test_allows_public_ip() -> None:
    # IP literal, so no real DNS lookup is required — keeps this test
    # network-independent in CI.
    safe, reason = is_safe_url("https://8.8.8.8/")
    assert safe is True
    assert reason is None


def test_rejects_unresolvable_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    safe, reason = is_safe_url("https://this-should-not-resolve.invalid/")
    assert safe is False
    assert reason == "hostname did not resolve"


def test_rejects_url_with_no_hostname() -> None:
    safe, reason = is_safe_url("not-a-url")
    assert safe is False
    assert reason == "no hostname in URL"


def test_is_https_url() -> None:
    assert is_https_url("https://example.com") is True
    assert is_https_url("http://example.com") is False
    assert is_https_url("ftp://example.com") is False
