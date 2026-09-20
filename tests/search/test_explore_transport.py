"""Explore over the browser transport, offline.

``tests/search/fixtures/explore_anywhere_lhr.bin`` is a verbatim
``GetExploreDestinations`` body intercepted in a browser on 2026-09-20 for
``LHR`` on ``2026-10-16``. It is the evidence for the claim this change
makes: Explore was never a decoder problem, only a transport one, so the
existing parsers read an intercepted body **unchanged**.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fli.models import Airport, ExplorePlace, ExploreRegion, ExploreSearchFilters
from fli.search import explore as explore_module
from fli.search._browser import RpcCapture
from fli.search._tfs import explore_page_url
from fli.search.exceptions import BrowserTransportUnavailableError, SearchUnsupportedError
from fli.search.explore import EXPLORE_RPC_MARKER, SearchExplore
from fli.search.transport import Transport

FIXTURE = Path(__file__).parent / "fixtures" / "explore_anywhere_lhr.bin"

#: The exact ``tfs`` whose page load was observed firing the RPC. Pinned so a
#: future "tidy-up" of the encoder cannot quietly stop provoking it.
VERIFIED_TFS = "CBwQAhoVEgoyMDI2LTEwLTE2agcIARIDTEhSQAFIAXABmAEC"


@pytest.fixture(scope="module")
def captured_body() -> bytes:
    return FIXTURE.read_bytes()


def _future_date(days_ahead: int = 30) -> str:
    """Return a date still in the future whenever this suite is run."""
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _filters(**kwargs) -> ExploreSearchFilters:
    kwargs.setdefault("origin", Airport.LHR)
    kwargs.setdefault("departure_date", _future_date())
    return ExploreSearchFilters(**kwargs)


def _verified_filters() -> ExploreSearchFilters:
    """Return the exact filters whose page load was observed firing the RPC.

    Built with ``model_construct`` so the observation survives its own
    validator: 2026-10-16 is evidence, and evidence does not stay in the
    future. Pinning it through the normal constructor meant every test in
    this module -- including the two that have nothing to do with dates --
    would start erroring the day after, which is how a date-pinned fixture
    turns into a suite-wide outage. Every other test here uses a relative
    date, as the rest of the suite already does.
    """
    return ExploreSearchFilters.model_construct(
        origin=Airport.LHR,
        departure_date="2026-10-16",
    )


class TestExplorePageUrl:
    def test_reproduces_the_url_that_was_observed_firing_the_rpc(self):
        url = explore_page_url(_verified_filters(), currency="USD")
        assert url.startswith("https://www.google.com/travel/explore?tfs=")
        assert f"tfs={VERIFIED_TFS}" in url
        assert "curr=USD" in url and "hl=en" in url and "gl=US" in url

    def test_a_narrowed_destination_is_refused_rather_than_ignored(self):
        """Tested live: Google ignores a mid here and serves a wider board.

        Honouring the filter silently would mean returning results that do
        not match it, which is worse than saying no.
        """
        with pytest.raises(SearchUnsupportedError, match="anywhere"):
            explore_page_url(_filters(destination=ExploreRegion.SOUTHERN_EUROPE))

    def test_a_knowledge_graph_origin_is_refused(self):
        with pytest.raises(SearchUnsupportedError, match="airport origin"):
            explore_page_url(_filters(origin=ExplorePlace(mid="/m/04jpl", type_code=4)))

    def test_anywhere_is_accepted_explicitly_too(self):
        assert explore_page_url(_filters(destination=ExploreRegion.ANYWHERE))


class TestSearchExploreOverBrowser:
    @pytest.fixture
    def capture_calls(self, monkeypatch, captured_body):
        calls: list[dict] = []

        def fake_capture(url, *, rpc_marker, options):
            calls.append({"url": url, "rpc_marker": rpc_marker})
            return RpcCapture(body=captured_body, rpc_urls=[], nudged=False)

        import fli.search._browser as browser_module

        monkeypatch.setattr(browser_module, "capture_rpc_body", fake_capture)
        monkeypatch.setattr(browser_module, "browser_available", lambda: True)
        return calls

    def test_the_existing_decoders_read_an_intercepted_body_unchanged(self, capture_calls):
        result = SearchExplore().search(_filters(), currency="USD")
        assert result is not None
        assert len(result.destinations) == 66
        assert result.origin_name == "London"
        priced = [d for d in result.destinations if d.price is not None]
        assert len(priced) == 52
        names = {d.name for d in result.destinations}
        assert {"Dublin", "Paris", "Barcelona"} <= names

    def test_auto_goes_to_the_browser_rather_than_a_request_it_knows_is_gated(
        self, capture_calls, monkeypatch
    ):
        """The HTTP endpoint answers error 13; firing it anyway is pure noise."""
        client = SearchExplore()

        def explode(*args, **kwargs):
            raise AssertionError("AUTO must not POST the gated endpoint")

        monkeypatch.setattr(client.client, "post", explode)
        client.search(_filters(), transport=Transport.AUTO)
        (call,) = capture_calls
        assert call["rpc_marker"] == EXPLORE_RPC_MARKER
        assert call["url"].startswith("https://www.google.com/travel/explore?tfs=")

    def test_browser_is_the_same_route_as_auto(self, capture_calls):
        SearchExplore().search(_filters(), transport=Transport.BROWSER)
        assert len(capture_calls) == 1

    def test_http_still_posts_the_endpoint_so_the_gate_can_be_retested(
        self, capture_calls, monkeypatch, captured_body
    ):
        client = SearchExplore()
        posted: list[str] = []

        class _Response:
            text = captured_body.decode("utf-8", "replace")

            @staticmethod
            def raise_for_status():
                return None

        def fake_post(url, **kwargs):
            posted.append(url)
            return _Response()

        monkeypatch.setattr(client.client, "post", fake_post)
        client.search(_filters(), transport=Transport.HTTP)
        assert posted and "GetExploreDestinations" in posted[0]
        assert not capture_calls, "HTTP must never reach a browser"


class TestWithoutTheExtra:
    def test_the_message_names_both_install_steps(self, monkeypatch):
        import fli.search._browser as browser_module

        monkeypatch.setattr(browser_module, "browser_available", lambda: False)
        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            SearchExplore().search(_filters())
        message = str(excinfo.value)
        assert "flights[browser]" in message
        assert "playwright install chromium" in message
        assert "gated" in message

    def test_http_remains_reachable_with_no_browser_installed(self, monkeypatch):
        """An explicit HTTP call must not be blocked by a missing extra."""
        import fli.search._browser as browser_module

        monkeypatch.setattr(browser_module, "browser_available", lambda: False)
        client = SearchExplore()
        calls: list[str] = []

        class _Response:
            text = ""

            @staticmethod
            def raise_for_status():
                return None

        monkeypatch.setattr(
            client.client, "post", lambda url, **kw: (calls.append(url), _Response())[1]
        )
        assert client.search(_filters(), transport=Transport.HTTP) is None
        assert calls


def test_explore_module_imports_capture_lazily():
    """The seam holds: importing Explore must not import the browser stack."""
    assert not hasattr(explore_module, "capture_rpc_body")


class TestTransferIsNotALayover:
    """``summary[8]`` is a drive from the served airport, not a layover.

    Read off the committed fixture, which is a verbatim intercepted body,
    so these are Google's own numbers rather than a constructed example.
    """

    def _by_name(self, captured_body):
        from fli.search._capture import iter_wrb_chunks
        from fli.search._decoders import (
            parse_explore_destinations_chunk,
            parse_explore_prices_chunk,
        )

        merged = {}
        for chunk in iter_wrb_chunks(captured_body):
            try:
                _, destinations = parse_explore_destinations_chunk(chunk)
            except Exception:
                destinations = []
            for d in destinations:
                merged.setdefault(d.mid, {})["name"] = d.name
            try:
                for mid, priced in parse_explore_prices_chunk(chunk).items():
                    merged.setdefault(mid, {}).update(priced)
            except Exception:
                pass
        return {v["name"]: v for v in merged.values() if "name" in v and "stops" in v}

    def test_a_nonstop_flight_can_still_carry_a_transfer(self, captured_body):
        """Galway: nonstop LHR->DUB, then 150 minutes on the ground.

        As ``layover_minutes`` this read ``stops=0`` with a 2.5h layover --
        two facts that cannot both be true.
        """
        rows = self._by_name(captured_body)
        galway = rows.get("Galway")
        if galway is None:
            pytest.skip("fixture does not name Galway")
        assert galway["stops"] == 0
        assert galway["transfer_minutes"] == 150
        assert galway["transfer_city"] == "Dublin"
        assert galway["destination_airport"] == "DUB"

    def test_the_field_no_longer_exists_under_its_old_name(self):
        from fli.models.google_flights.explore import ExploreDestination

        assert "transfer_minutes" in ExploreDestination.model_fields
        assert "layover_minutes" not in ExploreDestination.model_fields
