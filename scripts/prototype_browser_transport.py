r"""Prototype of the browser-backed transport described in ADR 001.

See ``docs/decisions/001-optional-browser-backed-transport.md``. This script is
a throwaway proving ground, **not** a library module: nothing under ``fli/``
imports it, and it adds no dependency to ``pyproject.toml``. Playwright is
imported lazily, inside the browser functions only, so every offline mode here
(``--print-url``, ``--parse-file``) runs on a plain checkout.

What it does, and why in this order:

1. Builds a multi-city Google Flights URL with **fli's own encoder**
   (:func:`fli.search._proto.encode_tfs_segment` for the segments,
   :func:`fli.search._tfs.page_url` for the URL), setting ``tfs`` field 19 to
   ``3``. ``encode_tfs_payload`` only writes ``1``/``2``, so the envelope is
   re-emitted here from fli's own primitives (:func:`_encode_tfs_envelope`)
   rather than hand-rolled — ``test_prototype_browser_transport.py`` pins it
   byte-for-byte against ``encode_tfs_payload`` for the trip types both can
   express.
2. Drives a browser to that URL and **intercepts the response** of the
   in-page ``.../FlightsFrontendService/GetShoppingResults`` call. That RPC is
   the endpoint gated since 2026-08; the page's own JavaScript signs it, so the
   browser produces the attestation as a side effect of being a browser.
3. Feeds the intercepted body through fli's **existing** decode pipeline —
   :func:`fli.search._wire.iter_wrb_chunks` then
   :func:`fli.search._decoders.parse_flight_row` — and emits
   :class:`~fli.models.FlightResult` objects. Nothing is reimplemented.
4. Falls back to reading the rendered DOM **only** when interception or decode
   yields nothing, and says so loudly (a banner on stderr plus
   ``logger.error``). The DOM path returns raw text blocks, not
   ``FlightResult`` — it exists to show the board had content, not to pretend
   it is an equivalent answer.
5. ``--save-fixture`` writes the intercepted body verbatim, so a real
   multi-city ``GetShoppingResults`` response can be committed under
   ``tests/search/fixtures/`` and replayed offline. That is how the central
   open risk in the ADR — *the multi-city response shape is unverified* — gets
   closed.

Usage::

    # Offline: build and print the multi-city URL, no browser, no network.
    uv run python scripts/prototype_browser_transport.py \
        --leg JFK:LHR:2026-11-02 --leg LHR:CDG:2026-11-09 \
        --leg CDG:JFK:2026-11-16 --print-url

    # Offline: run the decode half against a captured response body.
    uv run python scripts/prototype_browser_transport.py \
        --parse-file tests/search/fixtures/flight_search_jfk_lax_oneway_usd.bin

    # Live: launch a browser, intercept, decode, and keep the body.
    uv run --with playwright python scripts/prototype_browser_transport.py \
        --leg JFK:LHR:2026-11-02 --leg LHR:CDG:2026-11-09 --leg CDG:JFK:2026-11-16 \
        --headed --save-fixture /tmp/multi_city_shopping.bin

    # Live, against an already-running Chrome (see below).
    uv run --with playwright python scripts/prototype_browser_transport.py \
        --leg JFK:LHR:2026-11-02 --leg LHR:CDG:2026-11-09 \
        --cdp http://localhost:9222

Driving an already-running Chrome over CDP
------------------------------------------
Attaching to a real Chrome sidesteps two problems at once: a sandbox that
refuses to let the script spawn its own browser, and attestation, which may
behave differently for a fresh automation profile than for a profile that has
browsed before. Start Chrome with a debugging port and a profile directory of
its own (reusing the default profile requires every Chrome window to be closed
first, and is best avoided)::

    # macOS
    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
        --remote-debugging-port=9222 \
        --user-data-dir="$HOME/.cache/fli-chrome-cdp-profile"

    # Linux
    google-chrome --remote-debugging-port=9222 \
        --user-data-dir="$HOME/.cache/fli-chrome-cdp-profile"

Then pass ``--cdp http://localhost:9222``. The script opens a new tab in that
browser, does its work, closes the tab, and leaves the browser running.

Status: the browser half of this script has **never been executed**. The
development machine sandboxes Chromium — ``playwright install chromium``
succeeds but ``chromium.launch()`` dies with ``TargetClosedError`` / ``kill
EPERM``. Only the offline halves (URL building, decode) have been run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fli.models import FlightResult
from fli.search._decoders import parse_flight_row
from fli.search._proto import _to_urlsafe_b64, _varint_field, encode_tfs_segment
from fli.search._tfs import page_url
from fli.search._wire import iter_wrb_chunks

if TYPE_CHECKING:  # pragma: no cover - typing only, playwright is not a dependency
    from playwright.sync_api import Page, Response

logger = logging.getLogger("fli.prototype.browser_transport")

#: Substring identifying the shopping RPC among a page's many responses. This
#: is ``SearchFlights.BASE_URL``'s distinctive tail; matching on a substring
#: keeps the predicate indifferent to query parameters Google appends.
SHOPPING_RPC_MARKER = "FlightsFrontendService/GetShoppingResults"

#: ``tfs`` field 19 values. ``encode_tfs_payload`` writes only the first two.
TRIP_TYPE_ROUND_TRIP = 1
TRIP_TYPE_ONE_WAY = 2
TRIP_TYPE_MULTI_CITY = 3

#: Ceilings for the structural row search, so a pathological payload cannot
#: turn the fallback into an unbounded walk.
_MAX_WALK_NODES = 200_000
_MAX_WALK_DEPTH = 40


# ---------------------------------------------------------------------------
# Request building — fli's own encoder, with field 19 = 3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiCityLeg:
    """One leg of a multi-city itinerary.

    Attributes:
        origin: IATA code the leg departs from.
        destination: IATA code the leg arrives at.
        date: Departure date in ``YYYY-MM-DD`` form.

    """  # noqa: D413 - blank-line-after-last-section false positive on dataclasses

    origin: str
    destination: str
    date: str


def parse_leg(spec: str) -> MultiCityLeg:
    """Parse a ``ORIGIN:DEST:YYYY-MM-DD`` command-line leg specification.

    Args:
        spec: The raw ``--leg`` value.

    Returns:
        The parsed leg.

    Raises:
        argparse.ArgumentTypeError: The spec is not three colon-separated
            parts, or a part is empty.

    """
    parts = spec.split(":")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError(
            f"expected ORIGIN:DEST:YYYY-MM-DD, got {spec!r}",
        )
    origin, destination, date = (part.strip() for part in parts)
    return MultiCityLeg(origin=origin.upper(), destination=destination.upper(), date=date)


def _encode_tfs_envelope(
    segments: bytes,
    *,
    trip_type: int,
    passengers: Sequence[int] = (1,),
    seat: int = 1,
) -> str:
    """Wrap encoded segments in the ``tfs`` envelope with an explicit trip type.

    Byte-for-byte :func:`fli.search._proto.encode_tfs_payload`, except that the
    field 19 value is a parameter rather than a boolean. That function cannot
    express multi-city (3), and this prototype must not fork the encoding — so
    the envelope is re-emitted here from the same primitives, and
    ``tests/scripts/test_prototype_browser_transport.py`` asserts the two agree
    for trip types 1 and 2.

    Args:
        segments: Concatenated output of
            :func:`fli.search._proto.encode_tfs_segment`.
        trip_type: Field 19 value — 1 round-trip, 2 one-way, 3 multi-city.
        passengers: Passenger kind codes, one entry per traveller.
        seat: Cabin class (1 economy, 2 premium, 3 business, 4 first).

    Returns:
        The URL-safe base64 ``tfs`` value, unpadded.

    """
    payload = _varint_field(1, 28) + _varint_field(2, 2) + segments
    for kind in passengers:
        payload += _varint_field(8, kind)
    payload += _varint_field(9, seat) + _varint_field(14, 1)
    payload += _varint_field(19, trip_type)
    return _to_urlsafe_b64(payload)


def build_multi_city_tfs(
    legs: Sequence[MultiCityLeg],
    *,
    seat: int = 1,
    passengers: Sequence[int] = (1,),
    max_stops: int | None = None,
) -> str:
    """Build the ``tfs`` parameter for a multi-city itinerary.

    Each leg becomes one ``tfs`` segment via fli's own
    :func:`fli.search._proto.encode_tfs_segment`; only the envelope's trip type
    differs from what :func:`fli.search._tfs.build_tfs` would emit.

    Args:
        legs: Ordered legs of the itinerary; at least two.
        seat: Cabin class code.
        passengers: Passenger kind codes, one entry per traveller.
        max_stops: Zero-based stop ceiling (0 = non-stop). ``None`` leaves the
            search unconstrained; passing 0 for "any" would pin it to non-stop.

    Returns:
        The URL-safe base64 ``tfs`` value.

    Raises:
        ValueError: Fewer than two legs were supplied.

    """
    if len(legs) < 2:
        raise ValueError("multi-city needs at least two legs")
    segments = b"".join(
        encode_tfs_segment(leg.origin, leg.destination, leg.date, max_stops=max_stops)
        for leg in legs
    )
    return _encode_tfs_envelope(
        segments,
        trip_type=TRIP_TYPE_MULTI_CITY,
        passengers=passengers,
        seat=seat,
    )


def multi_city_url(
    legs: Sequence[MultiCityLeg],
    *,
    seat: int = 1,
    passengers: Sequence[int] = (1,),
    max_stops: int | None = None,
    currency: str | None = None,
    language: str | None = None,
    country: str | None = None,
) -> str:
    """Build the Google Flights search-page URL for a multi-city itinerary.

    Args:
        legs: Ordered legs of the itinerary.
        seat: Cabin class code.
        passengers: Passenger kind codes, one entry per traveller.
        max_stops: Zero-based stop ceiling, or ``None``.
        currency: Google ``curr=`` parameter.
        language: Google ``hl=`` parameter.
        country: Google ``gl=`` parameter.

    Returns:
        A fully formed ``https://www.google.com/travel/flights?...`` URL.

    """
    tfs = build_multi_city_tfs(legs, seat=seat, passengers=passengers, max_stops=max_stops)
    return page_url(tfs, currency, language, country)


# ---------------------------------------------------------------------------
# Decode path — pure, offline, and the half that is actually exercised here
# ---------------------------------------------------------------------------


@dataclass
class ParseReport:
    """Outcome of decoding one ``GetShoppingResults`` body.

    Attributes:
        flights: Successfully decoded itineraries.
        shape: How the rows were located — ``"known"`` (the one-way
            ``chunk[2]``/``chunk[3]`` layout ``parse_flight_row`` was written
            against), ``"structural"`` (rows found by walking the payload
            because the known layout was absent — the case the ADR flags as
            the central open risk for multi-city), or ``"none"``.
        chunks: Number of ``wrb.fr`` chunks seen in the body.
        rows_seen: Candidate rows handed to ``parse_flight_row``.
        rows_failed: Candidate rows it rejected.
        notes: Human-readable diagnostics, worth printing on any surprise.

    """  # noqa: D413 - blank-line-after-last-section false positive on dataclasses

    flights: list[FlightResult] = field(default_factory=list)
    shape: str = "none"
    chunks: int = 0
    rows_seen: int = 0
    rows_failed: int = 0
    notes: list[str] = field(default_factory=list)


def parse_shopping_response(body: str | bytes) -> list[FlightResult]:
    """Decode a captured ``GetShoppingResults`` body into flight results.

    This is the whole point of intercepting the RPC rather than scraping: the
    body goes straight into fli's existing pipeline
    (:func:`~fli.search._wire.iter_wrb_chunks` then
    :func:`~fli.search._decoders.parse_flight_row`) with no new parsing code.

    It takes bytes or text and needs no browser, so it is exercised offline
    against the captured fixtures in ``tests/search/fixtures/``.

    Args:
        body: The raw response body, as bytes from the wire or already-decoded
            text.

    Returns:
        Every row that decoded, in response order.

    """
    return parse_shopping_response_detailed(body).flights


def parse_shopping_response_detailed(body: str | bytes) -> ParseReport:
    """Decode a body and report how the rows were found.

    Same work as :func:`parse_shopping_response`, but keeps the diagnostics —
    which layout the rows came from, how many were rejected — because for a
    multi-city response those are the finding, not incidental logging.

    Args:
        body: The raw response body.

    Returns:
        A :class:`ParseReport`.

    """
    report = ParseReport()

    for chunk in iter_wrb_chunks(body):
        report.chunks += 1
        rows = _rows_known_shape(chunk)
        shape = "known"
        if rows is None:
            rows = _rows_structural(chunk)
            shape = "structural"
            report.notes.append(
                "chunk did not carry rows at the expected [2]/[3] positions; "
                "fell back to walking the payload for row-shaped nodes",
            )
        if not rows:
            continue
        # Sticky, not last-chunk-wins: a later chunk matching the known layout
        # must not erase the fact that an earlier one needed the walk, or the
        # banner never fires and the whole triage procedure reads "known".
        if shape == "structural" or report.shape == "none":
            report.shape = shape

        for row in rows:
            report.rows_seen += 1
            try:
                report.flights.append(parse_flight_row(row))
            except (AttributeError, KeyError, ValueError, TypeError) as exc:
                report.rows_failed += 1
                logger.debug("row rejected by parse_flight_row: %s", exc)

    if report.rows_seen and not report.flights:
        report.notes.append(
            f"every one of {report.rows_seen} candidate rows was rejected by "
            "parse_flight_row — the multi-city row shape probably differs from "
            "the one-way shape the decoder was written against",
        )
    return report


def _rows_known_shape(chunk: Any) -> list | None:
    """Return rows from the one-way layout, or ``None`` if it is not present.

    ``SearchFlights.search`` reads ``inner[2]`` and ``inner[3]``, each a list
    whose first element is the row array. Mirrored exactly so the prototype
    exercises production's own expectations rather than a lookalike.

    Args:
        chunk: One decoded ``wrb.fr`` inner payload.

    Returns:
        The concatenated rows, or ``None`` when neither slot holds them.

    """
    if not isinstance(chunk, list):
        return None
    rows: list = []
    found = False
    for index in (2, 3):
        if index >= len(chunk):
            continue
        slot = chunk[index]
        if isinstance(slot, list) and slot and isinstance(slot[0], list):
            rows.extend(slot[0])
            found = True
    return rows if found else None


def _body_decodes(body: str | bytes | None) -> bool:
    """Return True when *body* yields at least one decodable flight row.

    Used to decide whether the DOM is still worth capturing before the page
    closes: an intercepted body that decodes to nothing is the failure this
    prototype most expects, and the rendered board is the only remaining
    evidence.
    """
    if body is None:
        return False
    try:
        return bool(parse_shopping_response_detailed(body).flights)
    except Exception:  # noqa: BLE001 — diagnostic path, never fatal
        return False


def _looks_like_flight_row(node: Any) -> bool:
    """Heuristically decide whether ``node`` has the shape of a flight row.

    Keyed to what :func:`~fli.search._decoders.parse_flight_row` actually
    reads: ``row[0]`` is a detail list with at least a duration at index 9, and
    ``row[0][2]`` is a non-empty list of legs whose first entry reaches index
    22 (the airline block). Cheap and deliberately loose — anything it lets
    through is still validated by ``parse_flight_row`` itself.

    Args:
        node: A candidate node from the payload walk.

    Returns:
        ``True`` when the node is worth handing to the decoder.

    """
    if not isinstance(node, list) or not node:
        return False
    detail = node[0]
    if not isinstance(detail, list) or len(detail) < 10:
        return False
    legs = detail[2]
    if not isinstance(legs, list) or not legs:
        return False
    first_leg = legs[0]
    return isinstance(first_leg, list) and len(first_leg) > 22


def _rows_structural(chunk: Any) -> list:
    """Find row-shaped nodes anywhere in a payload whose layout is unknown.

    The fallback for the ADR's central open risk: a multi-city response
    plausibly nests a board per leg rather than a flat rows array at
    ``[2]``/``[3]``. Rather than guess the nesting, walk breadth-first and
    collect anything :func:`_looks_like_flight_row` accepts, never descending
    into a node already matched. Bounded by :data:`_MAX_WALK_NODES` and
    :data:`_MAX_WALK_DEPTH`.

    Args:
        chunk: One decoded ``wrb.fr`` inner payload.

    Returns:
        Candidate rows, in discovery order.

    """
    rows: list = []
    visited = 0
    queue: list[tuple[Any, int]] = [(chunk, 0)]
    while queue:
        node, depth = queue.pop(0)
        visited += 1
        if visited > _MAX_WALK_NODES or depth > _MAX_WALK_DEPTH:
            logger.warning("structural row search hit its walk ceiling; results may be partial")
            break
        if _looks_like_flight_row(node):
            rows.append(node)
            # Deliberately NOT `continue`. A *container* of rows satisfies this
            # heuristic too — container[0] is itself a row — so stopping here
            # collected the wrapper and silently dropped every row inside it
            # (measured: 5 of 28, 4 of 127, 13 of 69 on the real fixtures).
            # A wrapper that reaches parse_flight_row is rejected by it, which
            # is the correct arbiter; a row that never reaches it is lost with
            # no signal at all. Descend regardless and let the decoder judge.
        if isinstance(node, list):
            queue.extend((child, depth + 1) for child in node)
        elif isinstance(node, dict):
            queue.extend((child, depth + 1) for child in node.values())
    return rows


# ---------------------------------------------------------------------------
# Browser transport — NEVER EXECUTED on the development machine
# ---------------------------------------------------------------------------


@dataclass
class CaptureResult:
    """What one browser run produced.

    Attributes:
        body: The intercepted ``GetShoppingResults`` response body, or ``None``
            when the call was never seen.
        rpc_urls: Every ``FlightsFrontendUi`` URL the page requested, for
            diagnosing a miss (was ``browserinfo`` called? did the shopping RPC
            fire at all?).
        dom_blocks: Raw text of the rendered result rows, populated only when
            the DOM fallback ran.

    """  # noqa: D413 - blank-line-after-last-section false positive on dataclasses

    body: bytes | None = None
    rpc_urls: list[str] = field(default_factory=list)
    dom_blocks: list[str] = field(default_factory=list)


@dataclass
class BrowserOptions:
    """How to obtain a browser and how long to wait for the RPC.

    Attributes:
        cdp_endpoint: Attach to an already-running Chrome at this endpoint
            (e.g. ``http://localhost:9222``) instead of launching one.
        headless: Launch without a visible window. Whether headless Chrome
            clears Google's attestation as reliably as headed Chrome is
            unverified — see the ADR's open risks.
        user_data_dir: Launch a persistent context rooted here, so cookies and
            history survive between runs.
        timeout_ms: How long to wait for the shopping RPC.
        dom_fallback: Read the rendered DOM when interception or decode comes
            up empty.

    """  # noqa: D413 - blank-line-after-last-section false positive on dataclasses

    cdp_endpoint: str | None = None
    headless: bool = True
    user_data_dir: str | None = None
    timeout_ms: int = 45_000
    dom_fallback: bool = True


def _is_shopping_response(response: Response) -> bool:
    """Return whether a response is the shopping RPC we want to intercept."""
    return SHOPPING_RPC_MARKER in response.url


def capture_via_browser(url: str, options: BrowserOptions) -> CaptureResult:
    """Drive a browser to ``url`` and intercept the shopping RPC response.

    Interception uses ``page.expect_response`` rather than a bare
    ``page.on("response")`` handler: Playwright's sync API forbids reading a
    body from inside an event handler, and ``expect_response`` hands back a
    response object whose body can be read once the wait returns. A passive
    ``page.on("response")`` listener still runs alongside it, recording URLs
    only, so a miss can be diagnosed.

    Google sometimes serves the board without re-issuing the RPC (a cached
    result). When the first wait times out, the page is nudged by clicking its
    search control and the wait is retried once.

    Args:
        url: The multi-city search-page URL.
        options: Browser and timeout settings.

    Returns:
        A :class:`CaptureResult`.

    Raises:
        RuntimeError: Playwright is not installed. Run the script under
            ``uv run --with playwright`` — it is deliberately not a project
            dependency.

    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "playwright is not installed. This prototype deliberately does not "
            "add it to pyproject.toml; run the script with "
            "`uv run --with playwright python scripts/prototype_browser_transport.py ...` "
            "and `uv run --with playwright playwright install chromium` once.",
        ) from exc

    result = CaptureResult()

    with sync_playwright() as playwright:
        browser = None
        context = None
        owns_context = True
        if options.cdp_endpoint:
            browser = playwright.chromium.connect_over_cdp(options.cdp_endpoint)
            if browser.contexts:
                context = browser.contexts[0]
                owns_context = False
            else:
                context = browser.new_context()
        elif options.user_data_dir:
            context = playwright.chromium.launch_persistent_context(
                options.user_data_dir,
                headless=options.headless,
            )
        else:
            browser = playwright.chromium.launch(headless=options.headless)
            context = browser.new_context()

        page = context.new_page()
        page.on("response", lambda response: _record_rpc_url(result, response))
        try:
            result.body = _await_shopping_body(
                page,
                url,
                options.timeout_ms,
                PlaywrightTimeoutError,
            )
            # Also capture the DOM when a body came back but decodes to
            # nothing — that is the ADR's central risk landing badly, and the
            # page is still showing a full board. Gathering it only on
            # `body is None` meant the most likely real failure closed the
            # page with the evidence still on screen.
            if options.dom_fallback and not _body_decodes(result.body):
                result.dom_blocks = extract_dom_blocks(page)
        finally:
            page.close()
            # Over CDP the browser and its default context belong to the user;
            # closing either would take their Chrome down with it.
            if owns_context and context is not None and not options.cdp_endpoint:
                context.close()
            if browser is not None and not options.cdp_endpoint:
                browser.close()
    return result


