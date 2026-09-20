r"""Overnight soak of the optional browser transport, with an HTTP control.

Why this exists
---------------
The browser transport works *now*. What nobody knows is whether it keeps
working unattended: whether Google's attestation holds for hours, whether the
consent wall comes back, whether the CDP browser survives the night, and
whether a degradation is specific to the browser path or is Google blocking
this IP generally. Those last two are very different diagnoses and a soak that
cannot tell them apart is not worth running — so every cycle also runs a plain
HTTP one-way search as a **control**:

* browser checks failing while the control passes  → the browser path broke.
* everything failing together                      → Google is blocking us.
* control failing alone                            → ordinary network trouble.

Traffic discipline
------------------
``GetShoppingResults`` and ``GetExploreDestinations`` are gated *precisely* to
stop automated use. This harness is therefore deliberately slow: one cycle of
three checks every ``--interval-minutes`` (default 30, i.e. six requests an
hour), jittered, with the checks inside a cycle spaced by ``--gap-seconds``.
Browser failures multiply the next interval by ``--backoff`` up to
``--max-interval-minutes``, because the response to a service that just
declined a request is to back off, not to find out how fast it will decline
the next one. Do not "tune" these defaults downward to get more data points:
getting this IP flagged would land on the default HTTP path too, which is the
path everybody actually uses.

Usage
-----
Attach to an already-running Chrome (the only mode that works in a sandbox)::

    uv run --with playwright python scripts/soak_browser_transport.py \
        --cdp-endpoint http://127.0.0.1:9222 \
        --results ~/Developer/localdata/felciano/fli/soak-2026-09-20.jsonl \
        --max-hours 8

In the morning::

    uv run python scripts/soak_browser_transport.py --summarise \
        --results ~/Developer/localdata/felciano/fli/soak-2026-09-20.jsonl

Point ``--results`` outside the repo (``~/Developer/localdata/<org>/<repo>/``
is the house location for exactly this); the default writes into the working
directory, which is convenient and is not where an overnight run belongs.

The results file
----------------
One JSON line **per check**, not per cycle — a cycle's three checks fail for
different reasons and averaging them away is the one thing a morning reader
must not be made to do. Each line carries the cycle number, so cycles are
recoverable; ``--summarise`` reports both.

``ok: false`` covers more than exceptions. A board that comes back with zero
itineraries is recorded as ``no_results`` and an Explore response that decodes
to no destinations as ``decoded_empty``: neither raises, both are findings.
The summariser lists failure classes separately, so these never hide inside a
bare success rate.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fli.models import (
    Airport,
    ExploreSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
    TripType,
)
from fli.search import SearchExplore, SearchFlights, SearchMultiCity
from fli.search._browser import BrowserOptions
from fli.search.exceptions import (
    BrowserAttestationRejectedError,
    BrowserConsentRequiredError,
    BrowserDecodeError,
    BrowserRpcTimeoutError,
    BrowserTransportUnavailableError,
    BrowserUnreachableError,
    SearchClientError,
    SearchConnectionError,
    SearchHTTPError,
    SearchParseError,
    SearchRejectedError,
    SearchTimeoutError,
    SearchUnsupportedError,
)
from fli.search.transport import Transport

logger = logging.getLogger("soak")

#: The checks a cycle runs, in order. ``http_control`` is last on purpose: it
#: is the cheapest and the one most worth having even if the run is killed.
CHECKS: tuple[str, ...] = ("multi_city", "explore", "http_control")

#: Which transport each check exercises. Drives the diagnosis of a bare
#: ``SearchRejectedError``: over HTTP error 13 is the known bgr gate and is
#: expected; through a real browser it means attestation was refused, which is
#: the headline finding this soak exists to catch.
TRANSPORT_OF: dict[str, str] = {
    "multi_city": "browser",
    "explore": "browser",
    "http_control": "http",
}

#: Substrings that mean the CDP browser went away mid-capture. Playwright
#: reports this as ``TargetClosedError`` or a websocket error rather than
#: anything fli can type, so it is matched on text.
_CDP_LOST_MARKERS: tuple[str, ...] = (
    "targetclosed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "connection closed",
    "websocket",
    "econnrefused",
    "connection refused",
)

#: How much of an exception message to keep in a record.
_ERROR_CHARS = 400


class SoakCheckFailure(Exception):
    """A check produced a disappointing answer rather than an exception.

    Carries the failure tag directly so an empty board and a refused request
    land in the same classified column of the results file.
    """

    def __init__(self, failure: str, message: str) -> None:
        """Record the failure tag alongside the human-readable message.

        Args:
            failure: The failure class, e.g. ``"no_results"``.
            message: What happened, for the record's ``error`` field.

        """
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class SoakConfig:
    """Everything a cycle needs that does not change between cycles.

    Attributes:
        browser_options: How to reach the browser, already resolved.
        days_ahead: How far out the first searched date sits, in days.
        origin: Origin airport for every check.
        first_stop: Second city of the multi-city itinerary, and the
            destination of the HTTP control — so the control shares a route
            family with the browser checks.
        second_stop: Third city of the multi-city itinerary.
        currency: ``curr`` URL param passed to every check.
        gap_seconds: Pause between checks inside one cycle.

    """

    browser_options: BrowserOptions
    days_ahead: int = 90
    origin: Airport = Airport.LHR
    first_stop: Airport = Airport.BOS
    second_stop: Airport = Airport.CDG
    currency: str = "USD"
    gap_seconds: float = 20.0

    def dates(self) -> tuple[str, str, str]:
        """Return the three far-future dates the itinerary uses.

        Far future keeps the boards populated, so an empty board stays a real
        finding rather than an artefact of searching a sold-out week.

        Returns:
            Three ``YYYY-MM-DD`` strings, a week apart.

        """
        first = date.today() + timedelta(days=self.days_ahead)
        return (
            first.isoformat(),
            (first + timedelta(days=7)).isoformat(),
            (first + timedelta(days=14)).isoformat(),
        )


@dataclass
class CheckOutcome:
    """The result of one check, ready to be written as a record.

    Attributes:
        ok: Whether the check got what it wanted.
        count: How many things decoded — itineraries, destinations, flights.
        failure: The failure class, or ``None`` when ``ok``.
        error_class: The exception type name, when one was raised.
        error: A truncated exception message.
        detail: Check-specific extras, e.g. whether the page needed a nudge.

    """

    ok: bool
    count: int | None = None
    failure: str | None = None
    error_class: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _multi_city_filters(config: SoakConfig) -> FlightSearchFilters:
    """Build a genuinely multi-city itinerary — three legs, no A→B→A pair.

    Google normalises an ``A→B``/``B→A`` pair into a round trip and serves it
    inline, so a two-leg "multi-city" would silently test the HTTP path.

    Args:
        config: The soak configuration.

    Returns:
        Filters for a three-leg search.

    """
    one, two, three = config.dates()
    legs = [
        (config.origin, config.first_stop, one),
        (config.first_stop, config.second_stop, two),
        (config.second_stop, config.origin, three),
    ]
    return FlightSearchFilters(
        trip_type=TripType.MULTI_CITY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[destination, 0]],
                travel_date=travel_date,
            )
            for origin, destination, travel_date in legs
        ],
    )


def check_multi_city(config: SoakConfig) -> tuple[int, dict[str, Any]]:
    """Fetch a multi-city board through the browser transport.

    Args:
        config: The soak configuration.

    Returns:
        The number of itineraries and a detail mapping.

    Raises:
        SoakCheckFailure: Google returned a board with nothing on it.

    """
    board = SearchMultiCity(options=config.browser_options).search(
        _multi_city_filters(config), currency=config.currency
    )
    if board is None:
        raise SoakCheckFailure(
            "no_results",
            "the multi-city board decoded but carried no itineraries at all",
        )
    priced = [result.price for result in board.results if result.price is not None]
    return len(board.results), {
        "priced": len(priced),
        "cheapest": min(priced) if priced else None,
    }


def check_explore(config: SoakConfig) -> tuple[int, dict[str, Any]]:
    """Fetch an Explore board through the browser transport.

    Args:
        config: The soak configuration.

    Returns:
        The number of destinations and a detail mapping.

    Raises:
        SoakCheckFailure: The response carried no parseable destinations.

    """
    departure, _, _ = config.dates()
    result = SearchExplore(options=config.browser_options).search(
        ExploreSearchFilters(origin=config.origin, departure_date=departure),
        currency=config.currency,
    )
    if result is None:
        raise SoakCheckFailure(
            "decoded_empty",
            "the Explore response was intercepted but decoded to no destinations",
        )
    priced = [d for d in result.destinations if d.price is not None]
    return len(result.destinations), {"priced": len(priced), "origin": result.origin_name}


def check_http_control(config: SoakConfig) -> tuple[int, dict[str, Any]]:
    """Run a plain one-way HTTP search — the control, no browser involved.

    ``Transport.HTTP`` is passed explicitly rather than relied on: the point
    of a control is that it cannot quietly become the thing it is controlling
    for, however ``AUTO`` is routed in future.

    Args:
        config: The soak configuration.

    Returns:
        The number of flights and a detail mapping.

    Raises:
        SoakCheckFailure: Google returned no flights for a route that has them.

    """
    departure, _, _ = config.dates()
    flights = SearchFlights().search(
        FlightSearchFilters(
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[config.origin, 0]],
                    arrival_airport=[[config.first_stop, 0]],
                    travel_date=departure,
                )
            ],
        ),
        top_n=5,
        currency=config.currency,
        transport=Transport.HTTP,
    )
    if not flights:
        raise SoakCheckFailure(
            "no_results",
            f"the control search {config.origin.name}→{config.first_stop.name} "
            "returned no flights, which this route always has",
        )
    return len(flights), {}


CHECK_FUNCTIONS: dict[str, Callable[[SoakConfig], tuple[int, dict[str, Any]]]] = {
    "multi_city": check_multi_city,
    "explore": check_explore,
    "http_control": check_http_control,
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_failure(exc: BaseException, *, transport: str) -> str:
    """Name the failure mode, so a morning reader gets a diagnosis not a stack.

    Order matters: the browser-specific errors subclass their HTTP
    equivalents, so they are tested first.

    Args:
        exc: What was raised.
        transport: ``"browser"`` or ``"http"`` — the same error code means
            different things on the two paths.

    Returns:
        A stable, greppable failure class.

    """
    if isinstance(exc, SoakCheckFailure):
        return exc.failure
    if isinstance(exc, BrowserConsentRequiredError):
        return "consent_wall"
    if isinstance(exc, BrowserUnreachableError):
        return "cdp_unreachable"
    if isinstance(exc, BrowserAttestationRejectedError):
        return "attestation_refused"
    if isinstance(exc, BrowserRpcTimeoutError):
        return "rpc_never_fired"
    if isinstance(exc, BrowserDecodeError):
        return "decoded_empty"
    if isinstance(exc, BrowserTransportUnavailableError):
        return "browser_not_installed"
    if isinstance(exc, SearchRejectedError):
        # Error 13 over HTTP is the known bgr gate and tells us nothing new.
        # Through a real browser it is attestation being refused, which is the
        # headline this soak exists to catch.
        if exc.code == 13:
            return "attestation_refused" if transport == "browser" else "gate_refused_13"
        return f"rejected_{exc.code}"
    if _looks_like_lost_browser(exc):
        return "cdp_unreachable"
    if isinstance(exc, SearchTimeoutError):
        return "timeout"
    if isinstance(exc, SearchConnectionError):
        return "connection"
    if isinstance(exc, SearchHTTPError):
        return f"http_{exc.status_code}"
    if isinstance(exc, SearchUnsupportedError):
        return "unsupported"
    if isinstance(exc, SearchParseError):
        return "parse_error"
    if isinstance(exc, SearchClientError):
        return "client_error"
    return f"unexpected_{type(exc).__name__}"


def _looks_like_lost_browser(exc: BaseException) -> bool:
    """Report whether an untyped exception is really the CDP browser vanishing.

    Playwright's ``TargetClosedError`` and its websocket failures do not reach
    fli as anything fli can name, and "the user quit Chrome at 3am" must not
    be reported overnight as a Google problem.

    Args:
        exc: What was raised.

    Returns:
        ``True`` when the exception's type or message names a closed target.

    """
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _CDP_LOST_MARKERS)


def cdp_reachable(endpoint: str | None, *, timeout: float = 5.0) -> bool | None:
    """Ask the CDP endpoint whether it is still there. Costs Google nothing.

    Recorded on every line so "the browser path broke" can be separated from
    "the browser went away" without reading exception text.

    Args:
        endpoint: The configured CDP endpoint, or ``None`` when fli launches
            its own browser and there is nothing to probe.
        timeout: Seconds to wait for the local HTTP response.

    Returns:
        ``True``/``False``, or ``None`` when no endpoint is configured.

    """
    if not endpoint:
        return None
    url = endpoint.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("CDP probe failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_check(name: str, config: SoakConfig) -> CheckOutcome:
    """Run one check and describe what happened. Never raises.

    Args:
        name: One of :data:`CHECKS`.
        config: The soak configuration.

    Returns:
        The outcome, successful or not.

    """
    transport = TRANSPORT_OF[name]
    try:
        count, detail = CHECK_FUNCTIONS[name](config)
    except Exception as exc:  # noqa: BLE001 - a failed check is data, not a crash
        failure = classify_failure(exc, transport=transport)
        logger.warning("%s failed: %s (%s)", name, failure, type(exc).__name__)
        return CheckOutcome(
            ok=False,
            failure=failure,
            error_class=type(exc).__name__,
            error=str(exc)[:_ERROR_CHARS] or None,
        )
    logger.info("%s ok: %d decoded", name, count)
    return CheckOutcome(ok=True, count=count, detail=detail)


def write_record(results_path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line, reopening the file each time.

    Reopening per record costs nothing at one cycle per half hour and means a
    kill -9 at 4am loses at most the line being written.

    Args:
        results_path: Where the results file lives.
        record: The record to serialise.

    """
    with results_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def run_cycle(
    cycle: int,
    config: SoakConfig,
    results_path: Path,
    checks: Sequence[str],
    stop: threading.Event,
) -> list[CheckOutcome]:
    """Run every check once, writing a record each. Never raises.

    Args:
        cycle: The 1-based cycle number.
        config: The soak configuration.
        results_path: Where to append records.
        checks: Which checks to run, in order.
        stop: Set when the run should wind up; checked between checks so a
            SIGINT does not have to wait out the whole cycle.

    Returns:
        One outcome per check that actually ran.

    """
    outcomes: list[CheckOutcome] = []
    for index, name in enumerate(checks):
        if stop.is_set():
            break
        if index:
            stop.wait(config.gap_seconds)
            if stop.is_set():
                break
        started = time.monotonic()
        probe = cdp_reachable(config.browser_options.cdp_endpoint)
        outcome = run_check(name, config)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        outcomes.append(outcome)
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "cycle": cycle,
            "check": name,
            "transport": TRANSPORT_OF[name],
            "ok": outcome.ok,
            "failure": outcome.failure,
            "error_class": outcome.error_class,
            "error": outcome.error,
            "count": outcome.count,
            "elapsed_ms": elapsed_ms,
            "cdp_ok": probe,
            **outcome.detail,
        }
        try:
            write_record(results_path, record)
        except OSError as exc:  # pragma: no cover - disk full, unwritable path
            logger.error("could not write the results file: %s", exc)
    return outcomes


