"""``ExploreSearchFilters`` and the request payload it formats.

Adopted from upstream punitarani/fli#226 (alexechoi, ``feat/explore-destinations``),
whose ``tests/models/test_explore_search_filters.py`` this is. Taken almost
verbatim, because its value is the part that cannot be re-derived: the
expected payloads below are decoded from real HAR captures of the Explore
endpoint, so they pin ``format()`` against what Google was actually sent
rather than against what this package currently emits.

Worth having even though the endpoint it addresses is gated. ``format()`` and
``encode()`` are still live on the HTTP path — ``SearchExplore._fetch_over_http``
POSTs ``f.req={filters.encode()}`` — which this fork keeps in order to re-test
the gate, and which had no tests here at all.

The rest of #226's tests are deliberately not adopted: ``test_mcp_explore.py``
covers an MCP tool this fork does not ship (see ADOPTING.md, and
``tests/test_http_path_stays_on_http.py``), and ``test_search_explore_live.py``
asserts success against the gated endpoint, which fails by construction here.
Transport-level Explore coverage lives in
``tests/search/test_explore_transport.py`` instead, against a captured
streaming-shape body #226's own fixture cannot produce.
"""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from fli.models import (
    Airline,
    Airport,
    Alliance,
    BagsFilter,
    ExplorePlace,
    ExploreRegion,
    ExploreSearchFilters,
    MaxStops,
    PassengerInfo,
    PriceLimit,
    SeatType,
    TripType,
)


def get_future_date(days: int = 30) -> str:
    """Generate a future date string in YYYY-MM-DD format."""
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


DEPARTURE_DATE = get_future_date(30)

TEST_CASES = [
    {
        # Replicates the richest captured HAR request (idx 337): London city
        # -> Southern Europe with every filter set. The expected literal below
        # is the decoded f.req inner payload from the capture (date swapped
        # for a future one).
        "name": "HAR capture replication (all filters)",
        "search": ExploreSearchFilters(
            origin=ExplorePlace(mid="/m/04jpl", type_code=4),
            destination=ExplorePlace(mid="/m/0250wj", type_code=6),
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=1),
            seat_type=SeatType.ECONOMY,
            price_limit=PriceLimit(max_price=900),
            bags=BagsFilter(carry_on=True, checked_bags=0),
            trip_length_window=[4, 23, 0, 23],
            stops=MaxStops.NON_STOP,
            alliances=[Alliance.ONEWORLD],
            departure_date=DEPARTURE_DATE,
            max_duration=600,
        ),
        "formatted": [
            [],
            None,
            None,
            [
                None,
                None,
                2,
                None,
                [],
                1,
                [1, 0, 0, 0],
                [None, 900],
                None,
                None,
                [1, 0],
                None,
                None,
                [
                    [
                        [[["/m/04jpl", 4]]],
                        [[["/m/0250wj", 6]]],
                        [4, 23, 0, 23],
                        1,
                        ["ONEWORLD"],
                        None,
                        DEPARTURE_DATE,
                        [600],
                    ]
                ],
                None,
                None,
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                1,
            ],
            None,
            1,
            None,
            0,
            None,
            0,
            [447, 712],
            3,
        ],
    },
    {
        "name": "Minimal: airport origin to ANYWHERE",
        "search": ExploreSearchFilters(origin=Airport.JFK, departure_date=DEPARTURE_DATE),
        "formatted": [
            [],
            None,
            None,
            [
                None,
                None,
                2,
                None,
                [],
                1,
                [1, 0, 0, 0],
                None,
                None,
                None,
                None,
                None,
                None,
                [
                    [
                        [[["JFK", 0]]],
                        [[["/m/02j71", 6]]],
                        None,
                        0,
                        None,
                        None,
                        DEPARTURE_DATE,
                        None,
                    ]
                ],
                None,
                None,
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                1,
            ],
            None,
            1,
            None,
            0,
            None,
            0,
            [447, 712],
            3,
        ],
    },
    {
        "name": "Airlines and region enum destination",
        "search": ExploreSearchFilters(
            origin=Airport.LHR,
            destination=ExploreRegion.EUROPE,
            departure_date=DEPARTURE_DATE,
            airlines=[Airline.BA, Airline.AA],
            airlines_exclude=[Airline.FR],
        ),
        "formatted": [
            [],
            None,
            None,
            [
                None,
                None,
                2,
                None,
                [],
                1,
                [1, 0, 0, 0],
                None,
                None,
                None,
                None,
                None,
                None,
                [
                    [
                        [[["LHR", 0]]],
                        [[["/m/02j9z", 6]]],
                        None,
                        0,
                        ["AA", "BA"],
                        ["FR"],
                        DEPARTURE_DATE,
                        None,
                    ]
                ],
                None,
                None,
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                1,
            ],
            None,
            1,
            None,
            0,
            None,
            0,
            [447, 712],
            3,
        ],
    },
]


