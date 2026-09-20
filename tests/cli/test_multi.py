"""Tests for the multi-city CLI command."""

from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.models import Airline, Airport, FlightLeg, FlightResult
from fli.models.google_flights.base import TripType
from tests.cli._output import collapsed, plain


@pytest.fixture
def runner():
    """Return a CliRunner instance."""
    return CliRunner()


def _future_date(days_ahead: int = 30) -> str:
    """Return a future date string in YYYY-MM-DD format."""
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _make_one_way_results():
    """Per-leg results: plain FlightResults, the shape a one-way search returns.

    `multi` no longer issues a single multi-city request — there is no
    transport for one — so each leg is searched independently and the mock
    must return what a one-way search returns, not a tuple per itinerary.
    """
    now = datetime.now()
    return [
        FlightResult(
            price=250.0,
            duration=600,
            stops=0,
            legs=[
                FlightLeg(
                    airline=Airline.DL,
                    flight_number="DL100",
                    departure_airport=Airport.SEA,
                    arrival_airport=Airport.HKG,
                    departure_datetime=now,
                    arrival_datetime=now + timedelta(hours=10),
                    duration=600,
                )
            ],
        )
    ]


def _make_multi_city_results():
    """Create mock multi-city results (3-tuple of FlightResults)."""
    now = datetime.now()
    return [
        (
            FlightResult(
                price=0.0,
                duration=600,
                stops=0,
                legs=[
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL100",
                        departure_airport=Airport.SEA,
                        arrival_airport=Airport.HKG,
                        departure_datetime=now,
                        arrival_datetime=now + timedelta(hours=10),
                        duration=600,
                    )
                ],
            ),
            FlightResult(
                price=0.0,
                duration=300,
                stops=0,
                legs=[
                    FlightLeg(
                        airline=Airline.CX,
                        flight_number="CX200",
                        departure_airport=Airport.HKG,
                        arrival_airport=Airport.PEK,
                        departure_datetime=now + timedelta(days=4),
                        arrival_datetime=now + timedelta(days=4, hours=5),
                        duration=300,
                    )
                ],
            ),
            FlightResult(
                price=2499.99,
                duration=660,
                stops=1,
                legs=[
                    FlightLeg(
                        airline=Airline.CA,
                        flight_number="CA300",
                        departure_airport=Airport.PEK,
                        arrival_airport=Airport.NRT,
                        departure_datetime=now + timedelta(days=7),
                        arrival_datetime=now + timedelta(days=7, hours=4),
                        duration=240,
                    ),
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL400",
                        departure_airport=Airport.NRT,
                        arrival_airport=Airport.SEA,
                        departure_datetime=now + timedelta(days=7, hours=6),
                        arrival_datetime=now + timedelta(days=7, hours=13),
                        duration=420,
                    ),
                ],
            ),
        )
    ]


