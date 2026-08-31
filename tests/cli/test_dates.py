"""Tests for the dates CLI command."""

import json
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.models import Airline, Airport
from fli.models.google_flights.base import TripType
from fli.search import DatePrice


@pytest.fixture
def runner():
    """Return a CliRunner instance."""
    return CliRunner()


def test_basic_dates_search(runner, mock_search_dates, mock_console):
    """Test basic dates search (one-way by default)."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(app, ["dates", "JFK", "LAX"])
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()
    args, _ = mock_search_dates.search.call_args
    assert args[0].trip_type == TripType.ONE_WAY


def test_dates_with_passengers(runner, mock_search_dates, mock_console):
    """Test dates search passes adult passenger count into filters."""
    mock_search_dates.search.return_value = []
    result = runner.invoke(app, ["dates", "JFK", "LAX", "--passengers", "2", "--format", "json"])
    assert result.exit_code == 0
    args, _ = mock_search_dates.search.call_args
    assert args[0].passenger_info.adults == 2
    payload = json.loads(result.stdout)
    assert payload["query"]["passengers"] == 2


def test_dates_with_date_range(runner, mock_search_dates, mock_console):
    """Test dates search with custom date range."""
    from_date = datetime.now().strftime("%Y-%m-%d")
    to_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--from", from_date, "--to", to_date],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_with_days(runner, mock_search_dates, mock_console):
    """Test dates search with specific days."""
    today = datetime.now()
    days_until_monday = (7 - today.weekday()) % 7
    next_monday = today + timedelta(days=days_until_monday)

    mock_search_dates.search.return_value = [
        DatePrice(
            date=(next_monday,),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--monday", "--friday"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_with_airlines(runner, mock_search_dates, mock_console):
    """Repeated -a flags resolve to the matching Airline enums on the filter."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "-a", "DL", "-a", "UA"],
    )
    assert result.exit_code == 0
    args, _ = mock_search_dates.search.call_args
    assert args[0].airlines == [Airline.DL, Airline.UA]


def test_dates_with_comma_separated_airlines(runner, mock_search_dates, mock_console):
    """Single -a flag with comma-joined codes splits into multiple airlines."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "-a", "DL,UA"],
    )
    assert result.exit_code == 0
    args, _ = mock_search_dates.search.call_args
    assert args[0].airlines == [Airline.DL, Airline.UA]


def test_dates_with_cabin_class(runner, mock_search_dates, mock_console):
    """Test dates search with cabin class."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--class", "BUSINESS"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_with_stops(runner, mock_search_dates, mock_console):
    """Test dates search with stops filter."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--stops", "NON_STOP"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_with_time(runner, mock_search_dates, mock_console):
    """Test dates search with time filter."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--time", "6-20"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_with_sort(runner, mock_search_dates, mock_console):
    """Test dates search with sort option."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1),),
            price=299.99,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--sort"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()


def test_dates_invalid_airport(runner, mock_search_dates, mock_console):
    """Test dates search with invalid airport code."""
    result = runner.invoke(app, ["dates", "XXX", "LAX"])
    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_dates_invalid_date_range(runner, mock_search_dates, mock_console):
    """Test dates search with invalid date range."""
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--from", "2024-01-01", "--to", "2023-12-31"],
    )
    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_dates_no_results(runner, mock_search_dates, mock_console):
    """Test dates search with no results."""
    mock_search_dates.search.return_value = []

    result = runner.invoke(app, ["dates", "JFK", "LAX"])
    assert result.exit_code == 1
    assert "No flights found" in result.stdout


def test_dates_round_trip(runner, mock_search_dates, mock_console):
    """Test dates search with round-trip flag."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(
                datetime.now() + timedelta(days=1),
                datetime.now() + timedelta(days=8),
            ),
            price=599.98,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--round"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()
    args, _ = mock_search_dates.search.call_args
    assert args[0].trip_type == TripType.ROUND_TRIP


