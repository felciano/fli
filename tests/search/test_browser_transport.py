"""Offline tests for the browser-backed transport's orchestration.

A fake ``playwright.sync_api`` is injected into ``sys.modules``, so every
branch of :func:`fli.search._browser.capture_rpc_body` — attach, launch,
consent, nudge, timeout — is driven without a real browser and without a
single live call. The live path has its own, separately gated test in
``test_browser_transport_live.py``.

The fake mimics the two Playwright behaviours this module actually depends
on, and nothing else:

* ``expect_response`` resolves its value in ``__exit__`` and, when the block
  raises, cancels rather than swallowing. The consent fast-path is built on
  exactly that, so the fake reproduces it rather than assuming it.
* a response body may be read only *after* the wait returns, never inside an
  event handler — which is why the passive handler here records URLs only.
"""

from __future__ import annotations

import importlib.machinery
import re
import sys
import types

import pytest

from fli.search._browser import BrowserOptions, RpcCapture, browser_available, capture_rpc_body
from fli.search.exceptions import (
    BrowserConsentRequiredError,
    BrowserRpcTimeoutError,
    BrowserTransportUnavailableError,
    BrowserUnreachableError,
)

MARKER = "FlightsFrontendService/GetShoppingResults"
RPC_URL = f"https://www.google.com/_/FlightsFrontendUi/data/batchexecute?rpcids={MARKER}"
OTHER_RPC_URL = "https://www.google.com/_/FlightsFrontendUi/browserinfo"
#: A JS bundle. Its URL contains "FlightsFrontendUi" but it is not an RPC —
#: measured live, counting these turned one real call into a reported 23.
ASSET_URL = (
    "https://www.gstatic.com/_/mss/boq-travel/_/js/"
    "k=boq-travel.FlightsFrontendUi_desktop_ms.en.Wat7Lr_BJNg.2021.O/am=AAAA"
)
TARGET_URL = "https://www.google.com/travel/flights?tfs=abc&hl=en"
CONSENT_URL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"

TIMEOUT = object()  # sentinel: this wait times out


# ---------------------------------------------------------------------------
# The fake browser
# ---------------------------------------------------------------------------


class FakeTimeoutError(Exception):
    """Stand-in for ``playwright.sync_api.TimeoutError``."""


class FakeResponse:
    def __init__(self, url: str, body: bytes = b"") -> None:
        self.url = url
        self._body = body

    def body(self) -> bytes:
        return self._body


class _Info:
    def __init__(self) -> None:
        self.value = None


class _Expect:
    """Mirror of Playwright's ``EventContextManager``."""

    def __init__(self, page: FakePage) -> None:
        self._page = page
        self._info = _Info()

    def __enter__(self) -> _Info:
        return self._info

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_val is not None:
            # Cancelled, exactly as the real one does. Crucially it does NOT
            # suppress: the consent fast-path depends on that.
            return False
        outcome = self._page.waits.pop(0) if self._page.waits else TIMEOUT
        if outcome is TIMEOUT:
            raise FakeTimeoutError("Timeout waiting for response")
        self._info.value = outcome
        return False


class FakeLocator:
    def __init__(self, page: FakePage, label: str, *, present: bool) -> None:
        self._page = page
        self._label = label
        self._present = present

    @property
    def first(self) -> FakeLocator:
        return self

    def click(self, timeout: int | None = None) -> None:
        if not self._present:
            raise FakeTimeoutError(f"locator {self._label!r} not found")
        self._page.clicks.append(self._label)


