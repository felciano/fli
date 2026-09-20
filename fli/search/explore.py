"""Google Flights Explore search implementation.

Explore answers "where can I fly cheaply?" — one request returns dozens of
destinations (each with its cheapest found fare) for an origin and a broad
destination such as :attr:`fli.models.ExploreRegion.ANYWHERE` or a continent.

Transport
---------
The ``GetExploreDestinations`` RPC has been gated behind
``x-goog-batchexecute-bgr`` since 2026-08, so the direct POST below answers
HTTP 200 with a payload-less ``wrb.fr`` row carrying error 13 — every time,
for everyone. It is kept, and kept reachable with ``Transport.HTTP``, because
it is the only way to find out if that ever changes; it is not the default,
because firing a request you know will be refused is exactly the traffic a
gate exists to stop.

``Transport.AUTO`` therefore routes Explore through the optional browser
transport, which loads ``/travel/explore?tfs=…`` and intercepts the response
the page's own JavaScript receives. Verified live 2026-09-20: ``LHR`` on
``2026-10-16`` produced 3 chunks, 66 destinations and 52 fares, decoded by
the parsers below **unchanged** — this is a transport fix, not a decoder one.
The URL builder's verified scope (airport origin, "anywhere" destination) is
documented on :func:`fli.search._tfs.explore_page_url`.

Request recipe for the HTTP path (probed live via ``scripts/probe_explore.py``, 2026-08):
unlike the sibling endpoints, ``GetExploreDestinations`` enforces a
same-origin check — at least one of ``x-same-domain`` / ``origin`` /
``referer`` must be present or every request fails with an opaque ``wrb.fr``
error ``[13]``. Nothing else from the browser's ceremony is needed: no
cookies, no ``at`` XSRF token, no ``f.sid``/``bl``/``soc-*``/``rt=c`` query
params, and the ``curr=``/``hl=``/``gl=`` locale params work as on every
other endpoint. Escalation ladder should this break in future:

1. current shape (``x-same-domain: 1`` + ``origin`` headers)
2. add ``referer: https://www.google.com/travel/explore``
3. add ``soc-app=162&soc-platform=1&soc-device=1&rt=c`` query params
4. currency via ``x-goog-ext-259736195-jspb: [hl, gl, curr, 1, null,
   [tz_minutes], null, null, 1, []]`` if ``curr=`` stops working
5. priming ``GET /travel/explore`` to harvest cookies + the ``SNlM0e``
   (``at``) token from ``WIZ_global_data``
"""

import logging
from typing import Any

from fli.models import ExploreResult, ExploreSearchFilters
from fli.search._decoders import (
    is_explore_destinations_chunk,
    is_explore_prices_chunk,
    merge_explore_payloads,
    parse_explore_destinations_chunk,
    parse_explore_prices_chunk,
)
from fli.search._tfs import apply_explore_filters, explore_page_url, unsupported_filters
from fli.search._urls import with_locale_params
from fli.search._wire import iter_wrb_chunks
from fli.search.client import get_client
from fli.search.exceptions import BrowserTransportUnavailableError
from fli.search.transport import Transport

logger = logging.getLogger(__name__)

#: The response the Explore page fills its board from.
EXPLORE_RPC_MARKER = "FlightsFrontendService/GetExploreDestinations"


