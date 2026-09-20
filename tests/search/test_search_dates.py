"""Tests for SearchDates class."""

from datetime import datetime, timedelta, timezone

import pytest

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
)
from fli.models.google_flights.base import TripType
from fli.search import SearchDates


@pytest.fixture
def search():
    """Create a reusable SearchDates instance."""
    return SearchDates()


@pytest.fixture
def basic_search_params():
    """Create basic date search params for testing."""
    today = datetime.now()
    future_date = today + timedelta(days=30)
    return DateSearchFilters(
        passenger_info=PassengerInfo(
            adults=1,
            children=0,
            infants_in_seat=0,
            infants_on_lap=0,
        ),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.PHX, 0]],
                arrival_airport=[[Airport.SFO, 0]],
                travel_date=future_date.strftime("%Y-%m-%d"),
            )
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
        from_date=(future_date - timedelta(days=30)).strftime("%Y-%m-%d"),
        to_date=(future_date + timedelta(days=30)).strftime("%Y-%m-%d"),
    )


@pytest.fixture
def complex_search_params():
    """Create more complex date search params for testing."""
    today = datetime.now()
    future_date = today + timedelta(days=60)
    return DateSearchFilters(
        passenger_info=PassengerInfo(
            adults=2,
            children=1,
            infants_in_seat=0,
            infants_on_lap=1,
        ),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.JFK, 0]],
                arrival_airport=[[Airport.LAX, 0]],
                travel_date=future_date.strftime("%Y-%m-%d"),
            )
        ],
        stops=MaxStops.ONE_STOP_OR_FEWER,
        seat_type=SeatType.FIRST,
        sort_by=SortBy.TOP_FLIGHTS,
        from_date=(future_date - timedelta(days=30)).strftime("%Y-%m-%d"),
        to_date=(future_date + timedelta(days=30)).strftime("%Y-%m-%d"),
    )


@pytest.fixture
def round_trip_search_params():
    """Create basic round trip search params for testing."""
    today = datetime.now()
    outbound_date = today + timedelta(days=30)
    return_date = outbound_date + timedelta(days=7)

    return DateSearchFilters(
        passenger_info=PassengerInfo(
            adults=1,
            children=0,
            infants_in_seat=0,
            infants_on_lap=0,
        ),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.SFO, 0]],
                arrival_airport=[[Airport.JFK, 0]],
                travel_date=outbound_date.strftime("%Y-%m-%d"),
            ),
            FlightSegment(
                departure_airport=[[Airport.JFK, 0]],
                arrival_airport=[[Airport.SFO, 0]],
                travel_date=return_date.strftime("%Y-%m-%d"),
            ),
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
        trip_type=TripType.ROUND_TRIP,
        from_date=(outbound_date - timedelta(days=30)).strftime("%Y-%m-%d"),
        to_date=(outbound_date + timedelta(days=30)).strftime("%Y-%m-%d"),
    )


@pytest.fixture
def complex_round_trip_params():
    """Create more complex round trip search params for testing."""
    today = datetime.now()
    outbound_date = today + timedelta(days=60)
    return_date = outbound_date + timedelta(days=14)

    return DateSearchFilters(
        passenger_info=PassengerInfo(
            adults=2,
            children=1,
            infants_in_seat=0,
            infants_on_lap=1,
        ),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.LAX, 0]],
                arrival_airport=[[Airport.ORD, 0]],
                travel_date=outbound_date.strftime("%Y-%m-%d"),
            ),
            FlightSegment(
                departure_airport=[[Airport.ORD, 0]],
                arrival_airport=[[Airport.LAX, 0]],
                travel_date=return_date.strftime("%Y-%m-%d"),
            ),
        ],
        stops=MaxStops.ONE_STOP_OR_FEWER,
        seat_type=SeatType.BUSINESS,
        sort_by=SortBy.TOP_FLIGHTS,
        trip_type=TripType.ROUND_TRIP,
        from_date=(outbound_date - timedelta(days=30)).strftime("%Y-%m-%d"),
        to_date=(outbound_date + timedelta(days=30)).strftime("%Y-%m-%d"),
    )


