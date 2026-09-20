"""``fli multi``: which of the two answers the user got, and whether it says so.

The command can print two very different things — a real multi-city board, or
per-leg research that is a *sum of independent one-way fares*. They look alike
on screen and mean different things, so every test here also asserts that the
output labels which one it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from fli.cli.main import app
from fli.models import Airline, Airport, FlightLeg, FlightResult, MultiCityBoard
from fli.search.exceptions import BrowserRpcTimeoutError

pytestmark = pytest.mark.wants_browser


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _unwrapped(text: str) -> str:
    """Undo the console's line wrapping so a message can be matched whole."""
    return text.replace("\n", "")


def _future(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _legs_args() -> list[str]:
    return [
        "multi",
        "--leg",
        f"SEA,HKG,{_future(30)}",
        "--leg",
        f"PEK,SEA,{_future(40)}",
    ]


def _board() -> MultiCityBoard:
    now = datetime.now() + timedelta(days=30)
    return MultiCityBoard(
        results=[
            FlightResult(
                price=1395.0,
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
        ],
        legs=[("SEA", "HKG", _future(30)), ("PEK", "SEA", _future(40))],
        booking_url="https://www.google.com/travel/flights?tfs=BOARD",
    )


@pytest.fixture
def with_browser(monkeypatch):
    """Pretend the extra is installed and hand back a canned board."""
    monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: True)
    search = MagicMock()
    search.search.return_value = _board()
    monkeypatch.setattr("fli.cli.commands.multi.SearchMultiCity", lambda *a, **k: search)
    return search


@pytest.fixture
def without_browser(monkeypatch):
    monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: False)


class TestBoardPath:
    def test_prints_the_board_and_says_what_the_price_covers(self, runner, with_browser):
        result = runner.invoke(app, _legs_args())
        assert result.exit_code == 0
        assert "Multi-city board" in result.stdout
        assert "ENTIRE" in result.stdout
        assert "tfs=BOARD" in result.stdout
        # Each option must not be headed "One-way Flight", which is what the
        # generic renderer says unless it is told the trip type.
        assert "One-way Flight" not in result.stdout
        assert "Multi-city Flight" in result.stdout

    def test_the_board_path_never_runs_per_leg_research(self, runner, with_browser, monkeypatch):
        """Researching legs after fetching the board would be wasted requests."""

        def explode(*args, **kwargs):
            raise AssertionError("per-leg research must not run when a board was fetched")

        monkeypatch.setattr("fli.cli.commands.multi.SearchFlights", explode)
        assert runner.invoke(app, _legs_args()).exit_code == 0

    def test_cdp_endpoint_flag_reaches_the_search(self, runner, monkeypatch):
        captured: dict = {}
        search = MagicMock()
        search.search.return_value = _board()

        def factory(options=None, *args, **kwargs):
            captured["options"] = options
            return search

        monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: True)
        monkeypatch.setattr("fli.cli.commands.multi.SearchMultiCity", factory)
        result = runner.invoke(app, [*_legs_args(), "--browser-cdp", "http://127.0.0.1:9222"])
        assert result.exit_code == 0
        assert captured["options"].cdp_endpoint == "http://127.0.0.1:9222"


class TestFallback:
    def test_no_browser_flag_says_so_before_showing_research(
        self, runner, with_browser, mock_search_flights
    ):
        result = runner.invoke(app, [*_legs_args(), "--no-browser"])
        assert result.exit_code == 0
        assert "--no-browser" in result.stdout
        assert "per-leg research" in result.stdout.lower()
        with_browser.search.assert_not_called()

    def test_missing_extra_names_the_install_then_falls_back(
        self, runner, without_browser, mock_search_flights
    ):
        result = runner.invoke(app, _legs_args())
        assert result.exit_code == 0
        # Rich wraps the console output, so compare against the unwrapped text.
        assert "flights[browser]" in _unwrapped(result.stdout)
        assert "per-leg research" in result.stdout.lower()

    def test_a_browser_failure_is_reported_not_swallowed(
        self, runner, monkeypatch, mock_search_flights
    ):
        search = MagicMock()
        search.search.side_effect = BrowserRpcTimeoutError("the page never asked")
        monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: True)
        monkeypatch.setattr("fli.cli.commands.multi.SearchMultiCity", lambda *a, **k: search)
        result = runner.invoke(app, _legs_args())
        assert result.exit_code == 0
        assert "Could not fetch the multi-city board" in result.stdout
        assert "the page never asked" in result.stdout

    def test_explicit_browser_flag_fails_instead_of_downgrading(self, runner, monkeypatch):
        """Fail rather than quietly hand back a different product.

        Someone who typed --browser asked for the board specifically.
        """
        search = MagicMock()
        search.search.side_effect = BrowserRpcTimeoutError("the page never asked")
        monkeypatch.setattr("fli.cli.commands.multi.browser_available", lambda: True)
        monkeypatch.setattr("fli.cli.commands.multi.SearchMultiCity", lambda *a, **k: search)
        result = runner.invoke(app, [*_legs_args(), "--browser"])
        assert result.exit_code != 0

    def test_explicit_browser_flag_without_the_extra_is_a_usage_error(
        self, runner, without_browser
    ):
        result = runner.invoke(app, [*_legs_args(), "--browser"])
        assert result.exit_code != 0
        assert "flights[browser]" in _unwrapped(result.stdout)

    def test_research_output_is_labelled_as_not_a_multi_city_result(
        self, runner, without_browser, mock_search_flights
    ):
        result = runner.invoke(app, _legs_args())
        assert "independent one-way" in result.stdout
