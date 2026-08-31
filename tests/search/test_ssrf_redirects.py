"""Redirect-SSRF hardening tests (GHSA-qw2m-4pqf-rmpp / CVE-2026-33752).

curl-cffi < 0.15.0 follows HTTP redirects into private/internal address
space with no way to opt out. 0.15.0 added ``CurlFollow.SAFE``, reachable
from the requests layer as ``allow_redirects="safe"``, which follows
redirects but rejects any hop to a private IP.

The remediation is **opt-in**: ``allow_redirects=True`` still follows
private-IP redirects on every released version. So bumping the dependency
floor alone changes nothing at runtime — these tests pin both halves of
the fix: the floor that makes ``"safe"`` available, and the call sites
that actually pass it.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import fli.search.client as client_module
from fli.search.client import Client
from fli.search.exceptions import SearchConnectionError

SENTINEL = b"INTERNAL-METADATA-LEAKED"


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Each test starts with a clean singleton so tests don't share state."""
    original = client_module.client
    client_module.client = None
    yield
    client_module.client = original


class _Recorder:
    """Stand-in for a ``curl_cffi`` session that records request kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _respond(self, url: str, **kwargs: Any):  # noqa: ANN401, ARG002
        self.calls.append(dict(kwargs))
        return type("R", (), {"text": "", "raise_for_status": lambda self: None})()

    get = _respond
    post = _respond


def _fake_page(payload: Any) -> str:
    from tests.search._pages import as_search_page

    return as_search_page(payload)


# ---------------------------------------------------------------------------
# 1 + 2: the dependency floor must make the "safe" mode actually available
# ---------------------------------------------------------------------------


def test_curl_cffi_provides_safe_redirect_follow():
    """The installed curl-cffi must expose ``CurlFollow.SAFE`` (added in 0.15.0)."""
    from curl_cffi.const import CurlFollow

    assert CurlFollow.SAFE == 4


def test_declared_curl_cffi_floor_excludes_vulnerable_versions():
    """The declared floor must exclude every version affected by the advisory."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[import-not-found, no-redef]

    from packaging.requirements import Requirement

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    entries = [
        Requirement(dep)
        for dep in data["project"]["dependencies"]
        if Requirement(dep).name == "curl-cffi"
    ]
    assert len(entries) == 1, "expected exactly one curl-cffi dependency entry"
    spec = entries[0].specifier

    assert "0.13.0" not in spec
    assert "0.14.0" not in spec
    assert "0.15.0" in spec


# ---------------------------------------------------------------------------
# 3: the client must default to the safe mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post"])
def test_client_defaults_to_safe_redirects(monkeypatch, method):
    """``Client.get``/``Client.post`` must send ``allow_redirects="safe"`` by default."""
    recorder = _Recorder()
    monkeypatch.setattr(Client, "_session", lambda self: recorder)

    getattr(Client(), method)("https://www.google.com/x")

    assert recorder.calls, "transport was never invoked"
    # Assert the exact string: True is truthy *and* is the vulnerable value,
    # so a truthiness assertion here would catch nothing.
    assert recorder.calls[0].get("allow_redirects") == "safe"


@pytest.mark.parametrize("method", ["get", "post"])
def test_client_caller_can_still_override_redirect_mode(monkeypatch, method):
    """The default must be a ``setdefault``, not a hard override."""
    recorder = _Recorder()
    monkeypatch.setattr(Client, "_session", lambda self: recorder)

    getattr(Client(), method)("https://www.google.com/x", allow_redirects=False)

    assert recorder.calls[0].get("allow_redirects") is False


# ---------------------------------------------------------------------------
# 4: no search call site may downgrade the client default back to True
# ---------------------------------------------------------------------------


def _assert_never_unsafe(calls: list[dict[str, Any]]) -> None:
    assert calls, "transport was never invoked"
    for kwargs in calls:
        # Either the kwarg is omitted (inheriting the client default) or it is
        # explicitly "safe" — never True, which is the vulnerable value.
        assert kwargs.get("allow_redirects", "safe") == "safe"


