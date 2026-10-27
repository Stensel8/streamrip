"""The browser streamrip presents itself as: coherent, current, and every
so often a slightly different one."""

import re

import pytest

from streamrip.client import client as client_module
from streamrip.client.client import (
    _IDENTITIES,
    _sec_ch_ua,
    api_headers,
    browser_headers,
    new_session,
    register_header_dict,
)

# What Chromium 155, 156 and 157 send, read from measured captures of real
# builds (157 computed with Chromium's own recipe, which reproduces the
# measured versions exactly).
KNOWN_SEC_CH_UA = {
    155: '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    156: '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    157: '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
}


class Clock:
    """A fake `time` module whose monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 10_000.0

    def monotonic(self):
        return self.now


@pytest.fixture(autouse=True)
def identity_state(monkeypatch):
    """A fresh identity, no deadline served yet, nothing registered."""
    monkeypatch.setattr(client_module, "_identity", {})
    monkeypatch.setattr(client_module, "_rotate_at", 0.0)
    monkeypatch.setattr(client_module, "_header_dicts", [])


@pytest.fixture
def clock(monkeypatch, identity_state):
    clock = Clock()
    monkeypatch.setattr(client_module, "time", clock)
    return clock


@pytest.mark.parametrize("major,expected", sorted(KNOWN_SEC_CH_UA.items()))
def test_sec_ch_ua_is_chromiums_for_that_version(major, expected):
    assert _sec_ch_ua(major) == expected


def test_brave_puts_its_own_brand_in_the_same_list():
    assert _sec_ch_ua(156, brand="Brave") == (
        '"Not:A-Brand";v="8", "Chromium";v="156", "Brave";v="156"'
    )


@pytest.mark.parametrize("identity", _IDENTITIES)
def test_every_identity_agrees_with_itself(identity):
    """A Windows user agent never claims macOS in its hints, and the brand
    list follows the user agent's version: a contradiction between the two
    is exactly what gives a fake browser away."""
    match = re.fullmatch(
        r"Mozilla/5\.0 \(([^)]+)\) AppleWebKit/537\.36 \(KHTML, like Gecko\) "
        r"Chrome/(\d+)\.0\.0\.0 Safari/537\.36",
        identity["User-Agent"],
    )
    assert match is not None
    os_token, major = match.group(1), int(match.group(2))

    token_to_platform = {
        token: platform
        for platform, token in client_module._UA_PLATFORMS.items()
    }
    platform = token_to_platform[os_token]
    assert identity["Sec-CH-UA-Platform"] == f'"{platform}"'
    # Brave's user agent is Chrome's; only its brand in the list differs.
    assert identity["Sec-CH-UA"] in (_sec_ch_ua(major), _sec_ch_ua(major, "Brave"))
    assert identity["Sec-CH-UA-Mobile"] == "?0"
    assert re.fullmatch(
        r"[a-z]{2,3}(-[A-Z]{2})?(,[a-z]{2,3}(-[A-Z]{2})?(;q=[0-9.]+)?)*",
        identity["Accept-Language"],
    )
    assert set(identity) == {
        "User-Agent",
        "Sec-CH-UA",
        "Sec-CH-UA-Mobile",
        "Sec-CH-UA-Platform",
        "Accept-Language",
    }


def test_the_current_identity_is_one_from_the_pool():
    assert browser_headers() in _IDENTITIES


async def test_a_session_presents_the_current_identity():
    async with new_session() as session:
        assert dict(session.headers) == browser_headers()


async def test_an_explicit_user_agent_replaces_the_identity():
    async with new_session(user_agent="streamrip/test") as session:
        assert dict(session.headers) == {"User-Agent": "streamrip/test"}


async def test_api_calls_carry_the_fetch_headers_downloads_do_not():
    headers = api_headers({"authorization": "Bearer t"})

    assert headers["Sec-Fetch-Site"] == "same-site"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["authorization"] == "Bearer t"
    assert set(browser_headers()).isdisjoint({"Sec-Fetch-Site"})


async def test_sessions_follow_the_identity_as_it_rotates(clock):
    async with (
        new_session() as followed,
        new_session(user_agent="streamrip/test") as own_agent,
    ):
        before = dict(followed.headers)

        clock.now += 3 * 60 * 60  # past any identity's time

        assert browser_headers() != before
        assert dict(followed.headers) == browser_headers()
        assert dict(own_agent.headers) == {"User-Agent": "streamrip/test"}


def test_a_registered_header_dict_follows_but_keeps_its_own_language(clock):
    headers = {"User-Agent": "Chrome/79", "Accept-Language": "fr_FR"}
    register_header_dict(headers)

    clock.now += 3 * 60 * 60

    assert headers.pop("Accept-Language") == "fr_FR"
    assert headers == {
        k: v for k, v in api_headers().items() if k != "Accept-Language"
    }


def test_a_rotation_picks_another_identity_from_the_pool(clock):
    first = browser_headers()

    clock.now += 3 * 60 * 60

    assert (second := browser_headers()) != first
    assert second in _IDENTITIES
