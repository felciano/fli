"""Tests for how the MCP executors report parameter validation failures.

A tool response of "Invalid parameter value" tells an agent nothing it can act
on, and a raw multi-line pydantic dump is worse. The executors must surface the
one specific message the validator already produced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fli.mcp.server import (
    DateSearchParams,
    FlightSearchParams,
    _execute_booking_options,
    _execute_date_search,
    _execute_flight_search,
)


def utc_today():
    """Today's date in UTC - the anchor the validators reference."""
    return datetime.now(timezone.utc).date()


def _iso(offset_days: int) -> str:
    """Return an ISO date ``offset_days`` from today in UTC."""
    return (utc_today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _make_raiser(exc: BaseException):
    """Return a callable that unconditionally raises ``exc``."""

    def _raiser(*args, **kwargs):
        raise exc

    return _raiser


def _assert_clean_message(error: str) -> None:
    """Assert an error is one actionable line, not a pydantic dump."""
    assert error.startswith("Invalid parameter value - ")
    assert "\n" not in error
    assert "errors.pydantic.dev" not in error
    assert "Value error, " not in error


@pytest.fixture
def past_flight_params():
    """Flight search params whose departure date is past everywhere on Earth."""
    return FlightSearchParams(
        origin="SFO",
        destination="LAX",
        departure_date=_iso(-30),
    )


def test_flight_search_past_date_names_the_field(past_flight_params):
    """Test _execute_flight_search reports which field is wrong and why."""
    result = _execute_flight_search(past_flight_params)

    assert result["success"] is False
    assert result["flights"] == []
    _assert_clean_message(result["error"])
    assert "travel_date" in result["error"]
    assert "Travel date cannot be in the past" in result["error"]


def test_date_search_past_date_names_the_field():
    """Test _execute_date_search reports the specific validation failure.

    This executor had no validation branch at all, so it leaked the whole
    four-line pydantic dump - including the errors.pydantic.dev URL - straight
    into the tool response.
    """
    result = _execute_date_search(
        DateSearchParams(
            origin="SFO",
            destination="LAX",
            start_date=_iso(-40),
            end_date=_iso(-30),
        )
    )

    assert result["success"] is False
    assert result["dates"] == []
    _assert_clean_message(result["error"])
    assert "travel_date" in result["error"]
    assert "Travel date cannot be in the past" in result["error"]


def test_booking_options_past_date_names_the_field(past_flight_params):
    """Test _execute_booking_options reports the specific validation failure."""
    result = _execute_booking_options(past_flight_params, None)

    assert result["success"] is False
    assert result["options"] == []
    _assert_clean_message(result["error"])
    assert "Travel date cannot be in the past" in result["error"]


def test_backwards_round_trip_message_has_no_placeholder_location():
    """Test a whole-model error is reported without a fake field name.

    The cross-segment check is a model validator, so its error carries an empty
    location. Joining that to a placeholder would put the literal word "input:"
    in front of an otherwise readable sentence.
    """
    result = _execute_flight_search(
        FlightSearchParams(
            origin="SFO",
            destination="LAX",
            departure_date=_iso(30),
            return_date=_iso(20),
        )
    )

    assert result["success"] is False
    _assert_clean_message(result["error"])
    assert "cannot be before departure date" in result["error"]
    assert "input:" not in result["error"]


def test_non_validation_exception_still_reported(monkeypatch):
    """Test a genuine search failure is not relabelled as a parameter problem.

    Guards against widening ``except ValidationError`` to ``except ValueError``,
    which would bury real failures behind "Invalid parameter value".
    """
    monkeypatch.setattr(
        "fli.mcp.server.SearchFlights.search",
        _make_raiser(RuntimeError("boom")),
    )
    result = _execute_flight_search(
        FlightSearchParams(origin="SFO", destination="LAX", departure_date=_iso(30))
    )

    assert result["success"] is False
    assert result["error"] == "Search failed: boom"


def test_parse_error_message_is_not_reformatted():
    """Test ParseError messages survive untouched.

    ParseError subclasses ValueError, so the ``except ValidationError`` clause
    must stay *after* ``except ParseError``. Reordering them would swallow every
    airport-resolution message behind a generic parameter complaint.
    """
    result = _execute_flight_search(
        FlightSearchParams(origin="ZZZ", destination="LAX", departure_date=_iso(30))
    )

    assert result["success"] is False
    assert result["error"] == "Invalid airport code: 'ZZZ'"
    assert "Invalid parameter value" not in result["error"]