def test_search_flights_does_not_override_safe_redirects(monkeypatch):
    """``SearchFlights.search`` must not pass ``allow_redirects=True``."""
    from datetime import datetime, timedelta

    from fli.models import (
        Airport,
        FlightSearchFilters,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
        SortBy,
    )
    from fli.search import SearchFlights

    filters = FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.PHX, 0]],
                arrival_airport=[[Airport.SFO, 0]],
                travel_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
            )
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
    )

    calls: list[dict[str, Any]] = []
    page = _fake_page([[None, None, None, None, "SESSION"], None, [[]], None])

    def _fake_get(url, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        return type("R", (), {"text": page, "raise_for_status": lambda s: None})()

    search = SearchFlights()
    monkeypatch.setattr(search.client, "get", _fake_get)
    search.search(filters)

    _assert_never_unsafe(calls)


def test_search_dates_does_not_override_safe_redirects(monkeypatch):
    """``SearchDates.search`` must not pass ``allow_redirects=True``."""
    from datetime import datetime, timedelta

    from fli.models import (
        Airport,
        DateSearchFilters,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
        SortBy,
    )
    from fli.search import SearchDates

    future = datetime.now() + timedelta(days=30)
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.PHX, 0]],
                arrival_airport=[[Airport.SFO, 0]],
                travel_date=future.strftime("%Y-%m-%d"),
            )
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
        from_date=future.strftime("%Y-%m-%d"),
        to_date=(future + timedelta(days=2)).strftime("%Y-%m-%d"),
    )

    calls: list[dict[str, Any]] = []
    page = _fake_page([[None, None, None, None, "SESSION"], None, [[]], None])

    def _fake_get(url, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        return type("R", (), {"text": page, "raise_for_status": lambda s: None})()

    search = SearchDates()
    monkeypatch.setattr(search.client, "get", _fake_get)
    search.search(filters)

    _assert_never_unsafe(calls)


def test_get_booking_options_does_not_override_safe_redirects(monkeypatch):
    """The booking-options POST must not pass ``allow_redirects=True`` either."""
    from datetime import datetime, timedelta

    from fli.models import (
        Airport,
        FlightSearchFilters,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
        SortBy,
    )
    from fli.search import SearchFlights

    filters = FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.PHX, 0]],
                arrival_airport=[[Airport.SFO, 0]],
                travel_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
            )
        ],
        stops=MaxStops.NON_STOP,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
    )

    calls: list[dict[str, Any]] = []

    def _fake_post(url=None, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        return type("R", (), {"text": "", "raise_for_status": lambda s: None})()

    search = SearchFlights()
    search._session_id = "FAKE_SESSION"  # noqa: SLF001
    monkeypatch.setattr(search.client, "post", _fake_post)

    flight = _first_flight_stub()
    # An explicit token skips the session-id plumbing; we only care that the
    # POST reaches the transport with a safe redirect mode.
    search.get_booking_options(flight, filters, booking_token="FAKE_TOKEN")

    _assert_never_unsafe(calls)


def _first_flight_stub():
    """Build the minimal ``FlightResult`` ``get_booking_options`` needs."""
    from datetime import datetime, timedelta

    from fli.models import Airline, Airport, FlightLeg, FlightResult, MaxStops

    dep = datetime.now() + timedelta(days=30)
    return FlightResult(
        price=100.0,
        duration=60,
        stops=0,
        legs=[
            FlightLeg(
                airline=Airline.AA,
                flight_number="100",
                departure_airport=Airport.PHX,
                arrival_airport=Airport.SFO,
                departure_datetime=dep,
                arrival_datetime=dep + timedelta(hours=1),
                duration=60,
            )
        ],
        max_stops=MaxStops.NON_STOP,
    )


# ---------------------------------------------------------------------------
# 5: end-to-end behavioural proof — a redirect into loopback is refused
# ---------------------------------------------------------------------------


class _RedirectToLoopbackHandler(BaseHTTPRequestHandler):
    """Serves ``/redirect`` -> 302 -> ``/internal-secret`` on loopback."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/redirect":
            host, port = self.server.server_address[0], self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{port}/internal-secret")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(SENTINEL)))
        self.end_headers()
        self.wfile.write(SENTINEL)

    def log_message(self, *args: Any) -> None:  # noqa: ANN401
        """Silence the stdlib request log."""


@pytest.fixture
def loopback_redirect_server():
    """Run a loopback HTTP server that 302s to a private-IP URL."""
    server = HTTPServer(("127.0.0.1", 0), _RedirectToLoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_safe_redirect_blocks_private_ip_redirect(monkeypatch, loopback_redirect_server):
    """A 3xx hop into loopback must be refused, not followed."""
    from tenacity import stop_after_attempt

    # Keep the test fast: the retry decorator would otherwise back off ~3s.
    monkeypatch.setattr(Client.get.retry, "stop", stop_after_attempt(1), raising=False)

    with pytest.raises(SearchConnectionError):
        Client().get(f"{loopback_redirect_server}/redirect")


def test_direct_fetch_of_internal_route_still_works(loopback_redirect_server):
    """Sanity check: only the *redirect* is blocked, not loopback itself."""
    response = Client().get(f"{loopback_redirect_server}/internal-secret")
    assert SENTINEL in response.content
