from datetime import date, datetime, timedelta, timezone

import pytest

from fli.core.builders import build_date_search_segments, build_flight_segments, normalize_date
from fli.core.parsers import ParseError
from fli.models import Airport, TimeRestrictions, TripType


def _future_date(days_ahead: int) -> str:
    """Return a date this many days from now, in YYYY-MM-DD format.

    Travel dates here are arbitrary — the tests care about how the builders
    shape segments, not about which day is requested — but they still pass
    through ``FlightSegment``, which rejects a date in the past. Hardcoded
    literals therefore turn into failures on a date chosen by whoever wrote
    them; deriving from today means they cannot.
    """
    today_utc = datetime.now(timezone.utc).date()
    return (today_utc + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _fifth_of_a_future_month(months_ahead: int = 4) -> date:
    """Return the 5th of a month a few months from now.

    Pinning the day to a single digit guarantees the unpadded rendering
    below really differs from the ISO one, so the normalization tests stay
    meaningful whatever today happens to be — a computed date that landed
    on the 15th would compare "2027-1-15" against "2027-01-15" on the month
    alone, and one in October would not exercise padding at all.
    """
    today_utc = datetime.now(timezone.utc).date()
    month = today_utc.month + months_ahead
    return date(today_utc.year + (month - 1) // 12, (month - 1) % 12 + 1, 5)


_DEPART = _fifth_of_a_future_month()
_RETURN = _DEPART + timedelta(days=7)

# Outbound/return and range start/end, kept 7 days apart as the originals were.
DEPART_DATE = _DEPART.strftime("%Y-%m-%d")
RETURN_DATE = _RETURN.strftime("%Y-%m-%d")
START_DATE = _future_date(180)
END_DATE = _future_date(187)

# The same two dates as a user might type them — no zero padding. The
# builders are expected to normalize these into DEPART_DATE / RETURN_DATE.
DEPART_DATE_LOOSE = f"{_DEPART.year}-{_DEPART.month}-{_DEPART.day}"
RETURN_DATE_LOOSE = f"{_RETURN.year}-{_RETURN.month}-{_RETURN.day}"


class TestNormalizeDate:
    """Tests for normalize_date."""

    def test_already_padded(self):
        assert normalize_date("2027-04-02") == "2027-04-02"

    def test_single_digit_month_and_day(self):
        assert normalize_date("2027-4-2") == "2027-04-02"

    def test_single_digit_day(self):
        assert normalize_date("2027-12-5") == "2027-12-05"

    def test_single_digit_month(self):
        assert normalize_date("2027-1-15") == "2027-01-15"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            normalize_date("not-a-date")

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            normalize_date("2027-13-01")


class TestBuildFlightSegments:
    """Tests for date normalization in build_flight_segments."""

    def test_normalizes_departure_date(self):
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE_LOOSE,
        )
        assert segments[0].travel_date == DEPART_DATE

    def test_normalizes_return_date(self):
        segments, trip_type = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE_LOOSE,
            return_date=RETURN_DATE_LOOSE,
        )
        assert trip_type == TripType.ROUND_TRIP
        assert segments[0].travel_date == DEPART_DATE
        assert segments[1].travel_date == RETURN_DATE


class TestBuildDateSearchSegments:
    """Tests for date normalization in build_date_search_segments."""

    def test_normalizes_start_date(self):
        segments, _ = build_date_search_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            start_date=DEPART_DATE_LOOSE,
        )
        assert segments[0].travel_date == DEPART_DATE

    def test_normalizes_start_date_round_trip(self):
        segments, trip_type = build_date_search_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            start_date=DEPART_DATE_LOOSE,
            is_round_trip=True,
            trip_duration=7,
        )
        assert trip_type == TripType.ROUND_TRIP
        assert segments[0].travel_date == DEPART_DATE
        assert segments[1].travel_date == RETURN_DATE


class TestBuildFlightSegmentsMultiAirport:
    """Tests for multi-airport support in build_flight_segments."""

    def test_single_airport_wraps_to_list(self):
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=START_DATE,
        )
        assert segments[0].departure_airport == [[Airport.JFK, 0]]
        assert segments[0].arrival_airport == [[Airport.LAX, 0]]

    def test_list_of_origins(self):
        segments, _ = build_flight_segments(
            origin=[Airport.JFK, Airport.LGA],
            destination=Airport.LHR,
            departure_date=START_DATE,
        )
        assert segments[0].departure_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]
        assert segments[0].arrival_airport == [[Airport.LHR, 0]]

    def test_list_of_destinations(self):
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=[Airport.LHR, Airport.CDG],
            departure_date=START_DATE,
        )
        assert segments[0].departure_airport == [[Airport.JFK, 0]]
        assert segments[0].arrival_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]

    def test_lists_on_both_sides(self):
        segments, _ = build_flight_segments(
            origin=[Airport.JFK, Airport.LGA, Airport.EWR],
            destination=[Airport.LHR, Airport.CDG],
            departure_date=START_DATE,
        )
        assert segments[0].departure_airport == [
            [Airport.JFK, 0],
            [Airport.LGA, 0],
            [Airport.EWR, 0],
        ]
        assert segments[0].arrival_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]

    def test_round_trip_mirrors_multi_airport(self):
        segments, trip_type = build_flight_segments(
            origin=[Airport.JFK, Airport.LGA],
            destination=[Airport.LHR, Airport.CDG],
            departure_date=START_DATE,
            return_date=END_DATE,
        )
        assert trip_type == TripType.ROUND_TRIP
        assert segments[0].departure_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]
        assert segments[0].arrival_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]
        assert segments[1].departure_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]
        assert segments[1].arrival_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]


