"""Tests for child and infant passenger counts on the MCP tools."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from fli.mcp.server import (
    DateSearchParams,
    FlightSearchParams,
    _build_flight_filters,
    _execute_date_search,
)


def _future_date(days: int = 30) -> str:
    """Return a future date string in YYYY-MM-DD format."""
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture
def mock_search_dates(monkeypatch):
    """Mock SearchDates so no live request is made."""
    mock = MagicMock()
    mock.search.return_value = []
    monkeypatch.setattr("fli.search.dates.SearchDates.__new__", lambda cls: mock)
    monkeypatch.setattr("fli.search.SearchDates.__new__", lambda cls: mock)
    return mock


def test_flight_filters_carry_child_and_infant_counts():
    """search_flights params thread child and infant counts into PassengerInfo."""
    params = FlightSearchParams(
        origin="JFK",
        destination="LHR",
        departure_date=_future_date(30),
        passengers=2,
        children=1,
        infants_in_seat=1,
        infants_on_lap=2,
    )

    filters, _, _, _ = _build_flight_filters(params)

    assert filters.passenger_info.adults == 2
    assert filters.passenger_info.children == 1
    assert filters.passenger_info.infants_in_seat == 1
    assert filters.passenger_info.infants_on_lap == 2


def test_flight_filters_default_child_and_infant_counts_to_zero():
    """Omitting the new counts leaves PassengerInfo at adults-only."""
    params = FlightSearchParams(
        origin="JFK",
        destination="LHR",
        departure_date=_future_date(30),
    )

    filters, _, _, _ = _build_flight_filters(params)

    assert filters.passenger_info.children == 0
    assert filters.passenger_info.infants_in_seat == 0
    assert filters.passenger_info.infants_on_lap == 0


def test_date_search_carries_child_and_infant_counts(mock_search_dates):
    """search_dates params thread child and infant counts into PassengerInfo."""
    params = DateSearchParams(
        origin="JFK",
        destination="LHR",
        start_date=_future_date(30),
        end_date=_future_date(60),
        passengers=2,
        children=1,
        infants_in_seat=1,
        infants_on_lap=2,
    )

    _execute_date_search(params)

    args, _ = mock_search_dates.search.call_args
    passenger_info = args[0].passenger_info
    assert passenger_info.adults == 2
    assert passenger_info.children == 1
    assert passenger_info.infants_in_seat == 1
    assert passenger_info.infants_on_lap == 2


def test_flight_params_reject_negative_children():
    """Negative child counts are rejected by the params model."""
    with pytest.raises(ValueError):
        FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=_future_date(30),
            children=-1,
        )
