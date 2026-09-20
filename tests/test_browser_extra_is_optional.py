"""``uv add flights`` must keep installing no browser.

ADR 001's whole bargain is that the browser transport is opt-in. The dangerous
regression is not a loud ``ModuleNotFoundError`` — it is a module-scope
``import playwright`` that nobody notices because the development machine has
the extra installed. These tests simulate the default install by masking
``playwright`` everywhere it could be found, then importing the library.

``tests/test_dependency_declarations.py`` guards the other half statically:
that ``playwright`` is declared in the ``browser`` extra and nowhere near
``[project.dependencies]``.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import sys

import pytest

#: Every module the browser work touches, plus the package roots a user gets
#: from a plain `import fli`. All must import with no browser present.
MODULES = [
    "fli",
    "fli.search",
    "fli.search._browser",
    "fli.search._capture",
    "fli.search.transport",
    "fli.search.exceptions",
    "fli.search.explore",
    "fli.search.flights",
    "fli.search.multi_city",
    "fli.cli.main",
    "fli.cli.commands.multi",
    "fli.mcp.server",
]


@pytest.fixture
def without_playwright(monkeypatch):
    """Make ``playwright`` unimportable and undiscoverable, as on a plain install."""
    for name in list(sys.modules):
        if name == "playwright" or name.startswith("playwright."):
            monkeypatch.delitem(sys.modules, name)

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, package=None):
        if name == "playwright" or name.startswith("playwright."):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    return fake_import


@pytest.fixture
def fresh_fli_modules():
    """Re-import ``fli`` from scratch, then put the original modules back.

    Without the restore, a re-imported ``fli.search.exceptions`` leaves a
    *second* set of exception classes in play, and later tests in the same
    session compare a class raised by the old module against the new one and
    mysteriously fail to catch it.
    """
    saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "fli"}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n.split(".")[0] == "fli"]:
            del sys.modules[name]
        sys.modules.update(saved)


class TestImportsWithoutTheExtra:
    @pytest.mark.parametrize("module", MODULES)
    def test_module_imports(self, without_playwright, fresh_fli_modules, module):
        """A lazy import is only lazy if the module loads without the package."""
        assert importlib.import_module(module) is not None

    def test_browser_available_reports_false(self, without_playwright):
        from fli.search._browser import browser_available

        assert browser_available() is False

    def test_capture_names_both_install_steps(self, without_playwright):
        from fli.search._browser import BrowserOptions, capture_rpc_body
        from fli.search.exceptions import BrowserTransportUnavailableError

        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            capture_rpc_body(
                "https://www.google.com/travel/flights?tfs=abc",
                rpc_marker="GetShoppingResults",
                options=BrowserOptions(),
            )

        message = str(excinfo.value)
        assert 'uv add "flights[browser]"' in message
        assert "playwright install chromium" in message

    def test_the_unavailable_error_is_a_search_client_error(self):
        """So the CLI's existing arms handle it instead of printing a traceback."""
        from fli.search.exceptions import (
            BrowserTransportUnavailableError,
            SearchClientError,
            SearchUnsupportedError,
        )

        assert issubclass(BrowserTransportUnavailableError, SearchUnsupportedError)
        assert issubclass(BrowserTransportUnavailableError, SearchClientError)


class TestConsumersWithoutTheExtra:
    """The two consumers must fail with advice, not with an ImportError."""

    def _multi_city_filters(self):
        from fli.models import (
            Airport,
            FlightSearchFilters,
            FlightSegment,
            PassengerInfo,
        )
        from fli.models.google_flights.base import TripType

        return FlightSearchFilters(
            trip_type=TripType.MULTI_CITY,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport.LHR, 0]],
                    arrival_airport=[[Airport.BOS, 0]],
                    travel_date="2027-10-16",
                ),
                FlightSegment(
                    departure_airport=[[Airport.BOS, 0]],
                    arrival_airport=[[Airport.CDG, 0]],
                    travel_date="2027-10-23",
                ),
            ],
        )

    def test_multi_city_names_both_install_steps(self, without_playwright):
        from fli.search import SearchMultiCity
        from fli.search.exceptions import BrowserTransportUnavailableError

        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            SearchMultiCity().search(self._multi_city_filters())
        message = str(excinfo.value)
        assert 'uv add "flights[browser]"' in message
        assert "playwright install chromium" in message

    def test_explore_names_both_install_steps(self, without_playwright):
        from fli.models import Airport, ExploreSearchFilters
        from fli.search import SearchExplore
        from fli.search.exceptions import BrowserTransportUnavailableError

        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            SearchExplore().search(
                ExploreSearchFilters(origin=Airport.LHR, departure_date="2027-10-16")
            )
        message = str(excinfo.value)
        assert 'uv add "flights[browser]"' in message
        assert "playwright install chromium" in message

    def test_the_multi_city_url_still_works_with_no_browser(self, without_playwright):
        """It is the CLI's fallback, so it must never need the extra."""
        from fli.search._tfs import multi_city_url

        url = multi_city_url([("LHR", "BOS", "2027-10-16"), ("BOS", "CDG", "2027-10-23")])
        assert url.startswith("https://www.google.com/travel/flights?tfs=")

    def test_a_one_way_search_never_reaches_the_browser(self, without_playwright, monkeypatch):
        """AUTO on a search HTTP can serve must not even ask about a browser."""
        from fli.models import Airport, FlightSearchFilters, FlightSegment, PassengerInfo
        from fli.models.google_flights.base import TripType
        from fli.search import SearchFlights, Transport
        from fli.search import _browser as browser_module

        def explode(*args, **kwargs):
            raise AssertionError("a one-way AUTO search must not touch the browser")

        monkeypatch.setattr(browser_module, "capture_rpc_body", explode)
        client = SearchFlights()
        monkeypatch.setattr(
            client.client, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        filters = FlightSearchFilters(
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport.LHR, 0]],
                    arrival_airport=[[Airport.BOS, 0]],
                    travel_date="2027-10-16",
                )
            ],
        )
        # It reaches the HTTP client (RuntimeError) rather than the browser.
        with pytest.raises(RuntimeError, match="stop"):
            client.search(filters, transport=Transport.AUTO)


class TestNoModuleScopeImport:
    def test_browser_module_has_no_runtime_playwright_import_at_module_scope(self):
        """Read the source, not the behaviour — the failure is a stray edit.

        Behavioural tests pass on a machine that has the extra installed, so
        the guard that actually catches the regression is structural.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        source = root / "fli" / "search" / "_browser.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

        offenders = []
        for node in tree.body:  # module scope only
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith("playwright")]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("playwright"):
                offenders.append(node.module)

        assert not offenders, (
            f"fli/search/_browser.py imports playwright at module scope: {offenders}. "
            "Every playwright import must be inside a function body (the "
            "TYPE_CHECKING block is erased at runtime and does not count)."
        )

    def test_only_the_browser_module_mentions_playwright_at_all(self):
        """The containment ADR 001 committed to, checked rather than assumed."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "fli"
        allowed = {root / "search" / "_browser.py"}
        offenders = [
            str(path.relative_to(root.parent))
            for path in root.rglob("*.py")
            if path not in allowed and "playwright" in path.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"these modules name playwright: {offenders}. Only "
            "fli/search/_browser.py may; everything else goes through "
            "capture_rpc_body."
        )