class FakePage:
    def __init__(
        self,
        *,
        waits: list,
        nav_urls: list[str] | None = None,
        emits: list[str] | None = None,
        has_reject_all: bool = True,
        has_search_button: bool = True,
    ) -> None:
        self.waits = list(waits)
        self.nav_urls = list(nav_urls or [])
        self.emits = list(emits if emits is not None else [OTHER_RPC_URL, RPC_URL])
        self.has_reject_all = has_reject_all
        self.has_search_button = has_search_button
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.clicks: list[str] = []
        self.closed = False
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.url = self.nav_urls.pop(0) if self.nav_urls else url
        for emitted in self.emits:
            for handler in self._handlers.get("response", []):
                handler(FakeResponse(emitted))

    def expect_response(self, _predicate, timeout: int | None = None) -> _Expect:
        return _Expect(self)

    def get_by_role(self, role: str, name=None) -> FakeLocator:
        label = name.pattern if isinstance(name, re.Pattern) else str(name)
        present = self.has_reject_all if "reject" in label.lower() else self.has_search_button
        return FakeLocator(self, label, present=present)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.closed = False
        self.new_contexts: list[FakeContext] = []

    def new_context(self) -> FakeContext:
        context = FakeContext(FakePage(waits=[]))
        self.new_contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, *, browser=None, context=None, connect_error=None, launch_error=None):
        self.browser = browser
        self.context = context
        self.connect_error = connect_error
        self.launch_error = launch_error
        self.connect_calls: list[str] = []
        self.launch_calls: list[tuple[str, bool]] = []

    def connect_over_cdp(self, endpoint: str):
        self.connect_calls.append(endpoint)
        if self.connect_error is not None:
            raise self.connect_error
        return self.browser

    def launch_persistent_context(self, user_data_dir: str, headless: bool = True):
        self.launch_calls.append((user_data_dir, headless))
        if self.launch_error is not None:
            raise self.launch_error
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class _SyncPlaywright:
    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright
        self.exited = False

    def __enter__(self) -> FakePlaywright:
        return self._playwright

    def __exit__(self, *_exc) -> bool:
        self.exited = True
        return False


@pytest.fixture
def install_fake_playwright(monkeypatch):
    """Install a fake ``playwright.sync_api`` and yield a chromium factory."""

    def _install(chromium: FakeChromium):
        root = types.ModuleType("playwright")
        root.__spec__ = importlib.machinery.ModuleSpec("playwright", None)
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.__spec__ = importlib.machinery.ModuleSpec("playwright.sync_api", None)
        sync_api.TimeoutError = FakeTimeoutError

        holder: dict[str, _SyncPlaywright] = {}

        def sync_playwright() -> _SyncPlaywright:
            holder["instance"] = _SyncPlaywright(FakePlaywright(chromium))
            return holder["instance"]

        sync_api.sync_playwright = sync_playwright
        root.sync_api = sync_api
        monkeypatch.setitem(sys.modules, "playwright", root)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
        return holder

    return _install


def cdp_options(**overrides) -> BrowserOptions:
    return BrowserOptions(cdp_endpoint="http://127.0.0.1:9222", **overrides)


def owned_options(tmp_path, **overrides) -> BrowserOptions:
    return BrowserOptions(profile_dir=tmp_path / "profile", **overrides)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_missing_playwright_names_both_install_steps(self, monkeypatch):
        """The user needs the package and the browser; say both."""
        monkeypatch.setattr("fli.search._browser.browser_available", lambda: False)
        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())
        message = str(excinfo.value)
        assert 'uv add "flights[browser]"' in message
        assert "playwright install chromium" in message

    def test_browser_available_is_true_with_the_fake_installed(self, install_fake_playwright):
        install_fake_playwright(FakeChromium())
        assert browser_available() is True


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestCapture:
    def test_cdp_attach_reuses_the_default_context_and_closes_nothing(
        self, install_fake_playwright
    ):
        """The attached browser is the user's; fli closes only its own tab."""
        page = FakePage(waits=[FakeResponse(RPC_URL, b"body-bytes")])
        context = FakeContext(page)
        browser = FakeBrowser([context])
        chromium = FakeChromium(browser=browser)
        install_fake_playwright(chromium)

        capture = capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert isinstance(capture, RpcCapture)
        assert capture.body == b"body-bytes"
        assert capture.nudged is False
        assert chromium.connect_calls == ["http://127.0.0.1:9222"]
        assert page.goto_calls == [TARGET_URL]
        assert page.closed is True
        assert context.closed is False, "the user's default context must survive"
        assert browser.closed is False, "the user's browser must survive"

    def test_rpc_urls_are_recorded_for_diagnosis(self, install_fake_playwright):
        """Real RPCs only — a JS bundle in the count misdirects the diagnosis."""
        page = FakePage(
            waits=[FakeResponse(RPC_URL, b"x")],
            emits=[
                ASSET_URL,
                OTHER_RPC_URL,
                RPC_URL,
                "https://www.gstatic.com/unrelated.js",
            ],
        )
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        capture = capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert capture.rpc_urls == [OTHER_RPC_URL, RPC_URL]

    def test_persistent_launch_closes_the_context_it_created(
        self, install_fake_playwright, tmp_path
    ):
        page = FakePage(waits=[FakeResponse(RPC_URL, b"launched")])
        context = FakeContext(page)
        chromium = FakeChromium(context=context)
        install_fake_playwright(chromium)

        options = owned_options(tmp_path, headless=True)
        capture = capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=options)

        assert capture.body == b"launched"
        assert chromium.launch_calls == [(str(tmp_path / "profile"), True)]
        assert context.closed is True, "fli owns this context and must close it"

    def test_cdp_browser_without_contexts_gets_a_fresh_owned_one(self, install_fake_playwright):
        """A browser with no default context: fli makes one and closes it."""
        browser = FakeBrowser([])
        install_fake_playwright(FakeChromium(browser=browser))

        with pytest.raises(BrowserRpcTimeoutError):
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert len(browser.new_contexts) == 1
        assert browser.new_contexts[0].closed is True
        assert browser.closed is False


