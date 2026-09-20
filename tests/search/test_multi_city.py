"""Multi-city search over the browser transport, offline.

The transport half is faked — ``capture_rpc_body`` is replaced with a function
that returns the committed capture — so every assertion here is about what
:class:`SearchMultiCity` does with a body, not about whether a browser can be
started. The live half is covered by
``tests/search/test_browser_transport_live.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fli.models import Airport, FlightSearchFilters, FlightSegment, PassengerInfo
from fli.models.google_flights.base import MaxStops, SeatType, TripType
from fli.search import multi_city as multi_city_module
from fli.search._browser import BrowserOptions, RpcCapture
from fli.search._capture import decode_shopping_capture, merge_progressive_flights
from fli.search.exceptions import (
    BrowserAttestationRejectedError,
    BrowserDecodeError,
    BrowserRpcTimeoutError,
    SearchUnsupportedError,
)
from fli.search.multi_city import SHOPPING_RPC_MARKER, SearchMultiCity, board_total
from fli.search.transport import Transport

FIXTURE = Path(__file__).parent / "fixtures" / "flight_search_multi_city.bin"


def _wrb(inner: str) -> bytes:
    """Frame one payload the way Google's batchexecute responses are framed."""
    row = json.dumps([["wrb.fr", None, inner]], separators=(",", ":"))
    return f")]}}'\n\n{len(row)}\n{row}".encode()


def _rejected_wrb() -> bytes:
    """Build a payload-less ``wrb.fr`` row with error 13 — Google's refusal."""
    row = json.dumps([["wrb.fr", None, None, None, None, [13]]], separators=(",", ":"))
    return f")]}}'\n\n{len(row)}\n{row}".encode()


@pytest.fixture(scope="module")
def captured_body() -> bytes:
    return FIXTURE.read_bytes()


def _filters(legs=None, **kwargs) -> FlightSearchFilters:
    legs = legs or [
        (Airport.LHR, Airport.BOS, "2026-10-16"),
        (Airport.BOS, Airport.CDG, "2026-10-23"),
        (Airport.CDG, Airport.LHR, "2026-10-30"),
    ]
    return FlightSearchFilters(
        trip_type=kwargs.pop("trip_type", TripType.MULTI_CITY),
        passenger_info=kwargs.pop("passenger_info", PassengerInfo(adults=1)),
        flight_segments=[
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[dest, 0]],
                travel_date=date,
            )
            for origin, dest, date in legs
        ],
        **kwargs,
    )


@pytest.fixture
def capture_calls(monkeypatch, captured_body):
    """Replace the browser with a recorder that hands back the fixture."""
    calls: list[dict] = []

    def fake_capture(url, *, rpc_marker, options):
        calls.append({"url": url, "rpc_marker": rpc_marker, "options": options})
        return RpcCapture(body=captured_body, rpc_urls=[], nudged=False)

    monkeypatch.setattr(multi_city_module, "capture_rpc_body", fake_capture)
    return calls


class TestMerge:
    """The one piece of genuinely new decode logic."""

    def test_golden_board_from_the_committed_capture(self, captured_body):
        """83 raw rows across 9 frames are 11 itineraries, freshest price."""
        flights = decode_shopping_capture(captured_body, context="fixture")
        assert len(flights) == 11
        assert min(f.price for f in flights if f.price is not None) == 1394.0

    def test_later_frames_win(self, captured_body):
        """BA239 was first quoted 3578 and revised to 2976 mid-board.

        Keeping the first frame's price would quietly overcharge by 600.
        """
        flights = decode_shopping_capture(captured_body, context="fixture")
        ba239 = [f for f in flights if any(leg.flight_number == "239" for leg in f.legs)]
        assert len(ba239) == 1
        assert ba239[0].price == 2976.0

    def test_keying_on_price_too_would_double_count(self, captured_body):
        """Why the key is leg identity alone: legs+price gives 14, not 11."""
        flights = decode_shopping_capture(captured_body, context="fixture")
        merged_again = merge_progressive_flights(flights)
        assert len(merged_again) == len(flights), "merging is idempotent"

    def test_a_shape_change_is_not_reported_as_no_flights(self):
        """Slot absent means the wire changed; it must not read as 'none found'."""
        body = _wrb('[["nope"]]')
        with pytest.raises(BrowserDecodeError, match="row-shape change|\\[2\\]/\\[3\\]"):
            decode_shopping_capture(body, context="test")