# Google's search page inlines no results for some searches carrying infant
# passengers — the same query with adults and children returns rows, and the
# same infant query returns rows on other routes. Nothing in the request is
# rejected: the page simply comes back without a results grid, so these
# searches yield an empty list. Marked non-strict so a fix on Google's side
# shows up as an unexpected pass rather than a failure.
INFANT_RESULTS_MISSING = pytest.mark.xfail(
    reason="Google's page serves no inline results for this infant search",
    strict=False,
)


@pytest.mark.live
@pytest.mark.parametrize(
    "search_params_fixture",
    [
        "basic_search_params",
        pytest.param("complex_search_params", marks=INFANT_RESULTS_MISSING),
    ],
)
def test_search_functionality(search, search_params_fixture, request):
    """Test date search functionality with different data sets."""
    search_params = request.getfixturevalue(search_params_fixture)
    results = search.search(search_params)
    assert isinstance(results, list)


@pytest.mark.live
@INFANT_RESULTS_MISSING
def test_multiple_searches(search, basic_search_params, complex_search_params):
    """Test performing multiple searches with the same SearchDates instance."""
    # First search
    results1 = search.search(basic_search_params)
    assert isinstance(results1, list)

    # Second search with different data
    results2 = search.search(complex_search_params)
    assert isinstance(results2, list)

    # Third search reusing first search data
    results3 = search.search(basic_search_params)
    assert isinstance(results3, list)


@pytest.mark.live
def test_date_price_sorting(search, basic_search_params):
    """Test that date prices are sorted chronologically."""
    results = search.search(basic_search_params)
    assert len(results) > 0

    # Verify dates are sorted
    dates = [result.date[0] for result in results]  # Get first date from tuple
    assert dates == sorted(dates)


SHOPPING_TOKEN = (
    "CjRIQktCNmV1UjNqNjhBR043X0FCRy0tLS0tLS0tLS12dGpkN0FBQUFBR25JcWZNS2pGTTBBEgZV"
    "QTIyMDkaCgjcWxACGgNVU0Q4HHDcWw=="
)

CALENDAR_ITEM = ["2026-04-28", None, [[None, 118], SHOPPING_TOKEN], 1]


def test_parse_currency_from_calendar_item():
    """Calendar rows should expose the returned currency code."""
    assert SearchDates._SearchDates__parse_currency(CALENDAR_ITEM) == "USD"


def test_parse_price_from_calendar_item():
    """Calendar rows should keep using the numeric display price."""
    assert SearchDates._SearchDates__parse_price(CALENDAR_ITEM) == 118.0


@pytest.mark.live
def test_basic_round_trip_search(search, round_trip_search_params):
    """Test basic round trip date search functionality."""
    results = search.search(round_trip_search_params)
    assert isinstance(results, list)
    assert len(results) > 0

    # Verify date range
    from_date = datetime.strptime(round_trip_search_params.from_date, "%Y-%m-%d")
    to_date = datetime.strptime(round_trip_search_params.to_date, "%Y-%m-%d")

    for result in results:
        # For round trips, date is a tuple of (outbound_date, return_date)
        outbound_date, return_date = result.date
        assert from_date.date() <= outbound_date.date() <= to_date.date()
        assert outbound_date.date() <= return_date.date()  # Return can be same day or later
        assert hasattr(result, "price")
        assert result.price > 0


@pytest.mark.live
def test_complex_round_trip_search(search, complex_round_trip_params):
    """Test complex round trip date search with multiple passengers and stops."""
    results = search.search(complex_round_trip_params)
    assert isinstance(results, list)
    assert len(results) > 0

    # Verify date range
    from_date = datetime.strptime(complex_round_trip_params.from_date, "%Y-%m-%d")
    to_date = datetime.strptime(complex_round_trip_params.to_date, "%Y-%m-%d")

    for result in results:
        # For round trips, date is a tuple of (outbound_date, return_date)
        outbound_date, return_date = result.date
        assert from_date.date() <= outbound_date.date() <= to_date.date()
        assert outbound_date.date() <= return_date.date()  # Return can be same day or later
        assert hasattr(result, "price")
        assert result.price > 0


