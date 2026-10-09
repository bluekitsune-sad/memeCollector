"""URL navigation-guard tests (PRD §41) — no network, DNS monkeypatched.

Covers the three block classes (dangerous schemes/userinfo, non-global IP
literals, hosts that *resolve* to non-global addresses), the DNS fail-open
policy for offline/fixture hosts, and the structured ``key=value`` block log.
"""

from __future__ import annotations

import logging
import socket
from types import SimpleNamespace

import pytest

from backend.security import urls as urls_module
from backend.security.urls import (
    UnsafeURLError,
    ensure_safe_url,
    reject_url,
    validate_url,
    validate_url_with_dns,
)

#: A globally routable answer used whenever a test does not map the hostname.
PUBLIC_IP = "93.184.216.34"


def _patch_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, object]) -> None:
    """Route ``getaddrinfo`` through *mapping* (IP list or exception; default = public)."""

    def getaddrinfo(host: str, port: object = None, *args: object, **kwargs: object):
        answer = mapping.get(host, [PUBLIC_IP])
        if isinstance(answer, BaseException):
            raise answer
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in answer  # type: ignore[union-attr]
        ]

    monkeypatch.setattr(
        urls_module,
        "socket",
        SimpleNamespace(getaddrinfo=getaddrinfo, gaierror=socket.gaierror),
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "file:///etc/passwd",
        "ftp://example.com/payload.exe",
        "vbscript:msgbox(1)",
        "about:blank",
    ],
)
def test_dangerous_schemes_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(url)
    assert excinfo.value.reason.startswith("blocked scheme")
    assert excinfo.value.url == url


@pytest.mark.parametrize(
    "url",
    ["https://user:pass@example.com/", "https://admin@example.com/"],
)
def test_credentials_in_url_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(url)
    assert excinfo.value.reason == "credentials in url (userinfo)"


def test_empty_and_hostless_urls_are_blocked() -> None:
    for url, reason in (("", "empty url"), ("   ", "empty url"), ("https:///path", "missing host")):
        with pytest.raises(UnsafeURLError) as excinfo:
            validate_url(url)
        assert excinfo.value.reason == reason


def test_control_characters_in_url_are_blocked() -> None:
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url("https://exa\tmple.com/")
    assert excinfo.value.reason == "control or format character in url"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/x",
        "http://192.168.1.10/x",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://100.64.0.1/x",                        # carrier-grade NAT
        "http://0.0.0.0/x",
        "http://[::1]/x",
        "http://[fe80::1]/x",
        "http://[fd00:ec2::254]/",                    # IPv6 metadata-style address
        "http://[::ffff:127.0.0.1]/",                 # IPv4-mapped loopback
    ],
)
def test_non_global_addresses_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(url)
    assert excinfo.value.reason.startswith("blocked ip address")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/page?q=1",
        "http://cdn.example.com/media/img.png",
        "https://93.184.216.34/",
        "HTTPS://Example.COM/Path",
    ],
)
def test_public_urls_validate_unchanged(url: str) -> None:
    assert validate_url(url) == url


def test_validate_url_never_touches_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(*args: object, **kwargs: object):
        raise AssertionError("validate_url must not resolve DNS")

    monkeypatch.setattr(
        urls_module,
        "socket",
        SimpleNamespace(getaddrinfo=getaddrinfo, gaierror=socket.gaierror),
    )
    assert validate_url("https://offline-host.test/x") == "https://offline-host.test/x"


def test_dns_answer_to_private_address_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"metadata-host.test": ["169.254.169.254"]})
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url_with_dns("https://metadata-host.test/latest")
    assert excinfo.value.reason == "host resolves to blocked address"


def test_dns_answer_to_public_address_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"ok-host.test": [PUBLIC_IP]})
    url = "https://ok-host.test/x"
    assert validate_url_with_dns(url) == url


def test_unresolvable_host_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture/offline hostnames must keep working — DNS failure is not a block."""
    _patch_dns(monkeypatch, {"unknown-host.test": socket.gaierror(-2, "name not known")})
    url = "https://unknown-host.test/page"
    assert validate_url_with_dns(url) == url


async def test_ensure_safe_url_blocks_resolved_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dns(monkeypatch, {"internal-host.test": ["10.4.5.6"]})
    with pytest.raises(UnsafeURLError):
        await ensure_safe_url("https://internal-host.test/")


async def test_ensure_safe_url_allows_public_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dns(monkeypatch, {"public-host.test": [PUBLIC_IP]})
    url = "https://public-host.test/img.png"
    assert await ensure_safe_url(url) == url


def test_reject_url_logs_structured_key_value(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="memevault.security.urls")
    hostile_url = "https://bad.example/\x07steal"
    with pytest.raises(UnsafeURLError):
        reject_url(hostile_url, "test reason")
    assert "url blocked" in caplog.text
    assert "reason=test reason" in caplog.text
    assert "url=https://bad.example/steal" in caplog.text  # control char scrubbed
    assert "\x07" not in caplog.text