def _record_rpc_url(result: CaptureResult, response: Response) -> None:
    """Record a FlightsFrontendUi URL for diagnostics; never read a body here."""
    if "FlightsFrontendUi" in response.url:
        result.rpc_urls.append(response.url)


def _await_shopping_body(
    page: Page,
    url: str,
    timeout_ms: int,
    timeout_error: type[Exception],
) -> bytes | None:
    """Navigate, then wait for the shopping RPC, nudging the page once on miss.

    Args:
        page: The page to drive.
        url: Where to navigate.
        timeout_ms: Per-wait timeout.
        timeout_error: Playwright's ``TimeoutError``, passed in so this module
            never imports playwright at module scope.

    Returns:
        The intercepted body, or ``None`` if the RPC never fired.

    """
    try:
        with page.expect_response(_is_shopping_response, timeout=timeout_ms) as info:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return info.value.body()
    except timeout_error:
        logger.warning(
            "shopping RPC not seen during page load; nudging the search control and retrying",
        )

    try:
        with page.expect_response(_is_shopping_response, timeout=timeout_ms) as info:
            page.get_by_role("button", name="Search").first.click(timeout=timeout_ms)
        return info.value.body()
    except Exception as exc:  # noqa: BLE001 - a nudge failing is a diagnosis, not a crash
        logger.warning("nudge did not produce a shopping RPC either: %s", exc)
        return None