# ---------------------------------------------------------------------------
# The nudge and the timeout
# ---------------------------------------------------------------------------


class TestNudgeAndTimeout:
    def test_timeout_then_nudge_succeeds(self, install_fake_playwright):
        page = FakePage(waits=[TIMEOUT, FakeResponse(RPC_URL, b"after-nudge")])
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        capture = capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert capture.body == b"after-nudge"
        assert capture.nudged is True
        assert page.clicks == ["Search"]

    def test_only_one_nudge_is_ever_attempted(self, install_fake_playwright):
        """No retry loop against a deliberately gated endpoint."""
        page = FakePage(waits=[TIMEOUT, TIMEOUT, FakeResponse(RPC_URL, b"never-reached")])
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        with pytest.raises(BrowserRpcTimeoutError):
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert page.clicks == ["Search"], "exactly one nudge, then stop"

    def test_timeout_with_no_rpcs_at_all_says_the_page_never_got_going(
        self, install_fake_playwright
    ):
        page = FakePage(waits=[TIMEOUT, TIMEOUT], emits=[])
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        with pytest.raises(BrowserRpcTimeoutError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        message = str(excinfo.value)
        assert "no Flights RPCs at all" in message
        assert "FLI_BROWSER_TIMEOUT_MS" in message
        assert excinfo.value.rpc_urls == []

    def test_timeout_with_other_rpcs_blames_a_cached_board(self, install_fake_playwright):
        page = FakePage(waits=[TIMEOUT, TIMEOUT], emits=[OTHER_RPC_URL])
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        with pytest.raises(BrowserRpcTimeoutError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert "cached board" in str(excinfo.value)
        assert excinfo.value.rpc_urls == [OTHER_RPC_URL]

    def test_a_missing_search_button_is_a_diagnosis_not_a_crash(self, install_fake_playwright):
        page = FakePage(waits=[TIMEOUT], has_search_button=False)
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        with pytest.raises(BrowserRpcTimeoutError):
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert page.closed is True


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


class TestConsent:
    def test_own_profile_rejects_all_and_retries_from_zero(
        self, install_fake_playwright, tmp_path, caplog
    ):
        page = FakePage(
            waits=[FakeResponse(RPC_URL, b"after-consent")],
            nav_urls=[CONSENT_URL, TARGET_URL],
        )
        install_fake_playwright(FakeChromium(context=FakeContext(page)))

        with caplog.at_level("INFO", logger="fli.search._browser"):
            capture = capture_rpc_body(
                TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path)
            )

        assert capture.body == b"after-consent"
        assert page.clicks == ["^reject all$"], "the only control fli will click"
        assert page.goto_calls == [TARGET_URL, TARGET_URL], "navigation restarts from zero"
        assert "Reject all" in caplog.text

    def test_consent_in_the_users_browser_is_never_clicked(self, install_fake_playwright):
        page = FakePage(waits=[FakeResponse(RPC_URL, b"x")], nav_urls=[CONSENT_URL])
        install_fake_playwright(FakeChromium(browser=FakeBrowser([FakeContext(page)])))

        with pytest.raises(BrowserConsentRequiredError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        assert page.clicks == [], "fli must not click consent in a browser that is yours"
        message = str(excinfo.value)
        assert "http://127.0.0.1:9222" in message
        assert "dismiss the cookie notice yourself" in message

    def test_consent_never_refuses_even_in_flis_own_profile(
        self, install_fake_playwright, tmp_path
    ):
        page = FakePage(waits=[FakeResponse(RPC_URL, b"x")], nav_urls=[CONSENT_URL])
        install_fake_playwright(FakeChromium(context=FakeContext(page)))

        options = owned_options(tmp_path, consent="never")
        with pytest.raises(BrowserConsentRequiredError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=options)

        assert page.clicks == []
        assert "FLI_BROWSER_CONSENT=never" in str(excinfo.value)

    def test_missing_reject_all_control_clicks_nothing_at_all(
        self, install_fake_playwright, tmp_path
    ):
        """The hard rule: no fallback button, ever."""
        page = FakePage(
            waits=[FakeResponse(RPC_URL, b"x")],
            nav_urls=[CONSENT_URL],
            has_reject_all=False,
        )
        install_fake_playwright(FakeChromium(context=FakeContext(page)))

        with pytest.raises(BrowserConsentRequiredError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        assert page.clicks == []
        assert "will not press a button it cannot read" in str(excinfo.value)

    def test_consent_that_comes_back_is_not_retried_forever(
        self, install_fake_playwright, tmp_path
    ):
        page = FakePage(
            waits=[FakeResponse(RPC_URL, b"x")],
            nav_urls=[CONSENT_URL, CONSENT_URL],
        )
        install_fake_playwright(FakeChromium(context=FakeContext(page)))

        with pytest.raises(BrowserConsentRequiredError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        assert page.clicks == ["^reject all$"], "dismissed once, then stopped"
        assert "came back after fli dismissed it" in str(excinfo.value)

    def test_consent_detection_does_not_burn_the_timeout(self, install_fake_playwright, tmp_path):
        """The wait is cancelled by the redirect, not run out.

        The fake's ``__exit__`` only consumes a wait outcome when the block
        exits cleanly, so a consumed ``TIMEOUT`` here would prove the consent
        path had sat through a full wait first.
        """
        page = FakePage(
            waits=[FakeResponse(RPC_URL, b"fast")],
            nav_urls=[CONSENT_URL, TARGET_URL],
        )
        install_fake_playwright(FakeChromium(context=FakeContext(page)))

        capture = capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        assert capture.body == b"fast"
        assert page.waits == [], "exactly one wait was consumed — the successful one"


# ---------------------------------------------------------------------------
# Failure to obtain a browser at all
# ---------------------------------------------------------------------------


class TestBrowserAcquisitionFailures:
    def test_unreachable_endpoint_refuses_to_launch_instead(self, install_fake_playwright):
        chromium = FakeChromium(connect_error=ConnectionRefusedError("connect ECONNREFUSED"))
        install_fake_playwright(chromium)

        with pytest.raises(BrowserUnreachableError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=cdp_options())

        message = str(excinfo.value)
        assert "http://127.0.0.1:9222" in message
        assert "--remote-debugging-port=9222" in message
        assert "will not launch a browser instead" in message
        assert chromium.launch_calls == []

    def test_missing_chromium_binary_says_install_chromium_only(
        self, install_fake_playwright, tmp_path
    ):
        error = RuntimeError(
            "Executable doesn't exist at /Users/x/Library/Caches/ms-playwright/"
            "chromium-1140/chrome-mac/Chromium.app/Contents/MacOS/Chromium"
        )
        install_fake_playwright(FakeChromium(launch_error=error))

        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        message = str(excinfo.value)
        assert "playwright install chromium" in message
        assert "ms-playwright" in message, "quote the path playwright reported"
        assert 'uv add "flights[browser]"' not in message, "do not re-install what they have"

    def test_sandboxed_launch_is_diagnosed_not_traced(self, install_fake_playwright, tmp_path):
        class TargetClosedError(Exception):
            pass

        install_fake_playwright(
            FakeChromium(launch_error=TargetClosedError("Target page, context or browser closed"))
        )

        with pytest.raises(BrowserUnreachableError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        message = str(excinfo.value)
        assert "sandbox" in message
        assert "FLI_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222" in message

    def test_unclassified_launch_failure_still_names_the_profile(
        self, install_fake_playwright, tmp_path
    ):
        install_fake_playwright(FakeChromium(launch_error=RuntimeError("disk on fire")))

        with pytest.raises(BrowserUnreachableError) as excinfo:
            capture_rpc_body(TARGET_URL, rpc_marker=MARKER, options=owned_options(tmp_path))

        assert str(tmp_path / "profile") in str(excinfo.value)
        assert "disk on fire" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class TestBrowserOptions:
    def test_defaults(self):
        options = BrowserOptions()
        assert options.cdp_endpoint is None
        assert options.headless is True
        assert options.timeout_ms == 45_000
        assert options.consent == "reject"
        assert options.profile_dir.name == "browser-profile"

    def test_is_frozen(self):
        """Options are captured once per capture; mutating one mid-flight is a bug."""
        from pydantic import ValidationError

        options = BrowserOptions()
        with pytest.raises(ValidationError):
            options.headless = False

    def test_from_env_reads_every_knob(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLI_BROWSER_CDP_ENDPOINT", "http://127.0.0.1:9333")
        monkeypatch.setenv("FLI_BROWSER_PROFILE_DIR", str(tmp_path / "p"))
        monkeypatch.setenv("FLI_BROWSER_HEADLESS", "0")
        monkeypatch.setenv("FLI_BROWSER_TIMEOUT_MS", "9000")
        monkeypatch.setenv("FLI_BROWSER_CONSENT", "never")

        options = BrowserOptions.from_env()

        assert options.cdp_endpoint == "http://127.0.0.1:9333"
        assert options.profile_dir == tmp_path / "p"
        assert options.headless is False
        assert options.timeout_ms == 9000
        assert options.consent == "never"

    def test_from_env_with_nothing_set_matches_the_defaults(self, monkeypatch):
        for name in (
            "FLI_BROWSER_CDP_ENDPOINT",
            "FLI_BROWSER_PROFILE_DIR",
            "FLI_BROWSER_HEADLESS",
            "FLI_BROWSER_TIMEOUT_MS",
            "FLI_BROWSER_CONSENT",
        ):
            monkeypatch.delenv(name, raising=False)
        assert BrowserOptions.from_env() == BrowserOptions()

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("FLI_BROWSER_HEADLESS", "maybe"),
            ("FLI_BROWSER_TIMEOUT_MS", "soon"),
            ("FLI_BROWSER_CONSENT", "accept"),
        ],
    )
    def test_from_env_rejects_garbage_rather_than_ignoring_it(self, monkeypatch, name, value):
        """Silently falling back to a default hides a misconfiguration."""
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            BrowserOptions.from_env()


class TestConsentUrlDetection:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (CONSENT_URL, True),
            ("https://CONSENT.GOOGLE.COM/m", True),
            (TARGET_URL, False),
            ("https://www.google.com/consent.google.com", False),
            ("https://evil.example.com/?x=consent.google.com", False),
            ("", False),
        ],
    )
    def test_matches_on_host_not_text(self, url, expected):
        from fli.search._browser import is_consent_url

        assert is_consent_url(url) is expected


class TestRpcUrlDetection:
    """A JS bundle is not an RPC, however much its URL looks like one."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (RPC_URL, True),
            (OTHER_RPC_URL, True),
            ("https://www.google.com/_/FlightsFrontendUi/data/batchexecute?rpcids=x", True),
            (ASSET_URL, False),
            ("https://www.gstatic.com/unrelated.js", False),
            (TARGET_URL, False),
        ],
    )
    def test_only_real_rpcs_count(self, url, expected):
        from fli.search._browser import is_rpc_url

        assert is_rpc_url(url) is expected
