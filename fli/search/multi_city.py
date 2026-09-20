"""Multi-city search, over the optional browser transport.

Multi-city is the one search class the HTTP transport cannot serve at all.
Google renders the board client-side from ``GetShoppingResults``, which is
gated behind the ``x-goog-batchexecute-bgr`` header only its own JavaScript
can produce, and the page inlines nothing to read instead — re-verified
2026-09-20 on a three-leg and on an open-jaw two-leg itinerary, both of which
return a ``ds:1`` payload with no rows at ``[2]``/``[3]``. (A two-leg
``A→B``/``B→A`` "multi-city" *does* inline rows, because Google normalises it
into a round trip. That is not the multi-city case and must not be mistaken
for evidence that this one works over HTTP.)

So this module loads the page in a real browser and intercepts the response
the page's own JavaScript receives. Verified live the same day: a three-leg
``LHR→BOS→CDG→LHR`` search produced 5 frames, 62 raw rows and 15 itineraries,
with no nudge needed.

What comes back is a **first-leg board**, not itineraries — see
:class:`fli.models.MultiCityBoard` for why that distinction gets its own type.
"""

from __future__ import annotations

import logging

from fli.models import FlightResult, FlightSearchFilters, MultiCityBoard
from fli.models.google_flights.base import TripType
from fli.search._browser import BrowserOptions, capture_rpc_body
from fli.search._capture import decode_shopping_capture, decode_shopping_rows
from fli.search._decoders import flight_rows
from fli.search._tfs import (
    apply_client_side_filters,
    build_multi_city_tfs,
    extract_payload,
    multi_city_url,
    page_url,
    passenger_codes,
    unsupported_filters,
)
from fli.search.client import get_client
from fli.search.exceptions import BrowserAttestationRejectedError, SearchRejectedError

logger = logging.getLogger(__name__)

#: The response the multi-city page fetches its board from.
SHOPPING_RPC_MARKER = "FlightsFrontendService/GetShoppingResults"


