"""Tests for the trip-duration sweep resolver shared by the CLI and MCP.

``resolve_duration_sweep`` owns every rule governing ``--duration`` vs
``--min-duration`` / ``--max-duration``: precedence, contradictory
combinations, and — most importantly — the hard cap on how many
(duration x date) page fetches a sweep is allowed to issue.
"""

import pytest

from fli.core.builders import MAX_DURATION_SWEEP_COMBINATIONS, resolve_duration_sweep
from fli.core.parsers import ParseError


def _resolve(**kwargs):
    """Call the resolver with the round-trip/no-flag defaults filled in."""
    params = {
        "trip_duration": None,
        "min_duration": None,
        "max_duration": None,
        "is_round_trip": True,
        "days_in_range": 30,
    }
    params.update(kwargs)
    return resolve_duration_sweep(**params)


def test_one_way_with_no_flags_yields_a_single_null_duration():
    """A one-way search has no trip duration at all."""
    assert _resolve(is_round_trip=False) == [None]


def test_round_trip_with_no_flags_keeps_the_three_day_default():
    """The effective default survives the ``int`` -> ``int | None`` sentinel."""
    assert _resolve() == [3]


def test_round_trip_with_explicit_duration_yields_just_that_duration():
    assert _resolve(trip_duration=5) == [5]


def test_min_and_max_expand_inclusively_on_both_ends():
    assert _resolve(min_duration=4, max_duration=7) == [4, 5, 6, 7]


def test_min_without_max_is_rejected():
    """An open-ended sweep is the request bomb; both bounds are required."""
    with pytest.raises(ParseError) as exc:
        _resolve(min_duration=4)
    assert "together" in str(exc.value)


def test_max_without_min_is_rejected():
    with pytest.raises(ParseError) as exc:
        _resolve(max_duration=7)
    assert "together" in str(exc.value)


def test_min_greater_than_max_is_rejected_naming_both_values():
    with pytest.raises(ParseError) as exc:
        _resolve(min_duration=7, max_duration=3)
    message = str(exc.value)
    assert "7" in message
    assert "3" in message


def test_duration_cannot_be_combined_with_a_sweep():
    with pytest.raises(ParseError) as exc:
        _resolve(trip_duration=5, min_duration=4, max_duration=7)
    message = str(exc.value)
    assert "Cannot combine" in message
    assert "--duration" in message


def test_sweep_requires_a_round_trip():
    with pytest.raises(ParseError) as exc:
        _resolve(min_duration=4, max_duration=7, is_round_trip=False)
    assert "--round" in str(exc.value)


def test_sweep_over_the_request_cap_is_rejected():
    """60 days x 30 durations = 1800 page fetches. Refuse before any network."""
    with pytest.raises(ParseError) as exc:
        _resolve(min_duration=1, max_duration=30, days_in_range=60)
    message = str(exc.value)
    assert "1800" in message
    assert str(MAX_DURATION_SWEEP_COMBINATIONS) in message
    assert "--min-duration" in message
    assert "--to" in message


def test_sweep_exactly_at_the_request_cap_is_allowed():
    """The boundary is inclusive, so a cap-sized sweep still runs."""
    days = 60
    durations = MAX_DURATION_SWEEP_COMBINATIONS // days
    result = _resolve(min_duration=1, max_duration=durations, days_in_range=days)
    assert result == list(range(1, durations + 1))
    assert len(result) * days == MAX_DURATION_SWEEP_COMBINATIONS


def test_parse_error_is_a_value_error():
    """Both surfaces already funnel ``ValueError`` into clean user-facing text."""
    assert issubclass(ParseError, ValueError)
