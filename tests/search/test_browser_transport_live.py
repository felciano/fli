r"""Live end-to-end checks of the browser transport. Double-gated, never in CI.

Google gates these RPCs precisely to stop automated use, so this file spends a
strict budget: **one page load per test**, three in total, run by hand.
Everything that can be tested against a committed fixture is tested there
instead (``test_multi_city.py``, ``test_explore_transport.py``), and everything
about the orchestration is tested against a fake browser
(``test_browser_transport.py``).

Run it with::

    FLI_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222 \\
        uv run --with playwright pytest tests/search/test_browser_transport_live.py --browser

Both gates are deliberate. ``--browser`` says the runner meant to generate
live traffic; ``FLI_BROWSER_CDP_ENDPOINT`` says a browser is actually there to
attach to. ``--all`` does **not** enable this file: a blanket "run everything"
must not quietly start hitting a gated endpoint.

A note on the itineraries below, because getting this wrong cost a day
-----------------------------------------------------------------------
An earlier version of this file searched ``LHR→BOS`` then ``BOS→LHR`` and was
left ``xfail`` after the RPC never fired. The RPC was not the problem: **that
is not a multi-city itinerary.** Google normalises an ``A→B``/``B→A`` pair
into a round trip, and a round trip is served inline in the page's ``ds:1``
payload with no ``batchexecute`` call at all — correctly, since the HTTP
transport can already read it.

A genuine multi-city search — three legs, or an open jaw — inlines nothing and
*does* fire ``GetShoppingResults``. Verified 2026-09-20: 5 frames, 62 rows, 15
itineraries, no nudge needed. So the itineraries here must stay
genuinely multi-city, and a failure here is a real finding rather than a
fixture artefact.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fli.models import (
    Airport,
    ExploreSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
)
from fli.models.google_flights.base import TripType
from fli.search import SearchExplore, SearchMultiCity
from fli.search._browser import BrowserOptions, capture_rpc_body
from fli.search._capture import decode_shopping_capture
from fli.search._tfs import build_multi_city_tfs, page_url
from fli.search.multi_city import SHOPPING_RPC_MARKER

pytestmark = pytest.mark.browser


def _dates() -> tuple[str, str, str]:
    """Return three far-future dates, so the boards are never empty."""
    first = date.today() + timedelta(days=90)
    return (
        first.isoformat(),
        (first + timedelta(days=7)).isoformat(),
        (first + timedelta(days=14)).isoformat(),
    )


def _multi_city_filters() -> FlightSearchFilters:
    """Build a genuinely multi-city itinerary — three legs, no A→B→A pair."""
    one, two, three = _dates()
    legs = [
        (Airport.LHR, Airport.BOS, one),
        (Airport.BOS, Airport.CDG, two),
        (Airport.CDG, Airport.LHR, three),
    ]
    return FlightSearchFilters(
        trip_type=TripType.MULTI_CITY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[dest, 0]],
                travel_date=travel_date,
            )
            for origin, dest, travel_date in legs
        ],
    )


def test_intercepts_and_decodes_a_live_multi_city_board():
    """One live capture, end to end, through the unchanged decode pipeline.

    Asserts the three things that cannot be learned offline: that the RPC
    still fires and is still interceptable, that what comes back still
    carries rows where ``parse_flight_row`` expects them, and that the frames
    still need merging.
    """
    filters = _multi_city_filters()
    url = page_url(build_multi_city_tfs(filters), "USD", "en", "US")

    capture = capture_rpc_body(
        url, rpc_marker=SHOPPING_RPC_MARKER, options=BrowserOptions.from_env()
    )

    assert capture.body, "the RPC fired but carried an empty body"
    assert any(SHOPPING_RPC_MARKER in rpc for rpc in capture.rpc_urls)

    flights = decode_shopping_capture(capture.body, context="live multi-city")
    assert flights, "the board decoded to no itineraries"
    assert all(flight.legs for flight in flights)

    first_leg_only = {
        (flight.legs[0].departure_airport, flight.legs[-1].arrival_airport) for flight in flights
    }
    assert first_leg_only == {(Airport.LHR, Airport.BOS)}, (
        "every row should be an option for leg 1 — if this changes, the board "
        "is no longer a first-leg board and MultiCityBoard's contract is wrong"
    )


def test_search_multi_city_returns_a_board():
    """The public entry point, live: one page load, one labelled board."""
    board = SearchMultiCity().search(_multi_city_filters(), currency="USD")

    assert board is not None, "no multi-city itineraries at all is suspicious here"
    assert board.price_basis == "entire_trip"
    assert board.board_leg_index == 0
    assert len(board.legs) == 3
    assert board.booking_url.startswith("https://www.google.com/travel/flights?tfs=")

    priced = [result.price for result in board.results if result.price is not None]
    assert priced == sorted(priced), "the board must come back cheapest first"
    # An entire-trip LHR→BOS→CDG→LHR fare that is under a few hundred is not
    # an entire-trip fare. Measured 2026-09-20: 1161–2516 USD, against
    # 858–924 USD for the same LHR→BOS flights as a one-way.
    assert priced and min(priced) > 500, (
        f"cheapest board price {min(priced) if priced else None} looks like a "
        "single-leg fare, not an entire-trip one — check price_basis"
    )


def test_search_explore_works_again():
    """Explore was a transport problem, not a decoder one. Live proof.

    Over HTTP this call dies with ``SearchRejectedError`` error 13. Through
    the browser it returns a full board, decoded by parsers this change did
    not touch.
    """
    departure, _, _ = _dates()
    result = SearchExplore().search(
        ExploreSearchFilters(origin=Airport.LHR, departure_date=departure),
        currency="USD",
    )

    assert result is not None, "Explore returned nothing parseable"
    assert len(result.destinations) > 20, "an 'anywhere' board should be large"
    assert result.origin_name, "the board should name the origin it searched"
    priced = [d for d in result.destinations if d.price is not None]
    assert priced, "no destination came back with a fare"