class TestSearchMultiCity:
    def test_returns_a_board_not_a_list(self, capture_calls):
        board = SearchMultiCity().search(_filters())
        assert board is not None
        assert board.price_basis == "entire_trip"
        assert board.board_leg_index == 0
        assert len(board.results) == 11
        assert board.legs == [
            ("LHR", "BOS", "2026-10-16"),
            ("BOS", "CDG", "2026-10-23"),
            ("CDG", "LHR", "2026-10-30"),
        ]

    def test_board_is_cheapest_first(self, capture_calls):
        board = SearchMultiCity().search(_filters())
        prices = [f.price for f in board.results if f.price is not None]
        assert prices == sorted(prices)
        assert board_total(board) == 1394.0

    def test_it_waits_for_the_shopping_rpc_on_a_multi_city_page(self, capture_calls):
        SearchMultiCity().search(_filters(), currency="USD")
        (call,) = capture_calls
        assert call["rpc_marker"] == SHOPPING_RPC_MARKER
        assert call["url"].startswith("https://www.google.com/travel/flights?tfs=")
        assert "curr=USD" in call["url"]

    def test_the_url_carries_the_filters_not_just_the_legs(self, capture_calls):
        """A board fetched with the wrong cabin is a wrong answer, not a near one."""
        SearchMultiCity().search(_filters(seat_type=SeatType.BUSINESS, stops=MaxStops.NON_STOP))
        plain_url = capture_calls[0]["url"]
        capture_calls.clear()
        SearchMultiCity().search(_filters())
        assert plain_url != capture_calls[0]["url"]

    def test_booking_url_survives_with_no_browser(self, capture_calls):
        """It is built from plain legs, so it works on a no-extra install too."""
        board = SearchMultiCity().search(_filters())
        assert board.booking_url.startswith("https://www.google.com/travel/flights?tfs=")

    def test_a_one_way_search_is_refused_by_type(self):
        with pytest.raises(ValueError, match="multi-city"):
            SearchMultiCity().search(
                _filters(
                    legs=[(Airport.LHR, Airport.BOS, "2026-10-16")],
                    trip_type=TripType.ONE_WAY,
                )
            )

    def test_no_itineraries_returns_none(self, monkeypatch):
        """Google finding nothing is an answer, not a failure."""
        # ``[[]]`` is "the slot is there and holds no rows" — Google found
        # nothing. A missing slot would be a shape change, which is the case
        # above; keeping the two apart is the point of ``flight_rows``.
        empty = _wrb("[null,null,[[]],[[]]]")

        def fake_capture(url, *, rpc_marker, options):
            return RpcCapture(body=empty, rpc_urls=[], nudged=False)

        monkeypatch.setattr(multi_city_module, "capture_rpc_body", fake_capture)
        assert SearchMultiCity().search(_filters()) is None

    def test_a_rejected_browser_is_a_different_diagnosis(self, monkeypatch):
        """Error 13 from inside a browser is bot detection, not the bgr gate."""
        rejected = _rejected_wrb()

        def fake_capture(url, *, rpc_marker, options):
            return RpcCapture(body=rejected, rpc_urls=[], nudged=False)

        monkeypatch.setattr(multi_city_module, "capture_rpc_body", fake_capture)
        with pytest.raises(BrowserAttestationRejectedError) as excinfo:
            SearchMultiCity().search(_filters())
        message = str(excinfo.value)
        assert "real browser" in message
        assert "FLI_BROWSER_HEADLESS=0" in message
        assert "Do not retry" in message


class TestSearchFlightsRedirectsMultiCity:
    def test_search_points_at_the_class_that_can_answer(self, monkeypatch):
        from fli.search import SearchFlights
        from fli.search import _browser as browser_module

        monkeypatch.setattr(browser_module, "browser_available", lambda: True)
        with pytest.raises(SearchUnsupportedError, match="SearchMultiCity"):
            SearchFlights().search(_filters())

    def test_without_the_extra_it_names_the_install(self, monkeypatch):
        from fli.search import SearchFlights
        from fli.search import _browser as browser_module
        from fli.search.exceptions import BrowserTransportUnavailableError

        monkeypatch.setattr(browser_module, "browser_available", lambda: False)
        with pytest.raises(BrowserTransportUnavailableError) as excinfo:
            SearchFlights().search(_filters())
        assert "flights[browser]" in str(excinfo.value)
        assert "playwright install chromium" in str(excinfo.value)

    def test_it_never_reaches_the_network(self, monkeypatch):
        """The refusal happens before a request, not after one."""
        from fli.search import SearchFlights

        client = SearchFlights()

        def explode(*args, **kwargs):
            raise AssertionError("multi-city must be refused before any request")

        monkeypatch.setattr(client.client, "get", explode)
        with pytest.raises(SearchUnsupportedError):
            client.search(_filters())


