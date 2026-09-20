from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from fli.models import (
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
)


@pytest.fixture(autouse=True)
def _tmp_cli_log_dir(monkeypatch, tmp_path):
    """Keep CLI error logs out of the real ``~/.fli/logs``.

    ``fli.cli.errors`` writes a traceback file next to the user's home on
    every reported error, so invoking the CLI under test littered the
    developer's own ``~/.fli/logs`` — and made these tests depend on
    whether ``$HOME`` happens to be writable. That coupling is why two
    ``test_multi.py`` cases passed in CI but failed under a sandbox with a
    ``PermissionError`` about ``~/.fli`` rather than the assertion being
    tested. Redirecting the directory makes the suite answer the same way
    everywhere.
    """
    monkeypatch.setattr("fli.cli.errors._LOG_DIR", tmp_path / "fli-logs")


@pytest.fixture
def mock_search_flights(monkeypatch):
    """Mock the SearchFlights class."""
    mock = MagicMock()
    mock.search.return_value = [
        FlightResult(
            price=299.99,
            duration=180,
            stops=0,
            legs=[
                FlightLeg(
                    airline=Airline.DL,
                    flight_number="DL123",
                    departure_airport=Airport.JFK,
                    arrival_airport=Airport.LAX,
                    departure_datetime=datetime.now(),
                    arrival_datetime=datetime.now() + timedelta(hours=3),
                    duration=180,
                )
            ],
        ),
        FlightResult(
            price=399.99,
            duration=240,
            stops=1,
            legs=[
                FlightLeg(
                    airline=Airline.UA,
                    flight_number="UA456",
                    departure_airport=Airport.JFK,
                    arrival_airport=Airport.ORD,
                    departure_datetime=datetime.now(),
                    arrival_datetime=datetime.now() + timedelta(hours=2),
                    duration=120,
                ),
                FlightLeg(
                    airline=Airline.UA,
                    flight_number="UA789",
                    departure_airport=Airport.ORD,
                    arrival_airport=Airport.LAX,
                    departure_datetime=datetime.now() + timedelta(hours=3),
                    arrival_datetime=datetime.now() + timedelta(hours=4),
                    duration=120,
                ),
            ],
        ),
    ]

    # Add round-trip mock results
    mock.search_round_trip.return_value = [
        {
            "outbound": FlightResult(
                price=299.99,
                duration=180,
                stops=0,
                legs=[
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL123",
                        departure_airport=Airport.JFK,
                        arrival_airport=Airport.LAX,
                        departure_datetime=datetime.now(),
                        arrival_datetime=datetime.now() + timedelta(hours=3),
                        duration=180,
                    )
                ],
            ),
            "return": FlightResult(
                price=299.99,
                duration=180,
                stops=0,
                legs=[
                    FlightLeg(
                        airline=Airline.DL,
                        flight_number="DL456",
                        departure_airport=Airport.LAX,
                        arrival_airport=Airport.JFK,
                        departure_datetime=datetime.now() + timedelta(days=7),
                        arrival_datetime=datetime.now() + timedelta(days=7, hours=3),
                        duration=180,
                    )
                ],
            ),
            "total_price": 599.98,
        }
    ]
    mock.build_flight_booking_url.return_value = (
        "https://www.google.com/travel/flights/booking?tfs=test"
    )
    monkeypatch.setattr("fli.search.flights.SearchFlights.__new__", lambda cls: mock)
    monkeypatch.setattr("fli.search.SearchFlights.__new__", lambda cls: mock)
    return mock


@pytest.fixture
def mock_search_dates(monkeypatch):
    """Mock SearchDates class."""
    mock = MagicMock()
    monkeypatch.setattr("fli.search.dates.SearchDates.__new__", lambda cls: mock)
    monkeypatch.setattr("fli.search.SearchDates.__new__", lambda cls: mock)
    return mock


@pytest.fixture
def mock_console(monkeypatch):
    """Mock the rich console to prevent output during tests."""
    mock = MagicMock()
    monkeypatch.setattr("fli.cli.utils.console", mock)
    return mock


@pytest.fixture(autouse=True)
def no_accidental_browser(request, monkeypatch):
    """Make the CLI tests incapable of starting a browser.

    ``fli multi`` now decides for itself whether to fetch the real
    multi-city board, and it decides on whether the ``browser`` extra is
    installed. ``all`` includes that extra and ``make test`` runs
    ``uv sync --all-extras``, so without this fixture the CLI tests would
    behave one way in a bare checkout and another way on a developer's
    machine — driving Chrome against Google for real in the second case.

    A test that wants the board path asks for it with
    ``@pytest.mark.usefixtures`` disabled or by patching
    ``fli.cli.commands.multi.SearchMultiCity`` and ``browser_available``
    itself; its own ``monkeypatch`` runs after this one and wins.
    """
    if request.node.get_closest_marker("wants_browser") is not None:
        yield
        return
    monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: False)
    yield
