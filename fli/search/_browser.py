r"""Optional browser-backed transport: URL in, RPC response bytes out.

This is the seam described in ADR 001
(``docs/decisions/001-optional-browser-backed-transport.md``). It exists
because ``GetShoppingResults`` and ``GetExploreDestinations`` are gated behind
an ``x-goog-batchexecute-bgr`` header that Google's own batchexecute client
produces. fli cannot synthesise that header, so for those requests the only
source of truth is a real browser making the request itself. This module
drives one, intercepts the response, and hands back the raw body.

**This is the only module under ``fli/`` that knows Playwright exists**, and
that containment is the maintenance posture the ADR committed to. Two rules
keep it true and both are enforced by ``tests/test_dependency_declarations.py``:

* every ``playwright`` import is lazy, inside a function body (the
  ``TYPE_CHECKING`` block is erased at runtime), so importing :mod:`fli` on an
  install without the ``browser`` extra works normally;
* nothing above this module imports ``playwright``, and nothing in this module
  imports :mod:`fli.models`, :mod:`fli.search._decoders` or
  :mod:`fli.search._tfs`. The contract is a URL in and bytes out. That is why
  :func:`capture_rpc_body` is generic over ``rpc_marker``: a future Explore
  consumer passes ``"GetExploreDestinations"`` and an Explore page URL, and
  nothing here changes.

Consent, stated as a hard rule
------------------------------
A fresh profile is redirected to ``consent.google.com`` and the app never
loads, so the interstitial must be dealt with. fli dismisses it **only** in a
profile fli created, and **only** by clicking "Reject all" — the option that
takes the least on the user's behalf. In a browser the user attached us to
(``cdp_endpoint`` set), fli never clicks: that profile is theirs, the choice
persists into their real browsing, and being the privacy-preserving click does
not transfer the authority to make it.

**If the "Reject all" control cannot be found, fli fails.** It does not click
"Accept all" and it does not click the first button it sees. A consent dialog
whose contents fli cannot read is precisely the situation in which it must not
press anything. Do not add a fallback click to make this "more robust".
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from fli.search.exceptions import (
    BrowserConsentRequiredError,
    BrowserRpcTimeoutError,
    BrowserTransportUnavailableError,
    BrowserUnreachableError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; playwright is optional
    from playwright.sync_api import BrowserContext, Page, Playwright, Response

logger = logging.getLogger(__name__)

#: The host Google redirects a consent-less profile to.
CONSENT_HOST = "consent.google.com"

#: The only consent control fli will ever click, case-insensitively.
_REJECT_ALL = re.compile(r"^reject all$", re.I)

#: What counts as an RPC worth recording for diagnosis. Deliberately narrow:
#: matching the bare string ``FlightsFrontendUi`` also matches the page's
#: gstatic JavaScript bundles, which are not RPCs at all — measured live, that
#: turned one real call into a reported "23 RPCs" and pointed the diagnosis in
#: exactly the wrong direction. An RPC is a batchexecute call, or something
#: served from Google's own ``/_/FlightsFrontendUi/`` path.
_RPC_URL_MARKERS = ("/batchexecute", "google.com/_/FlightsFrontendUi/")

#: Default home for the profile fli launches itself.
DEFAULT_PROFILE_DIR = Path.home() / ".fli" / "browser-profile"

#: Both halves of the install, because a user who is missing the package will
#: need both. Kept as one string so every message spells it identically.
INSTALL_HINT = 'run `uv add "flights[browser]"` and then `playwright install chromium`'


class _ConsentInterstitial(Exception):
    """Internal signal: navigation landed on the consent wall.

    Raised inside the ``expect_response`` block so the wait is cancelled
    immediately rather than running out the full timeout on a page that will
    never issue the RPC.
    """


class BrowserOptions(BaseModel):
    """How to obtain a browser, and how patient to be with it.

    Attributes:
        cdp_endpoint: Attach to an already-running Chrome at this endpoint
            (e.g. ``http://127.0.0.1:9222``) instead of launching one.
            Setting it is an instruction, not a hint: if the endpoint refuses
            the connection fli raises rather than quietly launching a second
            browser the user never asked for and would never see.
        profile_dir: Directory for the persistent profile fli launches when
            no endpoint is configured. Consent, once dismissed, persists here.
        headless: Launch without a visible window. Whether headless Chrome
            clears Google's attestation as reliably as headed Chrome is
            unverified — see the ADR's open risks.
        timeout_ms: How long to wait for the RPC, per attempt.
        consent: ``"reject"`` dismisses Google's cookie interstitial with
            "Reject all" in fli's own profile; ``"never"`` makes fli raise on
            the interstitial in every case, including its own profile.

    """

    model_config = ConfigDict(frozen=True)

    cdp_endpoint: str | None = None
    profile_dir: Path = Field(default_factory=lambda: DEFAULT_PROFILE_DIR)
    headless: bool = True
    timeout_ms: int = 45_000
    consent: Literal["reject", "never"] = "reject"

    @classmethod
    def from_env(cls) -> BrowserOptions:
        """Build options from the ``FLI_BROWSER_*`` environment variables.

        A plain classmethod rather than ``pydantic-settings``, which is
        confined to the ``mcp`` extra and must not become a core dependency
        to serve an optional one.

        Reads ``FLI_BROWSER_CDP_ENDPOINT``, ``FLI_BROWSER_PROFILE_DIR``,
        ``FLI_BROWSER_HEADLESS``, ``FLI_BROWSER_TIMEOUT_MS`` and
        ``FLI_BROWSER_CONSENT``. Every one is optional.

        Returns:
            The resolved options.

        Raises:
            ValueError: A variable is set to a value that cannot be read.

        """
        values: dict[str, Any] = {}
        endpoint = os.environ.get("FLI_BROWSER_CDP_ENDPOINT")
        if endpoint:
            values["cdp_endpoint"] = endpoint
        profile = os.environ.get("FLI_BROWSER_PROFILE_DIR")
        if profile:
            values["profile_dir"] = Path(profile).expanduser()
        headless = os.environ.get("FLI_BROWSER_HEADLESS")
        if headless is not None:
            values["headless"] = _as_bool("FLI_BROWSER_HEADLESS", headless)
        timeout = os.environ.get("FLI_BROWSER_TIMEOUT_MS")
        if timeout:
            values["timeout_ms"] = _as_int("FLI_BROWSER_TIMEOUT_MS", timeout)
        consent = os.environ.get("FLI_BROWSER_CONSENT")
        if consent:
            if consent.strip().lower() not in ("reject", "never"):
                raise ValueError(
                    f"FLI_BROWSER_CONSENT must be 'reject' or 'never', got {consent!r}"
                )
            values["consent"] = consent.strip().lower()
        return cls(**values)


class RpcCapture(BaseModel):
    """One intercepted RPC response, plus what it took to get it.

    Attributes:
        body: The raw response body, exactly as it came off the wire. It is
            the caller's job to decode it — this module never does.
        rpc_urls: Every Flights RPC URL the page requested, in order.
            Diagnostics, not data: they are what makes a miss explicable.
        nudged: Whether the RPC only fired after fli clicked the page's
            search control, which happens when Google serves the board from
            cache on first load.

    """

    body: bytes
    rpc_urls: list[str] = Field(default_factory=list)
    nudged: bool = False


def browser_available() -> bool:
    """Report whether the ``browser`` extra is installed.

    Uses :func:`importlib.util.find_spec`, which locates the package without
    executing it — importing Playwright to ask whether Playwright is
    importable costs real time on every call that does not need it.

    Returns:
        ``True`` when ``playwright`` can be imported.

    """
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def capture_rpc_body(
    url: str,
    *,
    rpc_marker: str,
    options: BrowserOptions | None = None,
) -> RpcCapture:
    """Drive a browser to ``url`` and return the body of the matching RPC.

    Interception uses ``page.expect_response`` rather than a bare
    ``page.on("response")`` handler: Playwright's sync API forbids reading a
    body from inside an event handler, while ``expect_response`` hands back a
    response whose body can be read once the wait returns. A passive handler
    runs alongside it recording URLs only, so a miss can be diagnosed.

    Google sometimes serves the board without re-issuing the RPC. When the
    first wait times out the page is nudged once, by clicking its search
    control, and the wait is retried. There is no retry beyond that: this
    transport does not draw on the HTTP client's rate limiter and must not
    bypass it either, and re-firing a request a gated service just declined
    is the behaviour the gate exists to punish.

    Args:
        url: The page to load — a Google Flights search-page URL.
        rpc_marker: Substring identifying the wanted RPC among the page's
            many responses, e.g.
            ``"FlightsFrontendService/GetShoppingResults"``.
        options: Browser settings. Defaults to :meth:`BrowserOptions.from_env`.

    Returns:
        The captured body and its diagnostics.

    Raises:
        BrowserTransportUnavailableError: Playwright, or its Chromium binary,
            is not installed.
        BrowserUnreachableError: No browser could be attached to or launched.
        BrowserConsentRequiredError: The consent interstitial blocked the page
            and fli would not dismiss it itself.
        BrowserRpcTimeoutError: The RPC never fired, nudge included.

    """
    options = options or BrowserOptions.from_env()
    _require_playwright()

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    rpc_urls: list[str] = []
    with sync_playwright() as playwright:
        with _browser_context(playwright, options) as context:
            page = context.new_page()
            page.on("response", lambda response: _record_rpc_url(rpc_urls, response))
            try:
                body, nudged = _capture(
                    page,
                    url,
                    rpc_marker=rpc_marker,
                    options=options,
                    timeout_error=PlaywrightTimeoutError,
                    rpc_urls=rpc_urls,
                )
            finally:
                page.close()

    return RpcCapture(body=body, rpc_urls=rpc_urls, nudged=nudged)


# ---------------------------------------------------------------------------
# Obtaining a browser
# ---------------------------------------------------------------------------


def _require_playwright() -> None:
    """Fail with an actionable message when the ``browser`` extra is absent.

    Raises:
        BrowserTransportUnavailableError: ``playwright`` is not importable.

    """
    if browser_available():
        return
    raise BrowserTransportUnavailableError(
        "This search needs the optional browser transport, which is not "
        f"installed. To enable it, {INSTALL_HINT}. The default install of "
        "fli deliberately ships no browser."
    )


@contextmanager
def _browser_context(playwright: Playwright, options: BrowserOptions) -> Iterator[BrowserContext]:
    """Yield a browser context, closing only what this function created.

    Over CDP the browser and its default context belong to the user; closing
    either would take their Chrome down with it. That ownership distinction
    is the reason this is a context manager rather than two calls.

    Args:
        playwright: An entered ``sync_playwright()`` instance.
        options: Browser settings.

    Yields:
        A usable browser context.

    Raises:
        BrowserUnreachableError: The endpoint refused the connection, or a
            browser could not be launched.
        BrowserTransportUnavailableError: Playwright is installed but its
            Chromium binary is not.

    """
    if options.cdp_endpoint:
        browser = _connect_over_cdp(playwright, options.cdp_endpoint)
        # An attached Chrome always has a default context. Reuse it so the
        # page inherits the profile (and its dismissed consent); fall back to
        # a fresh one only if there is somehow none, and own that one.
        if browser.contexts:
            yield browser.contexts[0]
            return
        context = browser.new_context()
        try:
            yield context
        finally:
            context.close()
        return

    context = _launch_persistent(playwright, options)
    try:
        yield context
    finally:
        context.close()


def _connect_over_cdp(playwright: Playwright, endpoint: str) -> Any:
    """Attach to a Chrome already listening for CDP.

    Args:
        playwright: An entered ``sync_playwright()`` instance.
        endpoint: The CDP endpoint, e.g. ``http://127.0.0.1:9222``.

    Returns:
        The connected browser.

    Raises:
        BrowserUnreachableError: The connection was refused.

    """
    try:
        return playwright.chromium.connect_over_cdp(endpoint)
    except Exception as exc:  # noqa: BLE001 - playwright raises a wide family here
        raise BrowserUnreachableError(
            f"Could not attach to a browser at {endpoint}. Start Chrome with "
            f"--remote-debugging-port={_port_of(endpoint)} and a --user-data-dir of its "
            "own, then try again. fli will not launch a browser instead: an "
            "explicitly configured endpoint is an instruction, not a hint, and "
            "silently launching would start a second browser you never see. "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _launch_persistent(playwright: Playwright, options: BrowserOptions) -> BrowserContext:
    """Launch fli's own persistent browser profile.

    Persistent rather than throwaway so a dismissed consent survives to the
    next run: the first run pays the interstitial, later runs do not.

    Args:
        playwright: An entered ``sync_playwright()`` instance.
        options: Browser settings.

    Returns:
        The launched context.

    Raises:
        BrowserTransportUnavailableError: The Chromium binary is missing.
        BrowserUnreachableError: The launch failed for any other reason,
            commonly a sandbox.

    """
    fresh = not options.profile_dir.exists()
    try:
        context = playwright.chromium.launch_persistent_context(
            str(options.profile_dir),
            headless=options.headless,
        )
    except Exception as exc:  # noqa: BLE001 - classified below, then re-raised
        raise _classify_launch_failure(exc, options) from exc
    if fresh:
        logger.info(
            "Created a new browser profile at %s. It is signed out and used "
            "only by fli; delete the directory to start over.",
            options.profile_dir,
        )
    return context


def _classify_launch_failure(exc: Exception, options: BrowserOptions) -> Exception:
    """Turn a raw launch failure into a diagnosis.

    Three causes look identical in a traceback and need different fixes: a
    missing Chromium download, a sandbox refusing to let the process spawn
    one, and everything else.

    Args:
        exc: The exception Playwright raised.
        options: Browser settings, for naming the profile in the message.

    Returns:
        The exception to raise from *exc*.

    """
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return BrowserTransportUnavailableError(
            "Playwright is installed but its Chromium browser is not. Run "
            "`playwright install chromium` — the wheel is not the browser, so "
            "even a `flights[all]` install needs this step. Playwright "
            f"reported: {text.strip().splitlines()[0] if text.strip() else exc!r}"
        )
    sandboxed = (
        "TargetClosedError" in type(exc).__name__
        or "EPERM" in text
        or "Operation not permitted" in text
    )
    if sandboxed:
        return BrowserUnreachableError(
            "fli could not spawn a browser. This is usually a sandbox or "
            "seccomp restriction rather than a broken install — the process "
            "was not allowed to start Chromium. Start Chrome yourself with "
            "--remote-debugging-port=9222 and set "
            "FLI_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222 so fli attaches "
            f"to it instead. ({type(exc).__name__}: {exc})"
        )
    return BrowserUnreachableError(
        f"Could not launch a browser with profile {options.profile_dir}. "
        f"({type(exc).__name__}: {exc})"
    )


# ---------------------------------------------------------------------------
# Capturing the RPC
# ---------------------------------------------------------------------------


def _capture(
    page: Page,
    url: str,
    *,
    rpc_marker: str,
    options: BrowserOptions,
    timeout_error: type[Exception],
    rpc_urls: list[str],
) -> tuple[bytes, bool]:
    """Navigate, handle consent if it appears, and wait for the RPC.

    Args:
        page: The page to drive.
        url: Where to navigate.
        rpc_marker: Substring identifying the wanted response.
        options: Browser settings.
        timeout_error: Playwright's ``TimeoutError``, passed in so this
            module never imports playwright at module scope.
        rpc_urls: The live list of observed RPC URLs, for the timeout
            message.

    Returns:
        ``(body, nudged)``.

    Raises:
        BrowserConsentRequiredError: Consent blocked the page and fli would
            not, or could not, dismiss it.
        BrowserRpcTimeoutError: The RPC never fired, nudge included.

    """
    try:
        response = _try_navigate(page, url, rpc_marker, options.timeout_ms, timeout_error)
    except _ConsentInterstitial:
        _handle_consent(page, options)
        # The original navigation was consumed by the redirect, so the whole
        # wait restarts from zero rather than sharing a window across it.
        try:
            response = _try_navigate(page, url, rpc_marker, options.timeout_ms, timeout_error)
        except _ConsentInterstitial as exc:
            raise BrowserConsentRequiredError(
                "Google's consent page came back after fli dismissed it. The "
                f"choice is not persisting in {options.profile_dir}; check the "
                "directory is writable, or open the page in that profile and "
                "dismiss the notice by hand."
            ) from exc

    nudged = False
    if response is None:
        nudged = True
        response = _nudge(page, rpc_marker, options.timeout_ms, timeout_error)

    if response is None:
        raise BrowserRpcTimeoutError(
            _timeout_message(rpc_marker, options, rpc_urls), rpc_urls=rpc_urls
        )

    return response.body(), nudged


def _try_navigate(
    page: Page,
    url: str,
    rpc_marker: str,
    timeout_ms: int,
    timeout_error: type[Exception],
) -> Response | None:
    """Navigate with the RPC wait already armed.

    The consent check runs *inside* the wait block deliberately. Playwright
    cancels the wait when the block raises, so landing on the interstitial is
    detected in the second it takes to redirect instead of costing a full
    ``timeout_ms`` on a page that will never issue the RPC.

    Args:
        page: The page to drive.
        url: Where to navigate.
        rpc_marker: Substring identifying the wanted response.
        timeout_ms: How long to wait.
        timeout_error: Playwright's ``TimeoutError``.

    Returns:
        The matching response, or ``None`` if the wait timed out.

    Raises:
        _ConsentInterstitial: Navigation landed on the consent wall.

    """
    try:
        with page.expect_response(
            lambda response: rpc_marker in response.url, timeout=timeout_ms
        ) as info:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if is_consent_url(page.url):
                raise _ConsentInterstitial(page.url)
        return info.value
    except timeout_error:
        logger.debug("RPC %s not seen during page load", rpc_marker)
        return None


def _nudge(
    page: Page,
    rpc_marker: str,
    timeout_ms: int,
    timeout_error: type[Exception],
) -> Response | None:
    """Click the page's search control once and wait for the RPC again.

    Google sometimes renders the board from cache without re-issuing the RPC.
    Re-running the search in the page it is already showing is the cheapest
    way to make it ask again, and is the only retry this transport performs.

    Args:
        page: The page to drive.
        rpc_marker: Substring identifying the wanted response.
        timeout_ms: How long to wait.
        timeout_error: Playwright's ``TimeoutError``.

    Returns:
        The matching response, or ``None``.

    """
    logger.warning("RPC not seen during page load; nudging the search control and retrying")
    try:
        with page.expect_response(
            lambda response: rpc_marker in response.url, timeout=timeout_ms
        ) as info:
            page.get_by_role("button", name="Search").first.click(timeout=timeout_ms)
        return info.value
    except timeout_error:
        logger.debug("nudge produced no %s either", rpc_marker)
        return None
    except Exception as exc:  # noqa: BLE001 - a nudge failing is a diagnosis, not a crash
        logger.warning("could not nudge the search control: %s", exc)
        return None


def _timeout_message(rpc_marker: str, options: BrowserOptions, rpc_urls: list[str]) -> str:
    """Compose the timeout message, including what the page *did* request.

    Args:
        rpc_marker: The RPC that never arrived.
        options: Browser settings, for naming the timeout that elapsed.
        rpc_urls: Observed Flights RPC URLs.

    Returns:
        A message that says what to check next.

    """
    seconds = options.timeout_ms / 1000
    if not rpc_urls:
        diagnosis = (
            "The page requested no Flights RPCs at all, so it never "
            "got going — check network access, the consent state of the "
            "profile, and that the URL is a Google Flights search page."
        )
    else:
        diagnosis = (
            f"The page requested {len(rpc_urls)} other Flights RPC(s) "
            "but never this one, which usually means Google served a cached "
            "board and the search-control nudge did not re-run the search."
        )
    return (
        f"The browser loaded the page but never issued {rpc_marker} within "
        f"{seconds:g}s (nudge included). {diagnosis} Raise FLI_BROWSER_TIMEOUT_MS "
        "if the connection is simply slow."
    )


def is_rpc_url(url: str) -> bool:
    """Report whether a URL is a Flights RPC rather than a page asset.

    Args:
        url: A URL the page requested.

    Returns:
        ``True`` when it is worth recording for diagnosis.

    """
    return any(marker in url for marker in _RPC_URL_MARKERS)


def _record_rpc_url(rpc_urls: list[str], response: Response) -> None:
    """Record a Flights RPC URL; never read a body from a handler.

    Args:
        rpc_urls: The list to append to.
        response: The response the page just received.

    """
    if is_rpc_url(response.url):
        rpc_urls.append(response.url)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


def is_consent_url(url: str) -> bool:
    """Report whether a URL is Google's cookie-consent interstitial.

    Matches on the host rather than the page text, because the text is
    localised and the redirect is not.

    Args:
        url: The URL the page currently shows.

    Returns:
        ``True`` when the page is the consent wall.

    """
    return (urlparse(url).hostname or "").lower() == CONSENT_HOST


def _handle_consent(page: Page, options: BrowserOptions) -> None:
    """Dismiss the consent interstitial, or explain why fli will not.

    See the module docstring for the rule this enforces. In short: fli clicks
    "Reject all" in a profile fli created, and nothing at all anywhere else.

    Args:
        page: The page showing the interstitial.
        options: Browser settings.

    Raises:
        BrowserConsentRequiredError: fli would not click, or could not find
            the one control it is willing to click.

    """
    if options.consent == "never":
        raise BrowserConsentRequiredError(
            "Google's cookie-consent page is blocking the search and "
            "FLI_BROWSER_CONSENT=never forbids fli from interacting with it. "
            f"Open https://www.google.com/travel/flights in the profile at "
            f"{options.profile_dir}, dismiss the notice yourself, and re-run; "
            "the choice persists in that profile."
        )
    if options.cdp_endpoint:
        raise BrowserConsentRequiredError(
            "Google's cookie-consent page is blocking the search, and fli will "
            "not dismiss it in a browser that is yours: the choice would "
            "persist into your real browsing and may be tied to a signed-in "
            "account. Open https://www.google.com/travel/flights in the Chrome "
            f"at {options.cdp_endpoint}, dismiss the cookie notice yourself, "
            "and re-run; the choice persists in that profile."
        )

    try:
        page.get_by_role("button", name=_REJECT_ALL).first.click(timeout=options.timeout_ms)
    except Exception as exc:  # noqa: BLE001 - any failure to find it is the same answer
        raise BrowserConsentRequiredError(
            "Google's cookie-consent page is blocking the search and fli could "
            "not find its 'Reject all' control — the dialog may have been "
            "redesigned or served in another language. fli will not press a "
            "button it cannot read, so nothing was clicked. Open "
            "https://www.google.com/travel/flights in the profile at "
            f"{options.profile_dir}, dismiss the notice yourself, and re-run. "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    logger.info(
        "Dismissed Google's cookie notice with 'Reject all' in fli's own "
        "browser profile at %s. Nothing outside that directory is affected; "
        "set FLI_BROWSER_CONSENT=never if you would rather fli never touched "
        "a consent dialog.",
        options.profile_dir,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _port_of(endpoint: str) -> str:
    """Return the port named by a CDP endpoint, for the error message.

    Args:
        endpoint: A CDP endpoint URL.

    Returns:
        The port as a string, or ``"9222"`` when the URL names none.

    """
    try:
        return str(urlparse(endpoint).port or 9222)
    except ValueError:
        return "9222"


def _as_bool(name: str, value: str) -> bool:
    """Read an environment variable as a boolean.

    Args:
        name: The variable name, for the error message.
        value: Its raw value.

    Returns:
        The parsed boolean.

    Raises:
        ValueError: The value is not recognisably true or false.

    """
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean like 1/0 or true/false, got {value!r}")


def _as_int(name: str, value: str) -> int:
    """Read an environment variable as an integer.

    Args:
        name: The variable name, for the error message.
        value: Its raw value.

    Returns:
        The parsed integer.

    Raises:
        ValueError: The value is not an integer.

    """
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