class TestForcedBrowserTransport:
    """``Transport.BROWSER`` on an ordinary search.

    It exists to validate the browser path against a known-good HTTP result on
    the same ``tfs`` token. The committed capture happens to be a board of
    LHR→BOS options, so it stands in for a one-way search of that route.
    """

    def _one_way(self) -> FlightSearchFilters:
        return _filters(
            legs=[(Airport.LHR, Airport.BOS, "2026-10-16")],
            trip_type=TripType.ONE_WAY,
        )

    def test_it_intercepts_instead_of_fetching_the_page(self, monkeypatch, captured_body):
        from fli.search import SearchFlights
        from fli.search import _browser as browser_module

        calls: list[str] = []

        def fake_capture(url, *, rpc_marker, options):
            calls.append(url)
            return RpcCapture(body=captured_body, rpc_urls=[], nudged=False)

        monkeypatch.setattr(browser_module, "capture_rpc_body", fake_capture)
        client = SearchFlights()
        monkeypatch.setattr(
            client.client,
            "get",
            lambda *a, **k: pytest.fail("Transport.BROWSER must not fetch the page over HTTP"),
        )

        results = client.search(self._one_way(), transport=Transport.BROWSER)
        assert results and len(results) == 11
        assert calls and calls[0].startswith("https://www.google.com/travel/flights?tfs=")

    def test_it_applies_the_same_client_side_filters_as_http(self, monkeypatch, captured_body):
        """Otherwise the two transports answer the same question differently."""
        from fli.search import SearchFlights
        from fli.search import _browser as browser_module

        monkeypatch.setattr(
            browser_module,
            "capture_rpc_body",
            lambda url, *, rpc_marker, options: RpcCapture(body=captured_body),
        )
        filters = self._one_way()
        filters.max_duration = 600
        results = SearchFlights().search(filters, transport=Transport.BROWSER)
        assert results is not None
        assert all(flight.duration <= 600 for flight in results)

    def test_auto_does_not_reach_the_browser_for_a_one_way(self, monkeypatch):
        from fli.search import SearchFlights
        from fli.search import _browser as browser_module

        monkeypatch.setattr(
            browser_module,
            "capture_rpc_body",
            lambda *a, **k: pytest.fail("AUTO one-way must stay on HTTP"),
        )
        client = SearchFlights()
        monkeypatch.setattr(
            client.client,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reached HTTP")),
        )
        with pytest.raises(RuntimeError, match="reached HTTP"):
            client.search(self._one_way(), transport=Transport.AUTO)


class TestNoAutomaticRetry:
    """A refused or timed-out capture is reported, never re-fired.

    ADR 001 and the failure-mode design are explicit: re-firing a request the
    service just declined is precisely the behaviour a gate exists to punish,
    and the single search-control nudge inside ``capture_rpc_body`` is the
    only retry that exists anywhere on this path. The messages say so; these
    assert the code does so, which is the half a user actually pays for.
    """

    def _counting_capture(self, monkeypatch, outcome):
        """Install a capture that records its calls and then does ``outcome``."""
        calls: list[str] = []

        def fake_capture(url, *, rpc_marker, options):
            calls.append(url)
            return outcome()

        monkeypatch.setattr(multi_city_module, "capture_rpc_body", fake_capture)
        return calls

    def test_a_rejected_browser_is_captured_exactly_once(self, monkeypatch):
        rejected = _rejected_wrb()
        calls = self._counting_capture(
            monkeypatch, lambda: RpcCapture(body=rejected, rpc_urls=[], nudged=False)
        )
        with pytest.raises(BrowserAttestationRejectedError):
            SearchMultiCity().search(_filters())
        assert len(calls) == 1

    def test_the_rejection_names_the_profile_to_delete(self, monkeypatch):
        """'Get a fresh profile' is only actionable if it says which one."""
        rejected = _rejected_wrb()
        self._counting_capture(
            monkeypatch, lambda: RpcCapture(body=rejected, rpc_urls=[], nudged=False)
        )
        options = BrowserOptions(profile_dir=Path("/tmp/fli-test-profile"))
        with pytest.raises(BrowserAttestationRejectedError, match="/tmp/fli-test-profile"):
            SearchMultiCity(options=options).search(_filters())

    def test_a_timeout_propagates_without_a_second_page_load(self, monkeypatch):
        """The nudge lives inside the transport; nothing above it retries."""

        def timeout():
            raise BrowserRpcTimeoutError("never fired")

        calls = self._counting_capture(monkeypatch, timeout)
        with pytest.raises(BrowserRpcTimeoutError):
            SearchMultiCity().search(_filters())
        assert len(calls) == 1

    def test_a_shape_change_is_captured_exactly_once(self, monkeypatch):
        """Nothing about a wire change gets better by asking again."""
        calls = self._counting_capture(
            monkeypatch,
            lambda: RpcCapture(body=_wrb('[["nope"]]'), rpc_urls=[], nudged=False),
        )
        with pytest.raises(BrowserDecodeError):
            SearchMultiCity().search(_filters())
        assert len(calls) == 1

    def test_an_empty_board_is_captured_exactly_once(self, monkeypatch):
        """'No itineraries' is an answer; asking twice does not improve it."""
        calls = self._counting_capture(
            monkeypatch,
            lambda: RpcCapture(body=_wrb("[null,null,[[]]]"), rpc_urls=[], nudged=False),
        )
        assert SearchMultiCity().search(_filters()) is None
        assert len(calls) == 1