@pytest.mark.live
@pytest.mark.parametrize(
    "search_params_fixture",
    [
        "round_trip_search_params",
        "complex_round_trip_params",
    ],
)
def test_round_trip_result_structure(search, search_params_fixture, request):
    """Test the structure of round trip date search results with different parameters."""
    search_params = request.getfixturevalue(search_params_fixture)
    results = search.search(search_params)

    assert isinstance(results, list)
    assert len(results) > 0

    # Verify chronological order of outbound dates
    outbound_dates = [result.date[0] for result in results]
    assert outbound_dates == sorted(outbound_dates)

    # Verify result structure
    for result in results:
        assert isinstance(result.date, tuple)
        assert len(result.date) == 2  # Should have outbound and return dates
        outbound_date, return_date = result.date
        assert isinstance(outbound_date, datetime)
        assert isinstance(return_date, datetime)
        assert outbound_date <= return_date  # Return can be same day or later
        assert hasattr(result, "price")
        assert result.price > 0


class TestRoundTripDurationFallback:
    """A round trip with no explicit ``duration`` still prices a return leg.

    ``DateSearchFilters.duration`` is optional and its validator doesn't run
    on the default, so round-trip filters can reach the search with it unset.
    The return date then comes from the gap between the two segments.
    """

    def test_return_date_derived_from_segments(self, monkeypatch, round_trip_search_params):
        import base64
        from pathlib import Path

        from fli.search._wire import iter_wrb_chunks
        from tests.search._pages import as_search_page

        assert round_trip_search_params.duration is None
        page = as_search_page(
            next(
                iter_wrb_chunks(
                    (
                        Path(__file__).parent / "fixtures" / "flight_search_jfk_lax_oneway_usd.bin"
                    ).read_text()
                )
            )
        )
        urls: list[str] = []

        def _fake_get(url, **kwargs):
            urls.append(url)
            return type("R", (), {"text": page, "raise_for_status": lambda s: None})()

        search = SearchDates()
        monkeypatch.setattr(search.client, "get", _fake_get)
        results = search.search(round_trip_search_params)

        assert results and all(len(r.date) == 2 for r in results)
        # Outbound and return are 7 days apart in the fixture's segments.
        assert all((r.date[1] - r.date[0]).days == 7 for r in results)
        tfs = urls[0].split("tfs=")[1].split("&")[0]
        decoded = base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4)).decode("latin-1")
        for r in results[:1]:
            assert r.date[0].strftime("%Y-%m-%d") in decoded
            assert r.date[1].strftime("%Y-%m-%d") in decoded


def test_price_one_date_does_not_skip_utc_today_under_eastern_tz(
    search, basic_search_params, tz_override
):
    """Test the sweep's past-date guard does not drop today's date on a skewed host.

    ``_price_one_date`` short-circuits dates it considers past. Anchored to the
    naive server clock, a host running at UTC+14 considers today-in-UTC already
    gone and silently drops it from a date sweep - the same defect the segment
    validator has, in the code path added after the search transport was
    rewritten.
    """
    tz_override("Etc/GMT-14")

    calls = []

    class _RecordingClient:
        def get(self, url, **kwargs):
            calls.append(url)
            raise RuntimeError("stop after the guard")

    search.client = _RecordingClient()
    today_utc = datetime.now(timezone.utc).date()

    search._price_one_date(
        basic_search_params,
        datetime(today_utc.year, today_utc.month, today_utc.day),
        currency=None,
        language=None,
        country=None,
    )

    assert calls, "past-date guard short-circuited a date that is still today in UTC"


def _fixture_search_page() -> str:
    """Return one real Google search page, replayed for every fake request."""
    from pathlib import Path

    from fli.search._wire import iter_wrb_chunks
    from tests.search._pages import as_search_page

    return as_search_page(
        next(
            iter_wrb_chunks(
                (
                    Path(__file__).parent / "fixtures" / "flight_search_jfk_lax_oneway_usd.bin"
                ).read_text()
            )
        )
    )


