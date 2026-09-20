"""Turn an intercepted ``GetShoppingResults`` body into flights.

:mod:`fli.search._browser` hands back raw bytes and knows nothing about what
they mean; this module knows what they mean and nothing about browsers. Both
consumers of the browser transport — :class:`fli.search.multi_city.SearchMultiCity`
and :meth:`fli.search.SearchFlights.search` under ``Transport.BROWSER`` — decode
through here, so the frame-merge rule below exists once.

Why a merge rule is needed
--------------------------
An intercepted body is not one board: it is *several progressive re-sends of
one board as it fills in*. Concatenating the frames, which is what the HTTP
path's single inline payload never required, reports the same itinerary many
times over — and at a **stale price**, because later frames revise prices
downward as Google finishes pricing.

Measured on the committed nine-frame capture
(``tests/search/fixtures/flight_search_multi_city.bin``): 83 raw rows across
9 frames collapse to 11 itineraries, with BA203 revised 2235 → 2127, BA213
2346 → 2325 and BA239 3578 → 2976. Keying on legs *and* price yields 14 — the
same itinerary twice at two prices — which is why the key is leg identity
alone and the newest row wins.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fli.models import FlightResult
from fli.search._decoders import flight_rows, parse_flight_row
from fli.search._wire import iter_wrb_chunks
from fli.search.exceptions import BrowserDecodeError

logger = logging.getLogger(__name__)


def itinerary_key(flight: FlightResult) -> tuple:
    """Identify an itinerary by its legs, ignoring price.

    Args:
        flight: A decoded result.

    Returns:
        A hashable identity: one ``(airline, flight_number, departure,
        arrival)`` tuple per leg, in order.

    """
    return tuple(
        (leg.airline, leg.flight_number, leg.departure_datetime, leg.arrival_datetime)
        for leg in flight.legs
    )


def merge_progressive_flights(flights: Iterable[FlightResult]) -> list[FlightResult]:
    """Collapse progressive re-sends of one board, newest price winning.

    Insertion-ordered, so the board keeps the order Google first offered each
    itinerary in rather than the order it happened to finish pricing them.

    Args:
        flights: Every decoded row, in frame order.

    Returns:
        One result per distinct itinerary, carrying its most recent price.

    """
    merged: dict[tuple, FlightResult] = {}
    revised = 0
    for flight in flights:
        key = itinerary_key(flight)
        previous = merged.get(key)
        if previous is not None and previous.price != flight.price:
            revised += 1
        merged[key] = flight
    if revised:
        logger.debug("merged board: %d price revision(s) across frames", revised)
    return list(merged.values())


def decode_shopping_rows(rows: list, *, context: str) -> list[FlightResult]:
    """Decode already-located rows into merged itineraries.

    The rows-level half of :func:`decode_shopping_capture`, split out so the
    inline search-page path (two-leg multi-city, which needs no browser) and
    the intercepted-RPC path share one decoder and one merge rule instead of
    growing two that can drift.

    Args:
        rows: Row nodes from the ``[2]``/``[3]`` slots.
        context: What was searched, for the log line.

    Returns:
        The merged board, cheapest-price-wins duplicates collapsed.

    """
    flights: list[FlightResult] = []
    rejected = 0
    for row in rows:
        try:
            flights.append(parse_flight_row(row))
        except (AttributeError, KeyError, ValueError, TypeError):
            rejected += 1
    merged = merge_progressive_flights(flights)
    logger.debug(
        "%s: %d rows, %d rejected, %d itineraries after merge",
        context,
        len(rows),
        rejected,
        len(merged),
    )
    return merged


def decode_shopping_capture(body: bytes | str, *, context: str) -> list[FlightResult]:
    """Decode an intercepted shopping response into merged itineraries.

    Args:
        body: The raw RPC response body.
        context: What was being searched, for the error messages — e.g.
            ``"multi-city LHR→BOS→CDG→LHR"``. These failures are the ones
            ADR 001 most wants reported rather than swallowed, and a bare
            "shape changed" with no subject is not a report.

    Returns:
        The merged board. Empty when Google found no itineraries, which is
        an answer rather than a failure.

    Raises:
        BrowserDecodeError: The body decoded but carried no rows in the
            known shape, or carried rows that no longer parse.

    """
    chunks = list(iter_wrb_chunks(body))
    rows: list[Any] = []
    shaped = 0
    for chunk in chunks:
        chunk_rows = flight_rows(chunk)
        if chunk_rows is None:
            continue
        shaped += 1
        rows.extend(chunk_rows)

    if shaped == 0:
        raise BrowserDecodeError(
            f"Intercepted the response for {context} but none of its "
            f"{len(chunks)} chunk(s) carried flight rows at the [2]/[3] "
            "positions. The response decoded, so this is a row-shape change "
            "rather than a transport failure — it is ADR 001's reopen "
            "trigger 'the multi-city response does not reuse the one-way row "
            f"shape'. Top-level slot types: {_slot_types(chunks)}."
        )

    flights: list[FlightResult] = []
    rejected = 0
    first_error: Exception | None = None
    for row in rows:
        try:
            flights.append(parse_flight_row(row))
        except (AttributeError, KeyError, ValueError, TypeError) as exc:
            rejected += 1
            if first_error is None:
                first_error = exc
            logger.debug("skipping unparseable intercepted row: %s", exc)

    if rows and not flights:
        raise BrowserDecodeError(
            f"Intercepted the response for {context} and found {len(rows)} row(s) "
            "where they belong, but none of them parsed — the rows' contents "
            f"changed, not their position. First failure: "
            f"{type(first_error).__name__}: {first_error}"
        )

    merged = merge_progressive_flights(flights)
    logger.debug(
        "capture for %s: %d frame(s), %d shaped, %d raw row(s), %d rejected, %d itinerar(ies)",
        context,
        len(chunks),
        shaped,
        len(rows),
        rejected,
        len(merged),
    )
    return merged


def _slot_types(chunks: list[Any]) -> str:
    """Describe what the chunks held instead of rows, for a decode error.

    Args:
        chunks: The decoded chunks.

    Returns:
        A short, bounded description.

    """
    described = []
    for chunk in chunks[:3]:
        if not isinstance(chunk, list):
            described.append(type(chunk).__name__)
            continue
        described.append(
            "[" + ", ".join(type(slot).__name__ for slot in chunk[:5]) + f", …/{len(chunk)}]"
        )
    return "; ".join(described) or "none"