def soak(args: argparse.Namespace) -> int:
    """Run the soak until SIGINT, ``--max-hours`` or ``--max-cycles``.

    Args:
        args: Parsed command-line arguments.

    Returns:
        ``0`` when the run completed or was interrupted cleanly, ``1`` when it
        could not start at all.

    """
    results_path = args.results.expanduser()
    try:
        results_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("cannot create %s: %s", results_path.parent, exc)
        return 1

    options = _browser_options(args)
    config = SoakConfig(
        browser_options=options,
        days_ahead=args.days_ahead,
        currency=args.currency,
        gap_seconds=args.gap_seconds,
    )
    checks = tuple(args.only) if args.only else CHECKS

    if options.cdp_endpoint and cdp_reachable(options.cdp_endpoint) is False:
        logger.warning(
            "the CDP endpoint %s is not answering — starting anyway so the "
            "results file records that, but every browser check will fail",
            options.cdp_endpoint,
        )

    stop = threading.Event()
    _install_signal_handlers(stop)

    deadline = time.monotonic() + args.max_hours * 3600
    interval = args.interval_minutes * 60
    max_interval = args.max_interval_minutes * 60
    consecutive_browser_failures = 0
    cycle = 0

    logger.info(
        "soak starting: checks=%s interval=%.0fmin max=%.1fh results=%s",
        ",".join(checks),
        args.interval_minutes,
        args.max_hours,
        results_path,
    )

    while not stop.is_set() and time.monotonic() < deadline:
        cycle += 1
        outcomes = run_cycle(cycle, config, results_path, checks, stop)
        if stop.is_set():
            break
        if args.max_cycles and cycle >= args.max_cycles:
            logger.info("reached --max-cycles %d", args.max_cycles)
            break

        browser_failed = any(
            not outcome.ok
            for name, outcome in zip(checks, outcomes, strict=False)
            if TRANSPORT_OF[name] == "browser"
        )
        if browser_failed:
            consecutive_browser_failures += 1
        else:
            consecutive_browser_failures = 0

        backed_off = min(interval * (args.backoff**consecutive_browser_failures), max_interval)
        sleep_for = backed_off * (1 + random.uniform(-args.jitter, args.jitter))
        sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
        if sleep_for <= 0:
            break
        if consecutive_browser_failures:
            logger.info(
                "backing off after %d consecutive browser failure(s): sleeping %.1f min",
                consecutive_browser_failures,
                sleep_for / 60,
            )
        else:
            logger.info("cycle %d done; sleeping %.1f min", cycle, sleep_for / 60)
        stop.wait(sleep_for)

    logger.info("soak finished after %d cycle(s); results in %s", cycle, results_path)
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    """Make SIGINT and SIGTERM wind the run up instead of killing it mid-cycle.

    Args:
        stop: The event every wait in the loop watches.

    """

    def _handle(signum: int, _frame: Any) -> None:
        logger.info("signal %d received — finishing the current check and stopping", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def _browser_options(args: argparse.Namespace) -> BrowserOptions:
    """Resolve browser options from the environment, overridden by flags.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The options every browser check will use.

    """
    options = BrowserOptions.from_env()
    overrides: dict[str, Any] = {}
    if args.cdp_endpoint:
        overrides["cdp_endpoint"] = args.cdp_endpoint
    if args.timeout_ms:
        overrides["timeout_ms"] = args.timeout_ms
    if args.headed:
        overrides["headless"] = False
    return options.model_copy(update=overrides) if overrides else options


# ---------------------------------------------------------------------------
# Summarising
# ---------------------------------------------------------------------------


def summarise(results_path: Path) -> int:
    """Print success rate, failure classes and failure times, per check.

    Args:
        results_path: The results file to read.

    Returns:
        ``0`` when the file was read, ``1`` when it does not exist, ``2``
        when it held no usable records.

    """
    path = results_path.expanduser()
    if not path.exists():
        print(f"no results file at {path}", file=sys.stderr)
        return 1

    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(record, dict) and record.get("check"):
            records.append(record)
        else:
            malformed += 1

    if not records:
        print(f"{path} held no usable records ({malformed} malformed line(s))", file=sys.stderr)
        return 2

    _print_summary(path, records, malformed)
    return 0


def _print_summary(path: Path, records: list[dict[str, Any]], malformed: int) -> None:
    """Render the summary. Separated from parsing so each stays readable.

    Args:
        path: The results file, for the heading.
        records: Every parsed record.
        malformed: How many lines could not be parsed.

    """
    cycles = {record.get("cycle") for record in records if record.get("cycle") is not None}
    stamps = sorted(str(record.get("ts", "")) for record in records if record.get("ts"))
    print(f"soak summary — {path}")
    print(
        f"  {len(records)} record(s) across {len(cycles)} cycle(s)"
        + (f", {malformed} malformed line(s)" if malformed else "")
    )
    if stamps:
        print(f"  window: {stamps[0]} → {stamps[-1]}")
    print()

    by_check: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_check[str(record["check"])].append(record)

    print(f"  {'check':<14}{'runs':>6}{'ok':>6}{'rate':>9}{'median ms':>12}  failures")
    order = [name for name in CHECKS if name in by_check]
    order += sorted(name for name in by_check if name not in CHECKS)
    for name in order:
        rows = by_check[name]
        ok_rows = [row for row in rows if row.get("ok")]
        durations = [row["elapsed_ms"] for row in rows if isinstance(row.get("elapsed_ms"), int)]
        median = f"{statistics.median(durations):.0f}" if durations else "-"
        failures = Counter(
            str(row.get("failure") or "unknown") for row in rows if not row.get("ok")
        )
        rate = f"{100 * len(ok_rows) / len(rows):.1f}%"
        summary = ", ".join(f"{cls}×{n}" for cls, n in failures.most_common()) or "none"
        print(f"  {name:<14}{len(rows):>6}{len(ok_rows):>6}{rate:>9}{median:>12}  {summary}")

    failed = [row for row in records if not row.get("ok")]
    print()
    if not failed:
        print("  no failures recorded.")
        _print_verdict(by_check)
        return

    print(f"  {len(failed)} failure(s) overall:")
    for cls, count in Counter(str(row.get("failure") or "unknown") for row in failed).most_common():
        checks = sorted({str(row.get("check")) for row in failed if row.get("failure") == cls})
        print(f"    {cls:<24}{count:>4}   ({', '.join(checks)})")

    timed = sorted(failed, key=lambda row: str(row.get("ts", "")))
    first, last = timed[0], timed[-1]
    print()
    print(f"  first failure: {first.get('ts')}  {first.get('check')}  {first.get('failure')}")
    print(f"  last  failure: {last.get('ts')}  {last.get('check')}  {last.get('failure')}")
    _print_verdict(by_check)


def _print_verdict(by_check: dict[str, list[dict[str, Any]]]) -> None:
    """Say what the control implies, which is the whole point of having one.

    Args:
        by_check: Records grouped by check name.

    """
    browser_rows = [
        row
        for name, rows in by_check.items()
        if TRANSPORT_OF.get(name) == "browser"
        for row in rows
    ]
    control_rows = by_check.get("http_control", [])
    if not browser_rows or not control_rows:
        return

    browser_ok = sum(1 for row in browser_rows if row.get("ok")) / len(browser_rows)
    control_ok = sum(1 for row in control_rows if row.get("ok")) / len(control_rows)
    print()
    if browser_ok >= 0.9 and control_ok >= 0.9:
        verdict = "both paths held up."
    elif browser_ok < 0.9 <= control_ok:
        verdict = "the BROWSER path degraded while the HTTP control kept working."
    elif control_ok < 0.9 <= browser_ok:
        verdict = "the HTTP control degraded while the browser path kept working."
    else:
        verdict = "BOTH paths degraded — suspect Google blocking this IP, not the browser code."
    print(f"  verdict: {verdict} (browser {browser_ok:.0%} ok, control {control_ok:.0%} ok)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.

    """
    parser = argparse.ArgumentParser(
        prog="soak_browser_transport.py",
        description=(
            "Soak the optional browser transport overnight, with a plain-HTTP "
            "control so a morning reader can tell a broken browser path from "
            "Google blocking this IP."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Defaults are deliberately gentle: six requests an hour. Google "
            "gates these RPCs to stop automated use and a flagged IP would "
            "hurt the default HTTP path too."
        ),
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("soak-results.jsonl"),
        help="JSONL results file, appended to (default: %(default)s)",
    )
    parser.add_argument(
        "--summarise",
        action="store_true",
        help="read --results and print a summary instead of running anything",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=30.0,
        help="base gap between cycles (default: %(default)s)",
    )
    parser.add_argument(
        "--max-interval-minutes",
        type=float,
        default=120.0,
        help="cap on the backed-off gap (default: %(default)s)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.25,
        help="fraction of the interval to jitter by, ± (default: %(default)s)",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=2.0,
        help="interval multiplier per consecutive browser failure (default: %(default)s)",
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=20.0,
        help="pause between the checks within one cycle (default: %(default)s)",
    )
    parser.add_argument(
        "--max-hours",
        type=float,
        default=8.0,
        help="stop after this many hours (default: %(default)s)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="stop after this many cycles; 0 means no limit. --max-cycles 1 is the smoke test",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=CHECKS,
        help="run only this check; repeatable",
    )
    parser.add_argument(
        "--cdp-endpoint",
        help="attach to an already-running Chrome (overrides FLI_BROWSER_CDP_ENDPOINT)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        help="per-attempt RPC wait (overrides FLI_BROWSER_TIMEOUT_MS)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="launch with a visible window; ignored when attaching over CDP",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=90,
        help="how far out the first searched date sits (default: %(default)s)",
    )
    parser.add_argument("--currency", default="USD", help="curr param (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the soak, or summarise a previous one.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        A process exit status.

    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.summarise:
        return summarise(args.results)
    return soak(args)


if __name__ == "__main__":
    raise SystemExit(main())