def _tfs_dates(url: str) -> list[str]:
    """Decode a request URL's ``tfs`` token and return the dates it encodes."""
    import base64
    import re

    token = url.split("tfs=")[1].split("&")[0]
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("latin-1")
    return re.findall(r"\d{4}-\d{2}-\d{2}", decoded)


class TestSearchDurations:
    """``SearchDates.search_durations`` sweeps a range of trip lengths.

    Every test here is offline: ``client.get`` is replaced with a replay of a
    recorded search page, and the request URLs are collected so the sweep's
    actual wire behaviour can be asserted.
    """

    @pytest.fixture
    def replayed(self, monkeypatch):
        """Yield a ``SearchDates`` whose GETs are replayed, plus the URL log."""
        page = _fixture_search_page()
        urls: list[str] = []

        def _fake_get(url, **kwargs):
            urls.append(url)
            return type("R", (), {"text": page, "raise_for_status": lambda s: None})()

        search = SearchDates()
        monkeypatch.setattr(search.client, "get", _fake_get)
        return search, urls

    @staticmethod
    def _days_in_range(filters: DateSearchFilters) -> int:
        return (filters.parsed_to_date - filters.parsed_from_date).days + 1

    def test_each_duration_is_actually_searched(self, replayed, round_trip_search_params):
        """Two durations must cost two full sweeps, not one repeated."""
        search, urls = replayed
        days = self._days_in_range(round_trip_search_params)

        search.search_durations(round_trip_search_params, [4, 5])

        assert len(urls) == 2 * days

    @pytest.mark.parametrize("base_duration", [None, 7])
    def test_tfs_encodes_the_return_date_for_each_duration(
        self, replayed, round_trip_search_params, base_duration
    ):
        """Decode the wire token: half the requests carry +4, half carry +5.

        ``build_tfs`` takes its dates from ``_price_one_date``, which reads
        ``filters.duration`` and falls back to the gap between the two flight
        segments when it is unset. A sweep that copies the filters but updates
        neither silently reprices the same itinerary N times, and every other
        assertion still passes. Both base filters are exercised because the CLI
        and MCP always set ``duration`` while a hand-built filter may not, so
        each of the two paths has to carry the sweep on its own.
        """
        search, urls = replayed
        round_trip_search_params.duration = base_duration
        days = self._days_in_range(round_trip_search_params)

        search.search_durations(round_trip_search_params, [4, 5])

        gaps: list[int] = []
        for url in urls:
            encoded = _tfs_dates(url)
            assert len(encoded) == 2, f"expected an outbound and a return date, got {encoded}"
            out, back = (datetime.strptime(d, "%Y-%m-%d") for d in encoded)
            gaps.append((back - out).days)

        assert gaps.count(4) == days
        assert gaps.count(5) == days

    def test_merged_results_are_deduplicated_and_chronological(
        self, replayed, round_trip_search_params
    ):
        """The merged table reads by departure date, then by price."""
        search, _ = replayed

        results = search.search_durations(round_trip_search_params, [4, 5])

        assert results
        assert {(r.date[1] - r.date[0]).days for r in results} == {4, 5}
        keys = [tuple(r.date) for r in results]
        assert len(keys) == len(set(keys))
        assert results == sorted(results, key=lambda r: (r.date[0], r.price))

    @pytest.mark.parametrize("durations", [[4], [None]])
    def test_single_duration_delegates_straight_to_search(
        self, monkeypatch, round_trip_search_params, durations
    ):
        """The one-duration path must stay byte-for-byte the old behaviour."""
        search = SearchDates()
        sentinel = [object()]
        seen: list = []

        def _fake_search(filters, currency=None, language=None, country=None):
            seen.append(filters)
            return sentinel

        monkeypatch.setattr(search, "search", _fake_search)

        assert search.search_durations(round_trip_search_params, durations) is sentinel
        assert seen == [round_trip_search_params]
        assert seen[0] is round_trip_search_params

    def test_caller_filters_are_left_untouched(self, replayed, round_trip_search_params):
        """The sweep must not mutate the object the caller handed it."""
        search, _ = replayed
        before_duration = round_trip_search_params.duration
        before_return = round_trip_search_params.flight_segments[1].travel_date

        search.search_durations(round_trip_search_params, [4, 5])

        assert round_trip_search_params.duration == before_duration
        assert round_trip_search_params.flight_segments[1].travel_date == before_return

    def test_every_filter_field_survives_each_duration_variant(self, monkeypatch):
        """Hand-rebuilding filters per duration is what silently drops fields."""
        from fli.models import Airline, Alliance, LayoverRestrictions

        outbound = datetime.now() + timedelta(days=30)
        filters = DateSearchFilters(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=2, children=2, infants_in_seat=0, infants_on_lap=1),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport.SFO, 0]],
                    arrival_airport=[[Airport.JFK, 0]],
                    travel_date=outbound.strftime("%Y-%m-%d"),
                ),
                FlightSegment(
                    departure_airport=[[Airport.JFK, 0]],
                    arrival_airport=[[Airport.SFO, 0]],
                    travel_date=(outbound + timedelta(days=4)).strftime("%Y-%m-%d"),
                ),
            ],
            stops=MaxStops.NON_STOP,
            seat_type=SeatType.BUSINESS,
            airlines=[Airline.BA],
            airlines_exclude=[Airline.DL],
            alliances=[Alliance.ONEWORLD],
            alliances_exclude=[Alliance.SKYTEAM],
            layover_restrictions=LayoverRestrictions(min_duration=60, max_duration=240),
            from_date=outbound.strftime("%Y-%m-%d"),
            to_date=(outbound + timedelta(days=2)).strftime("%Y-%m-%d"),
            duration=4,
        )

        search = SearchDates()
        variants: list[DateSearchFilters] = []

        def _fake_search(f, currency=None, language=None, country=None):
            variants.append(f)
            return None

        monkeypatch.setattr(search, "search", _fake_search)
        search.search_durations(filters, [4, 5, 6])

        assert len(variants) == 3
        for variant, duration in zip(variants, [4, 5, 6], strict=True):
            assert variant.duration == duration
            gap = (
                variant.flight_segments[1].parsed_travel_date
                - variant.flight_segments[0].parsed_travel_date
            ).days
            assert gap == duration
            assert variant.passenger_info.children == 2
            assert variant.passenger_info.infants_on_lap == 1
            assert variant.airlines == [Airline.BA]
            assert variant.airlines_exclude == [Airline.DL]
            assert variant.alliances == [Alliance.ONEWORLD]
            assert variant.alliances_exclude == [Alliance.SKYTEAM]
            assert variant.layover_restrictions.min_duration == 60
            assert variant.layover_restrictions.max_duration == 240
            assert variant.seat_type == SeatType.BUSINESS
            assert variant.stops == MaxStops.NON_STOP

    def test_wide_sweep_neither_sleeps_nor_deadlocks(self, replayed):
        """A 12-duration sweep must finish promptly.

        ``_search_chunk`` already fans out over the single shared worker pool,
        so wrapping the duration loop in another ``parallel_map`` deadlocks once
        the duration count reaches the worker count. A per-duration
        ``time.sleep`` would also show up here.
        """
        import time

        search, urls = replayed
        outbound = datetime.now() + timedelta(days=30)
        filters = DateSearchFilters(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport.SFO, 0]],
                    arrival_airport=[[Airport.JFK, 0]],
                    travel_date=outbound.strftime("%Y-%m-%d"),
                ),
                FlightSegment(
                    departure_airport=[[Airport.JFK, 0]],
                    arrival_airport=[[Airport.SFO, 0]],
                    travel_date=(outbound + timedelta(days=1)).strftime("%Y-%m-%d"),
                ),
            ],
            stops=MaxStops.ANY,
            seat_type=SeatType.ECONOMY,
            from_date=outbound.strftime("%Y-%m-%d"),
            to_date=(outbound + timedelta(days=1)).strftime("%Y-%m-%d"),
            duration=1,
        )

        started = time.monotonic()
        search.search_durations(filters, list(range(1, 13)))
        elapsed = time.monotonic() - started

        assert len(urls) == 12 * 2
        assert elapsed < 5, f"12-duration sweep took {elapsed:.1f}s — sleeping or deadlocking?"


