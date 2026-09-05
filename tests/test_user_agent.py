"""The User-Agent regression tests — vikunja#657, and #53-#56 before it.

These are the tests whose absence let a wrong User-Agent ship twice. The old suite
mocked LibreChat with hand-authored fixtures and never once asserted what went out on
the wire, so the header could be anything at all and every test still passed.

**Why a real parser instead of a string comparison.** LibreChat's rule is not "the UA
equals string X", it is `ua-parser-js` resolving `ua.browser.name` to something truthy
(`api/server/middleware/uaParser.js`). A test asserting equality against the constant
would pass for any constant, including the next wrong one — it would re-test the same
nothing the old suite tested. So these apply the rule.

**The parser here is not the parser LibreChat runs, and that is stated rather than
glossed.** `ua-parser` (Python, uap-core regexes) and `ua-parser-js` (JavaScript,
faisalman's own corpus) are independent implementations. This corroborates the live
measurement, it does not replicate it. What makes it load-bearing anyway is the
negative control below: the three UAs measured as REJECTED by the live server on
2026-09-05 also resolve to no browser here, so this parser can actually tell the
cases apart. Without that control a green result would mean nothing.
"""

from __future__ import annotations

import pathlib
import re

import httpx
import pytest
import respx
from ua_parser import parse

import librechat_mcp.client as client_mod
from librechat_mcp import __version__
from librechat_mcp.client import USER_AGENT

from .conftest import BASE_URL

# Measured against the live LibreChat instance on 2026-09-05 (v0.8.8-rc2, and
# identically on v0.8.5). Each returned HTTP 200, no content-type header, and the
# body `event: error\ndata: {"message":"Illegal request"}`.
REJECTED_USER_AGENTS = [
    pytest.param("Mozilla/5.0 (compatible; librechat-mcp/0.1.0)", id="what-0.1.1-sent"),
    pytest.param("python-httpx/0.28.1", id="httpx-default"),
    pytest.param("", id="no-ua-at-all"),
]


def _browser_name(user_agent: str) -> str | None:
    """The one value LibreChat's middleware actually tests: `ua.browser.name`."""
    result = parse(user_agent)
    return result.user_agent.family if result.user_agent else None


# ---------------------------------------------------------------------------
# The negative control comes first, because the positive test depends on it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rejected_ua", REJECTED_USER_AGENTS)
def test_control_the_rejected_user_agents_resolve_to_no_browser(rejected_ua):
    """Establish that this parser can distinguish the cases at all.

    If any of the three UAs the live server rejected resolved to a browser name
    here, the parser would not be modelling LibreChat's rule and every assertion in
    this file would be green regardless of what we send.
    """
    assert _browser_name(rejected_ua) is None


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_user_agent_resolves_to_a_browser_name():
    """The property LibreChat requires — a truthy `browser.name`, not a literal."""
    assert _browser_name(USER_AGENT)


def test_user_agent_is_not_one_of_the_rejected_strings():
    assert USER_AGENT not in [p.values[0] for p in REJECTED_USER_AGENTS]


# ---------------------------------------------------------------------------
# Version single-sourcing
# ---------------------------------------------------------------------------


def test_user_agent_carries_the_package_version():
    assert f"librechat-mcp/{__version__}" in USER_AGENT


def test_version_is_not_written_out_a_second_time():
    """0.1.1 hardcoded `0.1.0` into the UA and it drifted the same release.

    This greps the source rather than comparing values: comparing the UA's version
    against `__version__` passes whether or not a literal is present, because today
    they agree. The literal is the defect, so the literal is what is asserted on.
    """
    source = pathlib.Path(client_mod.__file__).read_text()
    hardcoded = re.findall(r"librechat-mcp/\d+\.\d+[.\d]*", source)
    # The comment block quoting the historical `librechat-mcp/0.1.0` is allowed; a
    # version literal in code is not. Strip comments before looking.
    code_only = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"librechat-mcp/\d+\.\d+", code_only), (
        f"version literal in code, not derived from __version__: {hardcoded}"
    )


# ---------------------------------------------------------------------------
# What actually goes out on the wire — the assertion the old suite never made
# ---------------------------------------------------------------------------


async def test_outgoing_request_sends_a_parseable_browser_user_agent(authed_client):
    """Apply LibreChat's rule to the header this client really transmits.

    Not to the constant — to `route.calls.last.request.headers`. A constant can be
    correct while the client sends something else entirely (a per-request header
    override, a default that never made it onto the AsyncClient), and that gap is
    exactly the shape of the original defect.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [], "has_more": False})
        )
        await authed_client.request("GET", "/api/agents")

    sent = route.calls.last.request.headers["user-agent"]
    assert sent == USER_AGENT
    assert _browser_name(sent), f"transmitted UA resolves to no browser: {sent!r}"


async def test_login_request_also_sends_the_browser_user_agent(authed_client):
    """/api/auth/login is NOT uaParser-guarded, which is why this was invisible.

    Login succeeded throughout the outage while every guarded route failed, so the
    breakage read as a data bug rather than an auth one. The header is set on the
    AsyncClient and so rides on login too; asserting it here pins that, and means a
    future change that moves the UA to a per-call override cannot regress silently.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": "jwt-abc"})
        )
        authed_client._jwt = None
        authed_client._jwt_expires_at = None
        await authed_client._get_jwt()

    assert _browser_name(route.calls.last.request.headers["user-agent"])