class TestBuildDateSearchSegmentsMultiAirport:
    """Tests for multi-airport support in build_date_search_segments."""

    def test_single_airport_wraps_to_list(self):
        segments, _ = build_date_search_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            start_date=START_DATE,
        )
        assert segments[0].departure_airport == [[Airport.JFK, 0]]
        assert segments[0].arrival_airport == [[Airport.LAX, 0]]

    def test_list_inputs_preserved(self):
        segments, _ = build_date_search_segments(
            origin=[Airport.JFK, Airport.LGA],
            destination=[Airport.LHR, Airport.CDG],
            start_date=START_DATE,
        )
        assert segments[0].departure_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]
        assert segments[0].arrival_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]

    def test_round_trip_mirrors_multi_airport(self):
        segments, trip_type = build_date_search_segments(
            origin=[Airport.JFK, Airport.LGA],
            destination=[Airport.LHR, Airport.CDG],
            start_date=START_DATE,
            is_round_trip=True,
            trip_duration=7,
        )
        assert trip_type == TripType.ROUND_TRIP
        assert segments[0].departure_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]
        assert segments[0].arrival_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]
        assert segments[1].departure_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]
        assert segments[1].arrival_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]


class TestReturnTimeRestrictions:
    """Tests for a return leg carrying its own departure-time window.

    The default is load-bearing: with no ``return_time_restrictions`` the
    return segment must keep inheriting the outbound window. If that ever
    drifts, every existing round-trip search silently stops filtering its
    return leg — and because the window is applied client-side, the symptom
    is *more* results, not an error.
    """

    OUT = TimeRestrictions(earliest_departure=6, latest_departure=12)
    RET = TimeRestrictions(earliest_departure=17, latest_departure=23)

    def test_return_inherits_outbound_by_default(self):
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE,
            return_date=RETURN_DATE,
            time_restrictions=self.OUT,
        )
        assert segments[0].time_restrictions == self.OUT
        assert segments[1].time_restrictions == self.OUT

    def test_return_window_overrides_outbound(self):
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE,
            return_date=RETURN_DATE,
            time_restrictions=self.OUT,
            return_time_restrictions=self.RET,
        )
        assert segments[0].time_restrictions.latest_departure == 12
        assert segments[1].time_restrictions.latest_departure == 23
        assert segments[0].time_restrictions is not segments[1].time_restrictions

    def test_return_window_without_outbound_window(self):
        """A return-only window leaves the outbound unrestricted."""
        segments, _ = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE,
            return_date=RETURN_DATE,
            return_time_restrictions=self.RET,
        )
        assert segments[0].time_restrictions is None
        assert segments[1].time_restrictions == self.RET

    def test_return_window_without_a_return_date_raises(self):
        """A one-way search has no return leg to apply the window to.

        Accepting it silently would be the exact failure mode this feature
        exists to remove: a filter the caller believes is active and that
        nothing ever reads.
        """
        with pytest.raises(ParseError, match="round trip"):
            build_flight_segments(
                origin=Airport.JFK,
                destination=Airport.LAX,
                departure_date=DEPART_DATE,
                time_restrictions=self.OUT,
                return_time_restrictions=self.RET,
            )

    def test_one_way_without_a_return_window_is_unaffected(self):
        segments, trip_type = build_flight_segments(
            origin=Airport.JFK,
            destination=Airport.LAX,
            departure_date=DEPART_DATE,
            time_restrictions=self.OUT,
        )
        assert trip_type == TripType.ONE_WAY
        assert len(segments) == 1
        assert segments[0].time_restrictions == self.OUT

    def test_return_window_survives_multi_airport(self):
        """Airport mirroring and the per-leg window must not interfere."""
        segments, _ = build_flight_segments(
            origin=[Airport.JFK, Airport.LGA],
            destination=[Airport.LHR, Airport.CDG],
            departure_date=DEPART_DATE,
            return_date=RETURN_DATE,
            time_restrictions=self.OUT,
            return_time_restrictions=self.RET,
        )
        assert segments[1].departure_airport == [[Airport.LHR, 0], [Airport.CDG, 0]]
        assert segments[1].arrival_airport == [[Airport.JFK, 0], [Airport.LGA, 0]]
        assert segments[0].time_restrictions == self.OUT
        assert segments[1].time_restrictions == self.RET