def _tfs_carriers(url: str) -> bytes:
    """Decode a request URL's ``tfs`` token to raw bytes for field assertions."""
    import base64

    token = url.split("tfs=")[1].split("&")[0]
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


class TestCarrierFiltersReachTheWire:
    """Airline filters must be encoded into ``tfs``, not dropped.

    The date sweep returns ``(date, price)`` pairs with no itinerary behind
    them, so there is nothing for a client-side filter to work on — if the
    carrier lists do not reach the request, the flag is silently ignored.
    Every test here is offline: ``client.get`` replays a recorded page and
    the request URLs are decoded back into protobuf bytes.
    """

    @pytest.fixture
    def replayed(self, monkeypatch):
        """Yield a ``SearchDates`` whose GETs are replayed, plus the URL log."""
        page = _fixture_search_page()
        urls: list[str] = []

        def _fake_get(url, **kwargs):
            urls.append(url)
            return type("R", (), {"text": page, "raise_for_status": lambda s: None})()

        search = SearchDates()
        monkeypatch.setattr(search.client, "get", _fake_get)
        return search, urls

    @staticmethod
    def _filters(**kwargs) -> DateSearchFilters:
        outbound = datetime.now() + timedelta(days=30)
        return DateSearchFilters(
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport.SFO, 0]],
                    arrival_airport=[[Airport.JFK, 0]],
                    travel_date=outbound.strftime("%Y-%m-%d"),
                )
            ],
            stops=MaxStops.ANY,
            seat_type=SeatType.ECONOMY,
            from_date=outbound.strftime("%Y-%m-%d"),
            to_date=(outbound + timedelta(days=2)).strftime("%Y-%m-%d"),
            **kwargs,
        )

    def test_excluded_airline_reaches_every_request(self, replayed):
        """``--exclude-airlines BA`` must ride in each date's ``tfs`` token."""
        from fli.models import Airline

        search, urls = replayed
        search.search(self._filters(airlines_exclude=[Airline.BA]))

        assert urls, "no requests were made"
        for url in urls:
            assert b"\x3a\x02BA" in _tfs_carriers(url)

    def test_included_airline_reaches_every_request(self, replayed):
        """``--airlines AA`` rides in the include list, not the exclude one."""
        from fli.models import Airline

        search, urls = replayed
        search.search(self._filters(airlines=[Airline.AA]))

        assert urls, "no requests were made"
        for url in urls:
            raw = _tfs_carriers(url)
            assert b"\x32\x02AA" in raw
            assert b"\x3a\x02AA" not in raw

    def test_chunked_ranges_keep_every_carrier_filter(self):
        """A range past ``MAX_DAYS_PER_SEARCH`` must not drop filters per chunk."""
        from fli.models import Airline, Alliance

        search = SearchDates()
        outbound = datetime.now() + timedelta(days=30)
        filters = self._filters(
            airlines=[Airline.AA],
            airlines_exclude=[Airline.BA],
            alliances=[Alliance.ONEWORLD],
            alliances_exclude=[Alliance.SKYTEAM],
        )
        filters.to_date = (outbound + timedelta(days=90)).strftime("%Y-%m-%d")

        chunks = search._build_chunk_filters(
            filters, filters.parsed_from_date, filters.parsed_to_date
        )

        assert len(chunks) > 1, "range should have split into several chunks"
        for chunk in chunks:
            assert chunk.airlines == [Airline.AA]
            assert chunk.airlines_exclude == [Airline.BA]
            assert chunk.alliances == [Alliance.ONEWORLD]
            assert chunk.alliances_exclude == [Alliance.SKYTEAM]