def extract_dom_blocks(page: Page) -> list[str]:
    """Read the rendered result rows as raw text — the loud, degraded fallback.

    This returns text blocks, **not** ``FlightResult`` objects, and that is
    deliberate. The ADR keeps DOM extraction as a fallback precisely because it
    discards fli's decoders; producing model objects from scraped markup here
    would paper over the difference. What this proves is only that the board
    rendered and carried content while interception found none.

    Args:
        page: The page showing a rendered results board.

    Returns:
        The inner text of each candidate result row.

    """
    blocks: list[str] = []
    for selector in ('li[role="listitem"]', "ul li"):
        try:
            elements = page.query_selector_all(selector)
        except Exception as exc:  # noqa: BLE001 - selector support varies with markup
            logger.warning("DOM selector %r failed: %s", selector, exc)
            continue
        for element in elements:
            text = (element.inner_text() or "").strip()
            if len(text) > 40 and any(char.isdigit() for char in text):
                blocks.append(text)
        if blocks:
            break
    return blocks


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _shout(message: str) -> None:
    """Print a banner to stderr and log it at error level."""
    bar = "!" * 78
    sys.stdout.flush()
    print(f"\n{bar}\n!! {message}\n{bar}\n", file=sys.stderr)
    logger.error("%s", message)


