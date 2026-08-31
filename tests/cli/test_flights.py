"""Tests for the flights CLI command."""

import json
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.models import Airline, Airport, FlightLeg, FlightResult
from fli.models.google_flights.base import TripType


@pytest.fixture
def runner():
    """Return a CliRunner instance."""
    return CliRunner()


def test_basic_flights_search(runner, mock_search_flights, mock_console):
    """Test basic flight search with required parameters."""
    result = runner.invoke(app, ["flights", "JFK", "LAX", datetime.now().strftime("%Y-%m-%d")])
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()


def test_flights_with_time_filter(runner, mock_search_flights, mock_console):
    """Test flights search with time filter."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--time",
            "6-20",
        ],
    )
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()


def test_flights_with_passengers(runner, mock_search_flights, mock_console):
    """Test flights search passes adult passenger count into filters."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--passengers",
            "2",
        ],
    )
    assert result.exit_code == 0
    args, _ = mock_search_flights.search.call_args
    assert args[0].passenger_info.adults == 2


def test_flights_json_query_echoes_passengers(runner, mock_search_flights, mock_console):
    """JSON query echo includes requested adult passenger count."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--passengers",
            "3",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["passengers"] == 3


def test_flights_with_airlines(runner, mock_search_flights, mock_console):
    """Repeated -a flags resolve to the matching Airline enums on the filter."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "-a",
            "DL",
            "-a",
            "UA",
        ],
    )
    assert result.exit_code == 0
    args, _ = mock_search_flights.search.call_args
    assert args[0].airlines == [Airline.DL, Airline.UA]


def test_flights_with_comma_separated_airlines(runner, mock_search_flights, mock_console):
    """Single -a flag with comma-joined codes splits into multiple airlines."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "-a",
            "DL,UA",
        ],
    )
    assert result.exit_code == 0
    args, _ = mock_search_flights.search.call_args
    assert args[0].airlines == [Airline.DL, Airline.UA]


def test_flights_json_query_echoes_split_airlines(runner, mock_search_flights, mock_console):
    """JSON query echo reflects the parsed, split airline list — not the raw input."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "-a",
            "DL,UA",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["airlines"] == ["DL", "UA"]


def test_flights_with_cabin_class(runner, mock_search_flights, mock_console):
    """Test flights search with cabin class."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--class",
            "BUSINESS",
        ],
    )
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()


def test_flights_with_stops(runner, mock_search_flights, mock_console):
    """Test flights search with stops filter."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--stops",
            "NON_STOP",
        ],
    )
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()


def test_flights_invalid_airport(runner, mock_search_flights, mock_console):
    """Test flights search with invalid airport code."""
    result = runner.invoke(
        app,
        ["flights", "XXX", "LAX", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_flights_invalid_date(runner, mock_search_flights, mock_console):
    """Test flights search with invalid date format."""
    result = runner.invoke(app, ["flights", "JFK", "LAX", "2024-13-45"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_flights_no_results(runner, mock_search_flights, mock_console):
    """Test flights search with no results."""
    mock_search_flights.search.return_value = []

    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 1
    assert "No flights found" in result.stdout


def test_basic_round_trip_flights(runner, mock_search_flights, mock_console):
    """Test basic round-trip flight search."""
    outbound_date = datetime.now().strftime("%Y-%m-%d")
    return_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            outbound_date,
            "--return",
            return_date,
        ],
    )
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()


def test_round_trip_with_filters(runner, mock_search_flights, mock_console):
    """Test round-trip flights search with additional filters."""
    outbound_date = datetime.now().strftime("%Y-%m-%d")
    return_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            outbound_date,
            "--return",
            return_date,
            "--class",
            "BUSINESS",
            "--stops",
            "NON_STOP",
            "-a",
            "DL",
        ],
    )
    assert result.exit_code == 0
    mock_search_flights.search.assert_called_once()
    args, kwargs = mock_search_flights.search.call_args
    assert args[0].trip_type == TripType.ROUND_TRIP