@pytest.mark.parametrize("test_case", TEST_CASES, ids=[tc["name"] for tc in TEST_CASES])
def test_explore_search_filters_format(test_case):
    """Test explore filters format() against expected wire payloads."""
    assert test_case["search"].format() == test_case["formatted"]


def test_encode_wraps_and_urlencodes():
    """encode() must produce the double-encoded f.req value."""
    filters = ExploreSearchFilters(origin=Airport.JFK, departure_date=DEPARTURE_DATE)
    encoded = filters.encode()
    assert encoded.startswith("%5Bnull%2C%22%5B")  # [null,"[...
    assert "%20" not in encoded  # compact separators, no spaces


def test_multi_city_rejected():
    with pytest.raises(ValidationError, match="multi-city"):
        ExploreSearchFilters(
            origin=Airport.JFK, departure_date=DEPARTURE_DATE, trip_type=TripType.MULTI_CITY
        )


def test_departure_date_required():
    """The endpoint errors without a date, so the model requires one upfront."""
    with pytest.raises(ValidationError, match="departure_date"):
        ExploreSearchFilters(origin=Airport.JFK)


def test_past_departure_date_rejected():
    past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    with pytest.raises(ValidationError, match="past"):
        ExploreSearchFilters(origin=Airport.JFK, departure_date=past)


def test_bad_mid_rejected():
    with pytest.raises(ValidationError, match="knowledge-graph"):
        ExplorePlace(mid="LON")


def test_same_origin_destination_rejected():
    with pytest.raises(ValidationError, match="same place"):
        ExploreSearchFilters(
            origin=ExplorePlace(mid="/m/02j9z", type_code=6),
            destination=ExploreRegion.EUROPE,
            departure_date=DEPARTURE_DATE,
        )


def test_bags_order_is_carry_on_first():
    """HAR-confirmed: explore bags slot is [carry_on, checked] (reverse of dates)."""
    filters = ExploreSearchFilters(
        origin=Airport.JFK,
        departure_date=DEPARTURE_DATE,
        bags=BagsFilter(carry_on=False, checked_bags=2),
    )
    assert filters.format()[3][10] == [0, 2]


class TestThisForksAdditions:
    """Behaviour #226 has no equivalent of, pinned alongside its own cases."""

    def test_the_grace_window_is_the_project_wide_one(self):
        """Explore was the last filter model comparing against local midnight.

        ``earliest_searchable_date`` is ``utc_today - 1`` because real UTC
        offsets span UTC-12 to UTC+14: a UTC container would otherwise
        reject a genuine same-day San Francisco evening departure for the
        last seven hours of every Pacific day. Asserted against the helper
        rather than against a hardcoded day, so it cannot drift from the
        rule the other models follow.
        """
        from datetime import timedelta

        from fli.models.google_flights.base import earliest_searchable_date

        earliest = earliest_searchable_date()
        accepted = ExploreSearchFilters(
            origin=Airport.JFK, departure_date=earliest.strftime("%Y-%m-%d")
        )
        assert accepted.departure_date == earliest.strftime("%Y-%m-%d")

        with pytest.raises(ValidationError, match="past"):
            ExploreSearchFilters(
                origin=Airport.JFK,
                departure_date=(earliest - timedelta(days=1)).strftime("%Y-%m-%d"),
            )

    def test_trip_length_is_a_preset_not_a_range(self):
        """The three lengths Google's Explore UI actually offers.

        ``trip_length_window`` remains constructible -- it is the page-URL
        builder that refuses it, not the model, because the old RPC shape
        this model still formats did carry a window.
        """
        from fli.models.google_flights.explore import ExploreTripLength

        assert [t.value for t in ExploreTripLength] == [1, 2, 3]
        filters = ExploreSearchFilters(
            origin=Airport.JFK,
            departure_date=DEPARTURE_DATE,
            trip_type=TripType.ROUND_TRIP,
            trip_length=ExploreTripLength.TWO_WEEKS,
        )
        assert filters.trip_length is ExploreTripLength.TWO_WEEKS

    def test_transfer_minutes_replaced_layover_minutes(self):
        """summary[8] is a drive from the served airport, not a layover."""
        from fli.models.google_flights.explore import ExploreDestination

        assert "transfer_minutes" in ExploreDestination.model_fields
        assert "transfer_city" in ExploreDestination.model_fields
        assert "layover_minutes" not in ExploreDestination.model_fields