class SearchExplore:
    """Explore search: one origin, a broad destination, many priced results."""

    BASE_URL = "https://www.google.com/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetExploreDestinations"
    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        # Same-origin signals — required by this endpoint (see module docstring).
        "x-same-domain": "1",
        "origin": "https://www.google.com",
    }

    def __init__(self, options: Any | None = None):
        """Initialize the search client for explore searches.

        Args:
            options: Optional :class:`fli.search.BrowserOptions` for the
                browser transport. Defaults to
                :meth:`BrowserOptions.from_env` at search time.

        """
        self.client = get_client()
        self._browser_options = options

    def search(
        self,
        filters: ExploreSearchFilters,
        currency: str | None = None,
        language: str | None = None,
        country: str | None = None,
        transport: Transport = Transport.AUTO,
    ) -> ExploreResult | None:
        """Search destinations and prices for an Explore query.

        Args:
            filters: Explore search parameters (origin, destination region, date, ...)
            currency: Optional ISO 4217 currency code passed via the ``curr`` URL param.
            language: Optional BCP-47 language code passed via the ``hl`` URL param.
            country: Optional ISO 3166-1 alpha-2 country code passed via the ``gl`` URL param.
            transport: ``AUTO`` (the default) and ``BROWSER`` load the Explore
                page in a browser and intercept its RPC — the only route that
                currently returns results. ``HTTP`` POSTs the endpoint
                directly, which the gate has refused since 2026-08; it is
                kept so the gate can be re-tested, not because it works.

        Returns:
            An :class:`ExploreResult` with one entry per destination (price fields
            are None for destinations Google returned without a fare), or None if
            the response could not be parsed.

        Raises:
            BrowserTransportUnavailableError: The browser transport is needed
                and not installed.
            SearchUnsupportedError: The filters ask for something the Explore
                page URL cannot express — see
                :func:`fli.search._tfs.explore_page_url`.
            SearchRejectedError: Google declined the request.

        """
        dropped = unsupported_filters(filters)
        if dropped:
            # `bags` is the one that reaches here: it changes the fares
            # Google quotes rather than which destinations match, so no
            # post-hoc pass can reproduce it. Named out loud for the same
            # reason the flights path names its own -- a filter that is
            # silently ignored is worse than one that is refused, and this
            # path said nothing at all until now.
            logger.warning(
                "Filters not supported by the Explore transport, ignored: %s",
                ", ".join(dropped),
            )

        if transport is Transport.HTTP:
            body = self._fetch_over_http(filters, currency, language, country)
        else:
            body = self._fetch_over_browser(filters, currency, language, country)

        # Destination and price records stream across MANY wrb.fr chunks in
        # no guaranteed order (24 chunks observed for large regions), so
        # classify every chunk by shape and accumulate before joining.
        meta: dict = {}
        destinations: list = []
        prices: dict = {}
        for chunk in iter_wrb_chunks(body):
            if is_explore_destinations_chunk(chunk):
                chunk_meta, chunk_destinations = parse_explore_destinations_chunk(chunk)
                for key, value in chunk_meta.items():
                    if meta.get(key) is None:
                        meta[key] = value
                destinations.extend(chunk_destinations)
            if is_explore_prices_chunk(chunk):
                prices.update(parse_explore_prices_chunk(chunk, default_currency=currency))

        if not destinations:
            logger.warning("Explore search returned no parseable destination chunks")
            return None

        result = merge_explore_payloads(meta, destinations, prices)
        # price_limit and max_duration have no field in the page URL, but the
        # decoded cards carry both numbers, so they are honoured here rather
        # than dropped. Applied after the merge because an unpriced card only
        # gets its fare from the prices payload.
        result.destinations = apply_explore_filters(result.destinations, filters)
        return result

    def _fetch_over_http(
        self,
        filters: ExploreSearchFilters,
        currency: str | None,
        language: str | None,
        country: str | None,
    ) -> str:
        """POST the Explore endpoint directly. Gated since 2026-08.

        Args:
            filters: The Explore search.
            currency: ``curr`` URL param.
            language: ``hl`` URL param.
            country: ``gl`` URL param.

        Returns:
            The raw response text.

        """
        url = with_locale_params(self.BASE_URL, currency, language, country)
        response = self.client.post(
            url=url,
            data=f"f.req={filters.encode()}",
            impersonate="chrome",
            allow_redirects=True,
            headers=self.DEFAULT_HEADERS,
        )
        response.raise_for_status()
        return response.text

    def _fetch_over_browser(
        self,
        filters: ExploreSearchFilters,
        currency: str | None,
        language: str | None,
        country: str | None,
    ) -> bytes:
        """Load the Explore page in a browser and intercept its RPC response.

        Args:
            filters: The Explore search.
            currency: ``curr`` URL param.
            language: ``hl`` URL param.
            country: ``gl`` URL param.

        Returns:
            The raw intercepted body.

        Raises:
            BrowserTransportUnavailableError: The extra is not installed.

        """
        from fli.search._browser import (
            INSTALL_HINT,
            BrowserOptions,
            browser_available,
            capture_rpc_body,
        )

        if not browser_available():
            raise BrowserTransportUnavailableError(
                "Explore is served only over the optional browser transport: "
                "its endpoint has been gated since 2026-08 and answers a "
                "direct request with error 13. To enable it, "
                f"{INSTALL_HINT}. Passing transport=Transport.HTTP re-tests "
                "the gate, but expect it to fail."
            )

        url = explore_page_url(filters, currency, language, country)
        options = self._browser_options or BrowserOptions.from_env()
        capture = capture_rpc_body(url, rpc_marker=EXPLORE_RPC_MARKER, options=options)
        return capture.body