def test_round_trip_invalid_dates(runner, mock_search_flights, mock_console):
    """Test round-trip flights search with return date before outbound date."""
    outbound_date = datetime.now().strftime("%Y-%m-%d")
    return_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            outbound_date,
            "--return",
            return_date,
        ],
    )
    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_flights_json_output(runner, mock_search_flights, mock_console):
    """Test flights search with JSON output."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["data_source"] == "google_flights"
    assert payload["search_type"] == "flights"
    assert payload["trip_type"] == "ONE_WAY"
    assert payload["count"] == 2
    assert payload["query"]["origin"] == "JFK"
    assert payload["query"]["destination"] == "LAX"
    assert payload["flights"][0]["price"] == 299.99
    assert payload["flights"][0]["currency"] == "USD"
    assert payload["flights"][0]["legs"][0]["departure_airport"]["code"] == "JFK"
    assert payload["flights"][0]["legs"][0]["arrival_airport"]["code"] == "LAX"


def test_flights_json_round_trip_output(runner, mock_search_flights, mock_console):
    """Test round-trip flights JSON output preserves outbound and return sections."""
    now = datetime.now()
    mock_search_flights.search.return_value = [
        (
            FlightResult(
                price=599.98,
                duration=180,
                stops=0,
                legs=[
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL123",
                        departure_airport=Airport.JFK,
                        arrival_airport=Airport.LAX,
                        departure_datetime=now,
                        arrival_datetime=now + timedelta(hours=3),
                        duration=180,
                    )
                ],
            ),
            FlightResult(
                price=599.98,
                duration=200,
                stops=1,
                legs=[
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL456",
                        departure_airport=Airport.LAX,
                        arrival_airport=Airport.JFK,
                        departure_datetime=now + timedelta(days=7),
                        arrival_datetime=now + timedelta(days=7, hours=4),
                        duration=200,
                    )
                ],
            ),
        )
    ]

    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            now.strftime("%Y-%m-%d"),
            "--return",
            (now + timedelta(days=7)).strftime("%Y-%m-%d"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["trip_type"] == "ROUND_TRIP"
    assert payload["count"] == 1
    assert payload["flights"][0]["price"] == 599.98
    assert payload["flights"][0]["duration"] == 380
    assert payload["flights"][0]["stops"] == 1
    assert payload["flights"][0]["outbound"]["legs"][0]["flight_number"] == "DL123"
    assert payload["flights"][0]["return"]["legs"][0]["flight_number"] == "DL456"


def test_flights_json_invalid_date(runner, mock_search_flights, mock_console):
    """Test flights JSON output for invalid dates."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", "2024-13-45", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["search_type"] == "flights"
    assert payload["error"]["type"] == "validation_error"
    assert "YYYY-MM-DD" in payload["error"]["message"]


def test_flights_json_no_results(runner, mock_search_flights, mock_console):
    """Test flights JSON output when no results are found."""
    mock_search_flights.search.return_value = []

    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["count"] == 0
    assert payload["flights"] == []


def test_flights_with_children_and_infants(runner, mock_search_flights, mock_console):
    """Child and infant counts reach PassengerInfo on the flight filters."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
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


def test_flights_json_query_echoes_child_and_infant_counts(
    runner, mock_search_flights, mock_console
):
    """JSON query echo reports child and infant counts alongside adults."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--children",
            "2",
            "--infants-in-seat",
            "1",
            "--infants-on-lap",
            "1",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    query = json.loads(result.stdout)["query"]
    assert query["children"] == 2
    assert query["infants_in_seat"] == 1
    assert query["infants_on_lap"] == 1


def test_flights_rejects_negative_children(runner, mock_search_flights, mock_console):
    """Child counts below zero are rejected by the CLI."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            datetime.now().strftime("%Y-%m-%d"),
            "--children=-1",
        ],
    )
    assert result.exit_code != 0
    assert "x>=0" in result.output


def test_flights_comma_separated_origin(runner, mock_search_flights, mock_console):
    """Comma-separated origins resolve to a multi-airport departure list."""
    result = runner.invoke(
        app,
        ["flights", "JFK,LGA", "LAX", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 0
    filters = mock_search_flights.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.departure_airport] == [Airport.JFK, Airport.LGA]


def test_flights_comma_separated_destination(runner, mock_search_flights, mock_console):
    """Comma-separated destinations resolve to a multi-airport arrival list."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX,SFO", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 0
    filters = mock_search_flights.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.arrival_airport] == [Airport.LAX, Airport.SFO]