class SearchMultiCity:
    """Multi-city search: one browser page load, one intercepted response.

    Concurrency:
        **Not thread-safe, and never to be called from inside**
        :func:`fli.search._concurrency.parallel_map`. One capture at a time.
        This transport does not draw on the HTTP client's 10 req/sec token
        bucket, and it must not be used to route around it either: it simply
        is not that kind of client, and the endpoint it reaches is gated
        precisely to stop automated volume.
    """

    def __init__(self, options: BrowserOptions | None = None) -> None:
        """Prepare a multi-city searcher.

        Args:
            options: Browser settings. Defaults to
                :meth:`BrowserOptions.from_env`, resolved at search time so
                a process that sets ``FLI_BROWSER_*`` after constructing the
                object still gets what it set.

        """
        self._options = options

    def search(
        self,
        filters: FlightSearchFilters,
        *,
        currency: str | None = None,
        language: str | None = None,
        country: str | None = None,
    ) -> MultiCityBoard | None:
        """Fetch the first-leg board for a multi-city itinerary.

        Args:
            filters: A search with ``trip_type`` MULTI_CITY and at least two
                segments.
            currency: Optional ISO 4217 currency code (``curr`` URL param).
            language: Optional BCP-47 language code (``hl`` URL param).
            country: Optional ISO 3166-1 alpha-2 country code (``gl``).

        Returns:
            The board, cheapest first, or ``None`` when Google found no
            itineraries at all.

        Raises:
            ValueError: *filters* is not a multi-city search.
            BrowserTransportUnavailableError: The ``browser`` extra, or its
                Chromium binary, is not installed.
            BrowserUnreachableError: No browser could be attached or launched.
            BrowserConsentRequiredError: Consent blocked the page.
            BrowserRpcTimeoutError: The page never issued the RPC.
            BrowserAttestationRejectedError: Google declined the request even
                though it came from a real browser.
            BrowserDecodeError: The response decoded but carried no rows in
                the known shape.

        """
        if filters.trip_type != TripType.MULTI_CITY:
            raise ValueError(
                f"SearchMultiCity serves multi-city searches; got {filters.trip_type!r}. "
                "Use SearchFlights for one-way and round-trip."
            )

        legs = self._legs(filters)
        url = page_url(build_multi_city_tfs(filters), currency, language, country)
        context = "multi-city " + "→".join([legs[0][0]] + [leg[1] for leg in legs])

        dropped = unsupported_filters(filters)
        if dropped:
            logger.warning(
                "Filters not supported by the multi-city transport, ignored: %s",
                ", ".join(dropped),
            )

        # Try the search page first. It costs one cheap GET and occasionally
        # answers outright, which saves a page load, a browser and any
        # attestation risk.
        #
        # What comes back inline is narrower than leg count suggests, and the
        # distinction is easy to get wrong. Measured:
        #
        #   LHR→BOS, BOS→LHR   payload 27, 13 rows   (out and back: served)
        #   LHR→BOS, JFK→LHR   payload 24, no rows   (open jaw: not served)
        #   LHR→BOS, BOS→SFO   payload 24, no rows   (onward: not served)
        #
        # So the rule is not "two legs". It is "Google recognises this as a
        # round trip". A genuine open jaw — different return origin, the case
        # people most often mean by multi-city — is NOT served inline and does
        # need the browser. Hence the attempt rather than a leg-count branch.
        flights = self._search_http(url, context=context)
        if flights is not None:
            logger.debug("%s came back inline; no browser needed", context)
            return self._board(flights, filters, legs, currency, language, country)
        logger.debug("no inline rows for %s; using the browser transport", context)

        options = self._options or BrowserOptions.from_env()
        capture = capture_rpc_body(url, rpc_marker=SHOPPING_RPC_MARKER, options=options)
        if capture.nudged:
            logger.debug("board for %s arrived only after nudging the page", context)

        try:
            flights = decode_shopping_capture(capture.body, context=context)
        except SearchRejectedError as exc:
            # ``iter_wrb_chunks`` raises this on a payload-less ``wrb.fr`` row
            # carrying error 13. Reaching it here means the *browser* was
            # declined, which is a different diagnosis from the known bgr gate
            # the HTTP path hits — and deliberately not retried: re-firing a
            # request the service just refused is the behaviour the gate
            # exists to punish.
            raise BrowserAttestationRejectedError(
                f"Google declined the request for {context} even though it came "
                "from a real browser, so this is bot detection or a flagged "
                "profile rather than the known bgr gate. Do not retry "
                "immediately. Delete the fli browser profile "
                f"({options.profile_dir}) to get a fresh signed-out one, and if "
                "you are running headless try FLI_BROWSER_HEADLESS=0 — headless "
                f"attestation is unverified. ({exc})"
            ) from exc

        return self._board(flights, filters, legs, currency, language, country)

    def _search_http(self, url: str, *, context: str) -> list[FlightResult] | None:
        """Read an inline board from the search page, or None when it has none.

        Args:
            url: The multi-city search-page URL.
            context: What is being searched, for diagnostics.

        Returns:
            The decoded board, or None when the page carried no rows — which
            is how a 3+ leg itinerary presents and means "ask the browser",
            not "no flights".

        """
        html = get_client().get(url=url, impersonate="chrome").text
        payload = extract_payload(html)
        rows = flight_rows(payload)
        if rows is None:
            return None
        return decode_shopping_rows(rows, context=context)

    def _board(
        self,
        flights: list[FlightResult],
        filters: FlightSearchFilters,
        legs: list[tuple[str, str, str]],
        currency: str | None,
        language: str | None,
        country: str | None,
    ) -> MultiCityBoard | None:
        """Filter, sort and wrap a decoded board.

        The client-side filters run here for the same reason they run on the
        HTTP flights path: several filters have no tfs field, so without this
        step a caller who passed ``--airlines`` or a departure window would
        get a board that quietly ignores them. That is the defect class this
        project already fixed once in ``fli dates``.
        """
        flights = apply_client_side_filters(flights, filters)
        if not flights:
            return None

        flights.sort(key=lambda f: (f.price is None, f.price))
        return MultiCityBoard(
            results=flights,
            board_leg_index=0,
            legs=legs,
            booking_url=multi_city_url(
                legs,
                currency=currency,
                language=language,
                country=country,
                carriers=[_code(a) for a in (filters.airlines or [])],
                passengers=passenger_codes(filters.passenger_info) or [1],
                seat=filters.seat_type.value,
                max_stops=filters.stops.value,
            ),
        )

    @staticmethod
    def _legs(filters: FlightSearchFilters) -> list[tuple[str, str, str]]:
        """Describe the requested itinerary as plain ``(from, to, date)`` triples.

        Args:
            filters: The multi-city search.

        Returns:
            One triple per segment, IATA codes and ``YYYY-MM-DD``.

        """
        return [
            (
                _code(segment.departure_airport[0][0]),
                _code(segment.arrival_airport[0][0]),
                segment.travel_date,
            )
            for segment in filters.flight_segments
        ]


def _code(value: object) -> str:
    """Return the bare IATA code for an ``Airport``/``Airline`` enum or string.

    Args:
        value: The enum member or code.

    Returns:
        The code without the leading underscore enum members carry.

    """
    return str(getattr(value, "name", value)).removeprefix("_")


def board_total(board: MultiCityBoard) -> float | None:
    """Return the cheapest entire-trip price on a board, if any is priced.

    A convenience for callers that would otherwise reach for ``min(...)`` and
    have to remember what the price means.

    Args:
        board: A fetched board.

    Returns:
        The lowest price, or ``None`` when no option carries one.

    """
    priced = [result.price for result in board.results if result.price is not None]
    return min(priced) if priced else None


__all__ = ["SearchMultiCity", "MultiCityBoard", "FlightResult", "board_total"]