class TestMultiCityCommand:
    """Tests for the multi command."""

    def test_basic_two_leg_search(self, runner, mock_search_flights, mock_console):
        """Test basic multi-city search with two legs."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            ["multi", "--leg", f"SEA,HKG,{date1}", "--leg", f"HKG,SEA,{date2}"],
        )
        assert result.exit_code == 0
        # One search per leg: multi-city has no single-request transport, so
        # the command researches each leg as a one-way instead of refusing.
        assert mock_search_flights.search.call_count == 2
        assert all(
            c.args[0].trip_type == TripType.ONE_WAY
            for c in mock_search_flights.search.call_args_list
        )

    def test_three_leg_search(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with three legs."""
        mock_search_flights.search.return_value = _make_one_way_results()

        date1 = _future_date(30)
        date2 = _future_date(34)
        date3 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,PEK,{date2}",
                "--leg",
                f"PEK,SEA,{date3}",
            ],
        )
        assert result.exit_code == 0
        assert mock_search_flights.search.call_count == 3
        calls = mock_search_flights.search.call_args_list
        assert all(c.args[0].trip_type == TripType.ONE_WAY for c in calls)
        # Legs are searched in the order given, one segment each.
        routes = [
            (
                c.args[0].flight_segments[0].departure_airport[0][0].name,
                c.args[0].flight_segments[0].arrival_airport[0][0].name,
            )
            for c in calls
        ]
        assert routes == [("SEA", "HKG"), ("HKG", "PEK"), ("PEK", "SEA")]
        # Each leg is its own one-way request, so one segment per call.
        assert all(len(c.args[0].flight_segments) == 1 for c in calls)

    def test_with_passengers(self, runner, mock_search_flights, mock_console):
        """Test multi-city search passes adult passenger count into filters."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "--passengers",
                "2",
            ],
        )
        assert result.exit_code == 0
        args, _ = mock_search_flights.search.call_args
        assert args[0].passenger_info.adults == 2

    def test_with_children_and_infants(self, runner, mock_search_flights, mock_console):
        """Child and infant counts reach PassengerInfo on multi-city filters."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "--passengers",
                "2",
                "--children",
                "1",
                "--infants-in-seat",
                "1",
                "--infants-on-lap",
                "2",
            ],
        )
        assert result.exit_code == 0
        args, _ = mock_search_flights.search.call_args
        passenger_info = args[0].passenger_info
        assert passenger_info.adults == 2
        assert passenger_info.children == 1
        assert passenger_info.infants_in_seat == 1
        assert passenger_info.infants_on_lap == 2

    def test_with_cabin_class(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with cabin class filter."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "--class",
                "BUSINESS",
            ],
        )
        assert result.exit_code == 0

    def test_with_stops_filter(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with stops filter."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "--stops",
                "NON_STOP",
            ],
        )
        assert result.exit_code == 0

    def test_with_airlines_filter(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with airline filter."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "-a",
                "DL",
                "-a",
                "CX",
            ],
        )
        assert result.exit_code == 0

    def test_with_time_filter(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with departure time window."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{date1}",
                "--leg",
                f"HKG,SEA,{date2}",
                "--time",
                "6-20",
            ],
        )
        assert result.exit_code == 0

    def test_short_flag(self, runner, mock_search_flights, mock_console):
        """Test multi-city search using -l short flag."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            ["multi", "-l", f"SEA,HKG,{date1}", "-l", f"HKG,SEA,{date2}"],
        )
        assert result.exit_code == 0


class TestMultiCityValidation:
    """Tests for multi-city command validation and error handling."""

    def test_single_leg_rejected(self, runner, mock_search_flights, mock_console):
        """Test that a single leg is rejected."""
        date1 = _future_date(30)

        result = runner.invoke(
            app,
            ["multi", "--leg", f"SEA,HKG,{date1}"],
        )
        assert result.exit_code == 1
        assert "at least 2 legs" in result.stdout

    def test_invalid_leg_format(self, runner, mock_search_flights, mock_console):
        """Test that invalid leg format is rejected."""
        result = runner.invoke(
            app,
            ["multi", "--leg", "SEA-HKG-2026-12-26", "--leg", "HKG-SEA-2027-01-02"],
        )
        assert result.exit_code != 0

    def test_invalid_airport_code(self, runner, mock_search_flights, mock_console):
        """Test that invalid airport codes are rejected."""
        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            ["multi", "--leg", f"XXX,HKG,{date1}", "--leg", f"HKG,SEA,{date2}"],
        )
        assert result.exit_code == 1
        assert "Error" in result.stdout

    def test_invalid_date(self, runner, mock_search_flights, mock_console):
        """Test that invalid dates are rejected."""
        result = runner.invoke(
            app,
            ["multi", "--leg", "SEA,HKG,2026-13-45", "--leg", "HKG,SEA,2027-01-02"],
        )
        assert result.exit_code != 0

    def test_no_results(self, runner, mock_search_flights, mock_console):
        """Test multi-city search with no results."""
        mock_search_flights.search.return_value = []

        date1 = _future_date(30)
        date2 = _future_date(37)

        result = runner.invoke(
            app,
            ["multi", "--leg", f"SEA,HKG,{date1}", "--leg", f"HKG,SEA,{date2}"],
        )
        assert result.exit_code == 1
        assert "No flights found" in result.stdout


def test_multi_leg_rejects_multi_airport_origin(runner, mock_search_flights, mock_console):
    """`multi` legs use the comma as a field separator, so multi-airport legs are invalid.

    Multi-airport origins/destinations are supported by `flights` and `dates`,
    but a `--leg` value is positionally parsed as ORIGIN,DEST,DATE. Accepting
    `SEA,BFI,HKG,DATE` would be ambiguous with a four-field leg, so it is
    deliberately rejected. This test pins that grammar so the behaviour is not
    "fixed" by accident.
    """
    result = runner.invoke(
        app,
        [
            "multi",
            "--leg",
            f"SEA,BFI,HKG,{_future_date(30)}",
            "--leg",
            f"PEK,SEA,{_future_date(60)}",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid leg format" in result.stdout + result.stderr


def test_multi_leg_accepts_icao_codes(runner, mock_search_flights, mock_console):
    """ICAO codes work in `--leg` too, mixed freely with IATA codes.

    The leg grammar validates code *shape* before ``resolve_airport`` runs, so
    a 3-letter-only pattern would reject `KJFK` with a confusing "Invalid leg
    format" error rather than resolving it.
    """
    result = runner.invoke(
        app,
        [
            "multi",
            "--leg",
            f"KSEA,VHHH,{_future_date(30)}",
            "--leg",
            f"VHHH,SEA,{_future_date(37)}",
        ],
    )
    assert result.exit_code == 0
    calls = mock_search_flights.search.call_args_list
    assert len(calls) == 2, "one one-way search per leg"
    first = calls[0].args[0].flight_segments[0]
    second = calls[1].args[0].flight_segments[0]
    assert [apt for apt, _ in first.departure_airport] == [Airport.SEA]
    assert [apt for apt, _ in first.arrival_airport] == [Airport.HKG]
    assert [apt for apt, _ in second.departure_airport] == [Airport.HKG]
    assert [apt for apt, _ in second.arrival_airport] == [Airport.SEA]


def test_multi_leg_unmapped_icao_reports_an_airport_error(
    runner, mock_search_flights, mock_console
):
    """An unmapped 4-letter code is an airport error, not a leg-format error."""
    result = runner.invoke(
        app,
        [
            "multi",
            "--leg",
            f"ZZZZ,HKG,{_future_date(30)}",
            "--leg",
            f"HKG,SEA,{_future_date(37)}",
        ],
    )
    assert result.exit_code == 1
    assert "Invalid airport code: 'ZZZZ'" in result.stdout
    assert "ICAO" in result.stdout
    assert "Invalid leg format" not in result.stdout


def test_multi_leg_still_rejects_five_letter_codes(runner, mock_search_flights, mock_console):
    """Widening the code pattern to 3-or-4 letters must not open it further."""
    result = runner.invoke(
        app,
        [
            "multi",
            "--leg",
            f"KSEAX,HKG,{_future_date(30)}",
            "--leg",
            f"HKG,SEA,{_future_date(37)}",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid leg format" in result.stdout


class TestMultiCityResearchOutput:
    """`multi` researches legs and links the real fare, rather than refusing.

    Multi-city has no single-request transport — Google serves those results
    over the RPC gated since 2026-08, so the search page carries no rows. The
    command previously reported that as an error. It now searches each leg as
    a one-way, which does work, and prints the URL that prices the itinerary
    as one ticket.
    """

    def test_prints_a_multi_city_url(self, runner, mock_search_flights, mock_console):
        mock_search_flights.search.return_value = _make_one_way_results()
        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{_future_date(30)}",
                "--leg",
                f"HKG,SEA,{_future_date(37)}",
            ],
        )
        assert result.exit_code == 0
        out = plain(result.stdout)
        assert "google.com/travel/flights?tfs=" in out

    def test_labels_the_sum_as_separate_fares(self, runner, mock_search_flights, mock_console):
        """The per-leg total must not be presented as a multi-city price."""
        mock_search_flights.search.return_value = _make_one_way_results()
        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{_future_date(30)}",
                "--leg",
                f"HKG,SEA,{_future_date(37)}",
            ],
        )
        out = collapsed(result.stdout)
        assert "booked separately" in out
        assert "sum of independent one-way fares" in out

    def test_one_unparseable_leg_does_not_abort_the_command(
        self, runner, mock_search_flights, mock_console
    ):
        """SearchParseError descends from Exception, not SearchClientError.

        The per-leg loop caught only SearchClientError, so a leg Google
        served no board for took the whole command down and discarded the
        legs that had searched cleanly. A route with no service raises
        exactly this, which makes it routine in a multi-city itinerary
        rather than exotic.
        """
        from fli.search.exceptions import SearchParseError

        mock_search_flights.search.side_effect = [
            _make_one_way_results(),
            SearchParseError("Shopping response shape changed"),
        ]
        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{_future_date(30)}",
                "--leg",
                f"HKG,SEA,{_future_date(37)}",
            ],
        )
        out = collapsed(result.stdout)
        assert result.exit_code == 0, "one bad leg must not fail the command"
        assert "leg search failed" in out
        assert "Unexpected error" not in out
        assert "google.com/travel/flights?tfs=" in out

    def test_a_malformed_leg_writes_no_traceback_file(
        self, runner, mock_search_flights, mock_console
    ):
        """A typo is a usage mistake, not a fault to investigate."""
        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"KSEAX,HKG,{_future_date(30)}",
                "--leg",
                f"HKG,SEA,{_future_date(37)}",
            ],
        )
        out = collapsed(result.stdout)
        assert result.exit_code == 1
        assert "Invalid leg format" in out
        assert "traceback" not in out.lower()

    def test_exits_nonzero_when_no_leg_has_flights(self, runner, mock_search_flights, mock_console):
        mock_search_flights.search.return_value = []
        result = runner.invoke(
            app,
            [
                "multi",
                "--leg",
                f"SEA,HKG,{_future_date(30)}",
                "--leg",
                f"HKG,SEA,{_future_date(37)}",
            ],
        )
        assert result.exit_code == 1
        assert "No flights found" in result.stdout