def test_flights_multi_airport_whitespace_and_case(runner, mock_search_flights, mock_console):
    """Whitespace around comma-separated codes is tolerated and case ignored."""
    result = runner.invoke(
        app,
        ["flights", " jfk , lga ", "LAX", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 0
    filters = mock_search_flights.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.departure_airport] == [Airport.JFK, Airport.LGA]


def test_flights_multi_airport_round_trip_reverses_lists(runner, mock_search_flights, mock_console):
    """The return segment mirrors the full multi-airport origin/destination lists."""
    depart = datetime.now().strftime("%Y-%m-%d")
    ret = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    result = runner.invoke(app, ["flights", "JFK,LGA", "LAX,SFO", depart, "--return", ret])
    assert result.exit_code == 0
    filters = mock_search_flights.search.call_args[0][0]
    outbound, inbound = filters.flight_segments
    assert [apt for apt, _ in outbound.departure_airport] == [Airport.JFK, Airport.LGA]
    assert [apt for apt, _ in outbound.arrival_airport] == [Airport.LAX, Airport.SFO]
    assert [apt for apt, _ in inbound.departure_airport] == [Airport.LAX, Airport.SFO]
    assert [apt for apt, _ in inbound.arrival_airport] == [Airport.JFK, Airport.LGA]


def test_flights_multi_airport_json_query_and_booking_url(
    runner, mock_search_flights, mock_console
):
    """JSON echoes canonical codes; the shareable link uses the first airport only."""
    result = runner.invoke(
        app,
        ["flights", "jfk,lga", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["origin"] == "JFK,LGA"
    assert payload["query"]["destination"] == "LAX"
    # Deep links can only encode one route: first origin/destination wins.
    assert "JFK" in payload["booking_url"]
    assert "LGA" not in payload["booking_url"]


def test_flights_blank_origin_reports_parse_error(runner, mock_search_flights, mock_console):
    """A comma-only origin is user error, not a crash: clean ParseError, no crash log."""
    result = runner.invoke(
        app,
        ["flights", ",", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["type"] == "validation_error"
    assert "No valid origin airport codes" in payload["error"]["message"]
    assert "log_path" not in payload["error"]


def test_flights_partial_invalid_airport_list(runner, mock_search_flights, mock_console):
    """One bad code in a list fails with a message naming the offending token."""
    result = runner.invoke(
        app,
        ["flights", "JFK,XXX", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["type"] == "validation_error"
    # The message names only the offending token, not the whole raw string.
    assert "'XXX'" in payload["error"]["message"]
    assert "JFK" not in payload["error"]["message"]


def test_flights_accepts_icao_codes(runner, mock_search_flights, mock_console):
    """Four-letter ICAO codes reach the search layer as the right airports."""
    result = runner.invoke(
        app,
        ["flights", "KJFK", "KLAX", datetime.now().strftime("%Y-%m-%d")],
    )
    assert result.exit_code == 0
    filters = mock_search_flights.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.departure_airport] == [Airport.JFK]
    assert [apt for apt, _ in segment.arrival_airport] == [Airport.LAX]


def test_flights_rejects_unknown_icao_with_a_labelled_error(
    runner, mock_search_flights, mock_console
):
    """An unmapped four-letter code fails naming the slot, not just the code."""
    result = runner.invoke(
        app,
        ["flights", "ZZZZ", "LAX", datetime.now().strftime("%Y-%m-%d"), "--format", "json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["error"]["type"] == "validation_error"
    message = payload["error"]["message"]
    # Keeps the "Invalid <slot> airport code" prefix that MCP clients and the
    # CLI already string-match on, while explaining the four-letter dispatch.
    assert "Invalid origin airport code: 'ZZZZ'" in message
    assert "ICAO" in message


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _next_week() -> str:
    return (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")


def test_return_time_separate_from_outbound(runner, mock_search_flights, mock_console):
    """--return-time gives the return leg its own departure window."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            _today(),
            "--return",
            _next_week(),
            "--time",
            "6-14",
            "--return-time",
            "10-22",
        ],
    )
    assert result.exit_code == 0
    segments = mock_search_flights.search.call_args[0][0].flight_segments
    assert segments[0].time_restrictions.earliest_departure == 6
    assert segments[0].time_restrictions.latest_departure == 14
    assert segments[1].time_restrictions.earliest_departure == 10
    assert segments[1].time_restrictions.latest_departure == 22


def test_return_time_defaults_to_outbound(runner, mock_search_flights, mock_console):
    """Without --return-time, the return leg keeps inheriting --time.

    Regression guard: if this ever stops holding, round-trip searches quietly
    stop filtering their return leg and simply return more results.
    """
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", _today(), "--return", _next_week(), "--time", "6-14"],
    )
    assert result.exit_code == 0
    segments = mock_search_flights.search.call_args[0][0].flight_segments
    assert segments[1].time_restrictions.earliest_departure == 6
    assert segments[1].time_restrictions.latest_departure == 14


def test_return_time_without_return_date_errors(runner, mock_search_flights, mock_console):
    """--return-time needs a --return date; it must not be silently inert."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", _today(), "--return-time", "10-22"],
    )
    assert result.exit_code == 1
    assert "return date" in result.stdout
    mock_search_flights.search.assert_not_called()


def test_return_time_short_flag(runner, mock_search_flights, mock_console):
    """-T is the short form, mirroring -t for the outbound window."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", _today(), "--return", _next_week(), "-t", "6-14", "-T", "10-22"],
    )
    assert result.exit_code == 0
    segments = mock_search_flights.search.call_args[0][0].flight_segments
    assert segments[1].time_restrictions.latest_departure == 22


def test_return_time_in_json_output(runner, mock_search_flights, mock_console):
    """The echoed query reports the return window alongside the outbound one."""
    result = runner.invoke(
        app,
        [
            "flights",
            "JFK",
            "LAX",
            _today(),
            "--return",
            _next_week(),
            "--time",
            "6-14",
            "--return-time",
            "10-22",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["departure_window"] == "6-14"
    assert payload["query"]["return_departure_window"] == "10-22"


def test_return_time_bad_format_errors(runner, mock_search_flights, mock_console):
    """A malformed return window goes through the shared time-range parser."""
    result = runner.invoke(
        app,
        ["flights", "JFK", "LAX", _today(), "--return", _next_week(), "--return-time", "6:22"],
    )
    assert result.exit_code == 1
    assert "start-end" in result.stdout