def test_dates_round_trip_with_duration(runner, mock_search_dates, mock_console):
    """Test dates round-trip search with custom duration."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(
                datetime.now() + timedelta(days=1),
                datetime.now() + timedelta(days=15),
            ),
            price=599.98,
        ),
    ]
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--round", "-d", "14"],
    )
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()
    args, _ = mock_search_dates.search.call_args
    assert args[0].trip_type == TripType.ROUND_TRIP
    assert args[0].duration == 14


def test_dates_json_output(runner, mock_search_dates, mock_console):
    """Test dates search JSON output."""
    departure_date = datetime.now() + timedelta(days=1)
    return_date = departure_date + timedelta(days=7)
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(departure_date, return_date),
            price=599.98,
        )
    ]

    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--round", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["search_type"] == "dates"
    assert payload["trip_type"] == "ROUND_TRIP"
    assert payload["count"] == 1
    assert payload["query"]["is_round_trip"] is True
    assert payload["dates"][0]["departure_date"] == departure_date.date().isoformat()
    assert payload["dates"][0]["return_date"] == return_date.date().isoformat()
    assert payload["dates"][0]["price"] == 599.98
    assert payload["dates"][0]["currency"] == "USD"


def test_dates_json_invalid_date(runner, mock_search_dates, mock_console):
    """Test dates JSON output for invalid date input."""
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--from", "2024-13-45", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["search_type"] == "dates"
    assert payload["error"]["type"] == "validation_error"
    assert payload["error"]["message"] == "Date must be in YYYY-MM-DD format"


def test_dates_json_error_query_echoes_passengers(runner, mock_search_dates, mock_console):
    """Error-path JSON keeps the same query shape as the success path."""
    result = runner.invoke(
        app,
        [
            "dates",
            "JFK",
            "LAX",
            "--from",
            "2024-13-45",
            "--passengers",
            "2",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["query"]["passengers"] == 2


def test_dates_json_empty_results(runner, mock_search_dates, mock_console):
    """Test dates JSON output when no results are found."""
    mock_search_dates.search.return_value = []

    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["count"] == 0
    assert payload["dates"] == []


def test_dates_with_children_and_infants(runner, mock_search_dates, mock_console):
    """Child and infant counts reach PassengerInfo on the date filters."""
    mock_search_dates.search.return_value = []
    result = runner.invoke(
        app,
        [
            "dates",
            "JFK",
            "LAX",
            "--passengers",
            "2",
            "--children",
            "1",
            "--infants-in-seat",
            "1",
            "--infants-on-lap",
            "2",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    args, _ = mock_search_dates.search.call_args
    passenger_info = args[0].passenger_info
    assert passenger_info.adults == 2
    assert passenger_info.children == 1
    assert passenger_info.infants_in_seat == 1
    assert passenger_info.infants_on_lap == 2
    query = json.loads(result.stdout)["query"]
    assert query["children"] == 1
    assert query["infants_in_seat"] == 1
    assert query["infants_on_lap"] == 2


def _one_date_result():
    """Return a single DatePrice so text-mode output is non-empty."""
    return [DatePrice(date=(datetime.now() + timedelta(days=1),), price=299.99)]


def test_dates_comma_separated_origin(runner, mock_search_dates, mock_console):
    """Comma-separated origins resolve to a multi-airport departure list."""
    mock_search_dates.search.return_value = _one_date_result()
    result = runner.invoke(app, ["dates", "JFK,LGA", "LAX"])
    assert result.exit_code == 0
    filters = mock_search_dates.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.departure_airport] == [Airport.JFK, Airport.LGA]


def test_dates_comma_separated_destination(runner, mock_search_dates, mock_console):
    """Comma-separated destinations resolve to a multi-airport arrival list."""
    mock_search_dates.search.return_value = _one_date_result()
    result = runner.invoke(app, ["dates", "JFK", "LAX,SFO"])
    assert result.exit_code == 0
    filters = mock_search_dates.search.call_args[0][0]
    segment = filters.flight_segments[0]
    assert [apt for apt, _ in segment.arrival_airport] == [Airport.LAX, Airport.SFO]


def test_dates_multi_airport_round_trip(runner, mock_search_dates, mock_console):
    """The return segment mirrors the full multi-airport lists for round trips."""
    mock_search_dates.search.return_value = [
        DatePrice(
            date=(datetime.now() + timedelta(days=1), datetime.now() + timedelta(days=4)),
            price=499.99,
        )
    ]
    result = runner.invoke(app, ["dates", "JFK,LGA", "LAX,SFO", "-R"])
    assert result.exit_code == 0
    filters = mock_search_dates.search.call_args[0][0]
    outbound, inbound = filters.flight_segments
    assert [apt for apt, _ in outbound.departure_airport] == [Airport.JFK, Airport.LGA]
    assert [apt for apt, _ in outbound.arrival_airport] == [Airport.LAX, Airport.SFO]
    assert [apt for apt, _ in inbound.departure_airport] == [Airport.LAX, Airport.SFO]
    assert [apt for apt, _ in inbound.arrival_airport] == [Airport.JFK, Airport.LGA]


def test_dates_multi_airport_json_query(runner, mock_search_dates, mock_console):
    """JSON echoes canonical codes; per-date links use the first airport only."""
    mock_search_dates.search.return_value = _one_date_result()
    result = runner.invoke(app, ["dates", "jfk,lga", "LAX", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["origin"] == "JFK,LGA"
    assert payload["query"]["destination"] == "LAX"
    assert payload["dates"]
    for entry in payload["dates"]:
        assert "JFK" in entry["booking_url"]
        assert "LGA" not in entry["booking_url"]


def test_dates_blank_origin_reports_parse_error(runner, mock_search_dates, mock_console):
    """A comma-only origin fails cleanly rather than crashing."""
    mock_search_dates.search.return_value = _one_date_result()
    result = runner.invoke(app, ["dates", ",", "LAX"])
    assert result.exit_code == 1
    assert "No valid origin airport codes" in result.stdout
    assert "Traceback" not in result.stdout


def _sweep_results():
    """Return one DatePrice per trip length, all departing the same day."""
    departure = datetime.now() + timedelta(days=1)
    return [
        DatePrice(date=(departure, departure + timedelta(days=nights)), price=price)
        for nights, price in ((4, 499.0), (5, 399.0), (6, 599.0))
    ]


def test_dates_duration_sweep_calls_search_durations(runner, mock_search_dates, mock_console):
    """A min/max range fans out over every duration in the range."""
    mock_search_dates.search_durations.return_value = _sweep_results()
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--round", "--min-duration", "4", "--max-duration", "6"],
    )
    assert result.exit_code == 0
    mock_search_dates.search_durations.assert_called_once()
    args, _ = mock_search_dates.search_durations.call_args
    assert args[1] == [4, 5, 6]
    mock_search_dates.search.assert_not_called()


def test_dates_without_sweep_flags_uses_plain_search(runner, mock_search_dates, mock_console):
    """No sweep flags means the existing single-duration path, untouched."""
    mock_search_dates.search.return_value = _sweep_results()[:1]
    result = runner.invoke(app, ["dates", "JFK", "LAX", "--round"])
    assert result.exit_code == 0
    mock_search_dates.search.assert_called_once()
    mock_search_dates.search_durations.assert_not_called()


def test_dates_round_trip_default_duration_is_still_three(runner, mock_search_dates, mock_console):
    """The sentinel default must not change the effective trip length."""
    mock_search_dates.search.return_value = _sweep_results()[:1]
    result = runner.invoke(app, ["dates", "JFK", "LAX", "--round"])
    assert result.exit_code == 0
    filters = mock_search_dates.search.call_args[0][0]
    assert filters.duration == 3


def test_dates_sweep_requires_round_trip(runner, mock_search_dates, mock_console):
    """A one-way search has no trip duration to sweep."""
    result = runner.invoke(
        app, ["dates", "JFK", "LAX", "--min-duration", "4", "--max-duration", "6"]
    )
    assert result.exit_code == 1
    assert "round" in result.stdout


def test_dates_sweep_rejects_explicit_duration(runner, mock_search_dates, mock_console):
    """``--duration`` and the sweep flags mean two different things."""
    result = runner.invoke(
        app,
        [
            "dates",
            "JFK",
            "LAX",
            "--round",
            "--duration",
            "5",
            "--min-duration",
            "4",
            "--max-duration",
            "6",
        ],
    )
    assert result.exit_code == 1
    assert "Cannot combine" in result.stdout


def test_dates_sweep_rejects_inverted_range(runner, mock_search_dates, mock_console):
    result = runner.invoke(
        app,
        ["dates", "JFK", "LAX", "--round", "--min-duration", "7", "--max-duration", "3"],
    )
    assert result.exit_code == 1


def test_dates_sweep_requires_both_bounds(runner, mock_search_dates, mock_console):
    """An open-ended sweep would issue thousands of page fetches."""
    result = runner.invoke(app, ["dates", "JFK", "LAX", "--round", "--min-duration", "4"])
    assert result.exit_code == 1
    assert "together" in result.stdout


def test_dates_sweep_json_echoes_the_range(runner, mock_search_dates, mock_console):
    """JSON output reports the sweep bounds and a null fixed duration."""
    mock_search_dates.search_durations.return_value = _sweep_results()
    result = runner.invoke(
        app,
        [
            "dates",
            "JFK",
            "LAX",
            "--round",
            "--min-duration",
            "4",
            "--max-duration",
            "6",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["min_duration"] == 4
    assert payload["query"]["max_duration"] == 6
    assert payload["query"]["trip_duration"] is None
    returns = [entry["return_date"] for entry in payload["dates"]]
    assert len(set(returns)) == 3


def test_dates_sweep_json_error_payload_carries_the_range(runner, mock_search_dates, mock_console):
    """Both error-path query dicts must echo the new flags too."""
    result = runner.invoke(
        app,
        [
            "dates",
            "NOPE",
            "LAX",
            "--round",
            "--min-duration",
            "4",
            "--max-duration",
            "6",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["query"]["min_duration"] == 4
    assert payload["query"]["max_duration"] == 6