def _describe(flight: FlightResult) -> str:
    """Render one decoded itinerary as a single readable line."""
    route = " > ".join(
        f"{leg.departure_airport.name}-{leg.arrival_airport.name}"
        f" {leg.airline.name.removeprefix('_')}{leg.flight_number}"
        for leg in flight.legs
    )
    price = "price unknown" if flight.price is None else f"{flight.price:.0f} {flight.currency}"
    return f"{price:>18}  {flight.duration:>5}m  {flight.stops} stop(s)  {route}"


def _print_report(report: ParseReport, limit: int) -> None:
    """Print a decode report and a sample of the itineraries it produced."""
    print(
        f"decoded: chunks={report.chunks} rows={report.rows_seen} "
        f"failed={report.rows_failed} flights={len(report.flights)} shape={report.shape}",
    )
    for note in report.notes:
        print(f"  note: {note}")
    for flight in report.flights[:limit]:
        print(f"  {_describe(flight)}")
    if len(report.flights) > limit:
        print(f"  ... and {len(report.flights) - limit} more")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="prototype_browser_transport.py",
        description=(
            "Prototype browser-backed transport for multi-city Google Flights "
            "search (ADR 001). Intercepts the GetShoppingResults RPC and decodes "
            "it with fli's existing pipeline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Offline modes (--print-url, --parse-file) need no browser. The live "
            "modes need `uv run --with playwright`; playwright is deliberately not "
            "a project dependency."
        ),
    )
    parser.add_argument(
        "--leg",
        dest="legs",
        action="append",
        type=parse_leg,
        metavar="ORIGIN:DEST:YYYY-MM-DD",
        help="One itinerary leg; repeat at least twice for multi-city.",
    )
    parser.add_argument("--url", help="Use this URL verbatim instead of building one from --leg.")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="Build and print the multi-city URL, then exit. No browser, no network.",
    )
    parser.add_argument(
        "--parse-file",
        type=Path,
        metavar="PATH",
        help=(
            "Decode a previously captured GetShoppingResults body and exit. "
            "Exercises the whole decode half offline."
        ),
    )
    parser.add_argument(
        "--save-fixture",
        type=Path,
        metavar="PATH",
        help="Write the intercepted response body here, verbatim, for use as a test fixture.",
    )
    parser.add_argument(
        "--cdp",
        metavar="ENDPOINT",
        help=(
            "Attach to an already-running Chrome (e.g. http://localhost:9222) "
            "instead of launching one. Start Chrome with --remote-debugging-port."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window. Headless attestation is unverified.",
    )
    parser.add_argument(
        "--user-data-dir",
        metavar="DIR",
        help="Launch a persistent browser profile rooted here.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45_000,
        metavar="MS",
        help="How long to wait for the shopping RPC (default: 45000).",
    )
    parser.add_argument("--seat", type=int, default=1, help="Cabin class code (default: 1).")
    parser.add_argument("--adults", type=int, default=1, help="Adult travellers (default: 1).")
    parser.add_argument("--currency", help="Google curr= parameter.")
    parser.add_argument("--language", help="Google hl= parameter.")
    parser.add_argument("--country", help="Google gl= parameter.")
    parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="How many decoded itineraries to print (default: 10).",
    )
    parser.add_argument(
        "--no-dom-fallback",
        action="store_true",
        help="Do not read the DOM when interception or decode comes up empty.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser


def _resolve_url(args: argparse.Namespace) -> str:
    """Return the URL to drive the browser to.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The explicit ``--url``, or one built from ``--leg``.

    Raises:
        SystemExit: Neither ``--url`` nor at least two ``--leg`` values given.

    """
    if args.url:
        return args.url
    legs = args.legs or []
    if len(legs) < 2:
        raise SystemExit("error: pass --url, or at least two --leg ORIGIN:DEST:YYYY-MM-DD values")
    return multi_city_url(
        legs,
        seat=args.seat,
        passengers=[1] * max(args.adults, 1),
        currency=args.currency,
        language=args.language,
        country=args.country,
    )


def _run_offline_parse(path: Path, limit: int) -> int:
    """Decode a captured body from disk and print the report."""
    body = path.read_bytes()
    print(f"parsing {path} ({len(body)} bytes)")
    report = parse_shopping_response_detailed(body)
    _print_report(report, limit)
    if report.shape == "structural":
        _shout(
            "rows were NOT at the expected [2]/[3] positions — this body's shape "
            "differs from the one-way shape parse_flight_row was written against",
        )
    return 0 if report.flights else 1


def _run_browser(args: argparse.Namespace, url: str) -> int:
    """Drive the browser, intercept, decode, and report."""
    print(f"url: {url}")
    options = BrowserOptions(
        cdp_endpoint=args.cdp,
        headless=not args.headed,
        user_data_dir=args.user_data_dir,
        timeout_ms=args.timeout,
        dom_fallback=not args.no_dom_fallback,
    )
    capture = capture_via_browser(url, options)

    if capture.rpc_urls:
        print(f"FlightsFrontendUi calls seen: {len(capture.rpc_urls)}")
        for seen in capture.rpc_urls[:10]:
            print(f"  {seen.split('?', 1)[0]}")

    if capture.body is None:
        _shout("INTERCEPTION FAILED — the GetShoppingResults response was never captured")
        _report_dom(capture)
        return 2

    print(f"intercepted {len(capture.body)} bytes from GetShoppingResults")
    if args.save_fixture:
        args.save_fixture.parent.mkdir(parents=True, exist_ok=True)
        args.save_fixture.write_bytes(capture.body)
        print(f"saved fixture: {args.save_fixture}")

    report = parse_shopping_response_detailed(capture.body)
    _print_report(report, args.max_results)

    if not report.flights:
        _shout(
            "DECODE FAILED — the intercepted body produced no FlightResult. "
            "The multi-city response shape differs from the one-way shape; "
            "re-run with --save-fixture and add the body to tests/search/fixtures/.",
        )
        _report_dom(capture)
        return 3
    if report.shape == "structural":
        _shout(
            "rows were found by structural search, not at [2]/[3] — the decoders "
            "work but the response layout differs from one-way. Worth a fixture.",
        )
    return 0


def _report_dom(capture: CaptureResult) -> None:
    """Print whatever the DOM fallback found, clearly labelled as degraded."""
    if not capture.dom_blocks:
        print("DOM fallback found nothing either.", file=sys.stderr)
        return
    _shout(
        f"FALLING BACK TO DOM EXTRACTION — {len(capture.dom_blocks)} raw text "
        "blocks, NOT FlightResult objects. This is the degraded path.",
    )
    for block in capture.dom_blocks[:5]:
        print(f"  --- {' / '.join(block.splitlines())[:200]}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the prototype.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, non-zero when interception or decode failed.

    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.parse_file:
        return _run_offline_parse(args.parse_file, args.max_results)

    url = _resolve_url(args)
    if args.print_url:
        print(url)
        return 0
    return _run_browser(args, url)


if __name__ == "__main__":
    raise SystemExit(main())
