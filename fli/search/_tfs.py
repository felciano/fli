"""Search transport built on Google Flights' public ``/travel/flights`` page.

Since 2026-08 the ``FlightsFrontendService`` RPC endpoints
(``GetShoppingResults``, ``GetCalendarGraph``) require an
``x-goog-batchexecute-bgr`` header that only the page's own JavaScript can
produce. The signature is bound to the exact request bytes, so a captured
token cannot be replayed against a different body — every plain HTTP client
gets HTTP 200 with a payload-less ``wrb.fr`` row carrying error 13.

The public search page is not gated that way. It serves the same result
payload inline, in an ``AF_initDataCallback`` blob keyed ``ds:1``, whose
elements ``[2]`` and ``[3]`` hold exactly the flight rows the RPC used to
return — so :mod:`fli.search._decoders` keeps working untouched. The page is
addressed by a ``tfs`` protobuf parameter instead of the ``f.req`` JSON
struct, which is what this module builds.

Scope note: ``tfs`` carries trip type, segments, stop limit, cabin,
passengers, airline include/exclude, alliances and layover restrictions.
Filters with no known ``tfs`` field (price cap, duration, departure window)
are applied to the decoded results instead — see
:func:`apply_client_side_filters`. Anything that can be
neither encoded nor filtered after the fact is reported by
:func:`unsupported_filters` so the caller can warn rather than silently
return results that ignore it.

Multi-city is out of reach *over HTTP* entirely: the page renders the right
board but inlines no rows for it — verified again 2026-09-20, on a three-leg
and on an open-jaw two-leg itinerary, both of which return a ``ds:1`` payload
with nothing at ``[2]``/``[3]``. (A two-leg A→B/B→A "multi-city" does inline
rows, because Google normalises it to a round trip; that is not the multi-city
case.) So :func:`build_tfs` still refuses those searches rather than returning
the first leg's one-way board, and :func:`build_multi_city_tfs` builds the URL
that the browser transport loads instead — see :mod:`fli.search.multi_city`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fli.models.google_flights.base import Alliance, TripType
from fli.search._proto import (
    LegSpec,
    TfsTripType,
    encode_tfs_payload,
    encode_tfs_segment,
)
from fli.search.exceptions import SearchUnsupportedError

if TYPE_CHECKING:
    from fli.models import FlightResult

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.google.com/travel/flights"

# ``AF_initDataCallback({key: 'ds:1', hash: '..', data:[...], sideChannel: {}});``
_DS_BLOB = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_DS_KEY = re.compile(r"key:\s*'([^']+)'")
_DS_DATA = re.compile(r"data:(.*?), sideChannel", re.S)

PAGE_EXPLORE_URL = "https://www.google.com/travel/explore"

# Passenger kinds, in the order Google's repeated field 8 expects them.
_PASSENGER_FIELDS = ("adults", "children", "infants_in_seat", "infants_on_lap")


def passenger_codes(passenger_info: Any) -> list[int]:
    """Expand a :class:`PassengerInfo` into Google's repeated field 8 codes.

    Google encodes party composition as one field 8 entry per traveller,
    carrying that traveller's kind (1 = adult, 2 = child, 3 = infant in
    seat, 4 = infant on lap) rather than a count per kind. Two adults and a
    child are therefore ``[1, 1, 2]``.

    Shared by the search transport (:func:`build_tfs`) and the booking
    deep-link builder so a link always describes the party that was priced.

    Args:
        passenger_info: A :class:`PassengerInfo`, or None.

    Returns:
        Kind codes in field order. Empty when *passenger_info* is None or
        every count is zero — callers substitute a single adult.

    """
    return [
        code
        for kind, code in zip(_PASSENGER_FIELDS, (1, 2, 3, 4), strict=False)
        for _ in range(getattr(passenger_info, kind, 0) or 0)
    ]


# Filters with no ``tfs`` encoding and no reliable post-hoc equivalent —
# the decoded rows don't carry the data needed to apply them locally.
_UNSUPPORTED = (
    "emissions",
    "bags",
    "exclude_basic_economy",
)


def _iata(value: Any) -> str:
    """Return the bare IATA code for an ``Airport``/``Airline`` enum or string."""
    name = getattr(value, "name", None)
    return (name or str(value)).removeprefix("_")


def _legs_of(flight: FlightResult) -> list[LegSpec]:
    """Describe a chosen itinerary as the leg specs ``tfs`` pins it by."""
    return [
        LegSpec(
            origin=_iata(leg.departure_airport),
            dep_date=leg.departure_datetime.strftime("%Y-%m-%d"),
            dest=_iata(leg.arrival_airport),
            airline=_iata(leg.airline),
            flight_number=str(leg.flight_number),
        )
        for leg in flight.legs
    ]


def _encode_segments(filters: Any, travel_dates: list[str] | None = None) -> bytes:
    """Encode every travel direction in ``filters`` as ``tfs`` field 3 bytes.

    Shared by :func:`build_tfs` and :func:`build_multi_city_tfs`, which differ
    only in the trip type they stamp on the envelope and in which trip types
    they accept. Forking this loop is how the two would drift.

    Args:
        filters: A ``FlightSearchFilters`` or ``DateSearchFilters``.
        travel_dates: Optional per-segment date overrides, used by the date
            sweep to reprice one segment set across a range without
            deep-copying the whole filter object per day.

    Returns:
        The concatenated segment bytes.

    """
    stops = filters.stops.value

    # Airline codes and alliance names share one pair of carrier lists
    # (segment fields 6 and 7), so both ride in together.
    carriers = [_iata(a) for a in (getattr(filters, "airlines", None) or [])] + [
        a.value for a in (getattr(filters, "alliances", None) or [])
    ]
    carriers_exclude = [_iata(a) for a in (getattr(filters, "airlines_exclude", None) or [])] + [
        a.value for a in (getattr(filters, "alliances_exclude", None) or [])
    ]
    layovers = getattr(filters, "layover_restrictions", None)

    segments = b""
    for index, segment in enumerate(filters.flight_segments):
        selected = segment.selected_flight
        segments += encode_tfs_segment(
            [_iata(entry[0]) for entry in segment.departure_airport],
            [_iata(entry[0]) for entry in segment.arrival_airport],
            travel_dates[index] if travel_dates else segment.travel_date,
            legs=_legs_of(selected) if selected is not None else (),
            # MaxStops.ANY (0) must leave the field out — writing 0 for it
            # would silently pin every search to non-stop.
            max_stops=stops - 1 if stops else None,
            carriers=carriers,
            carriers_exclude=carriers_exclude,
            layover_airports=[_iata(a) for a in (getattr(layovers, "airports", None) or [])],
            min_layover=getattr(layovers, "min_duration", None),
            max_layover=getattr(layovers, "max_duration", None),
        )
    return segments


def _envelope(filters: Any, segments: bytes, trip_type: TfsTripType) -> str:
    """Wrap encoded segments with the party and cabin ``filters`` describe.

    Args:
        filters: The filters the segments came from.
        segments: Output of :func:`_encode_segments`.
        trip_type: The ``tfs`` field 19 value to stamp.

    Returns:
        The base64url ``tfs`` value, unpadded.

    """
    return encode_tfs_payload(
        segments,
        trip_type=trip_type,
        passengers=passenger_codes(filters.passenger_info) or [1],
        seat=filters.seat_type.value,
    )


def build_tfs(filters: Any, *, travel_dates: list[str] | None = None) -> str:
    """Build the ``tfs`` URL parameter for ``filters``.

    Args:
        filters: A ``FlightSearchFilters`` or ``DateSearchFilters``. Both
            carry ``trip_type``, ``passenger_info``, ``flight_segments``,
            ``stops`` and ``seat_type``, which is all this encoder reads.
        travel_dates: Optional per-segment date overrides, used by the date
            sweep to reprice one segment set across a range without
            deep-copying the whole filter object per day.

    Returns:
        The base64url ``tfs`` value, unpadded, as Google's own URLs carry it.

    Raises:
        SearchUnsupportedError: For multi-city trips, which *this* transport
            cannot serve. The refusal is correct and stays: this is a pure
            encoder for the HTTP search-page path, and routing does not
            belong in it.

    """
    # Field 19 is the trip type. Multi-city is 3, but sending 2 (one-way)
    # makes Google ignore every segment past the first and serve the first
    # leg's one-way board, which decodes cleanly into wrong results; sending
    # 3 renders the right board in a browser but inlines no flight rows —
    # multi-city is fetched client-side through the RPC gated since 2026-08.
    if filters.trip_type == TripType.MULTI_CITY:
        raise SearchUnsupportedError(
            "Multi-city search is not available through the search-page transport: "
            "Google loads those results client-side through the gated RPC, so the "
            "page carries no rows to read. fli can read it with the optional "
            "browser transport — install `flights[browser]` and use "
            "fli.search.SearchMultiCity, which returns a first-leg board priced "
            "for the entire trip. Without it, search each leg separately. "
            "See github.com/punitarani/fli#223."
        )

    return _envelope(
        filters,
        _encode_segments(filters, travel_dates),
        TfsTripType.ONE_WAY if filters.trip_type == TripType.ONE_WAY else TfsTripType.ROUND_TRIP,
    )


def build_multi_city_tfs(filters: Any) -> str:
    """Build the ``tfs`` parameter for a multi-city search page.

    Identical to :func:`build_tfs` but for the trip type stamped on the
    envelope, so a multi-city URL carries exactly the stop ceiling, cabin,
    party, carrier lists and layover bounds the caller asked for. The page it
    addresses inlines no rows; :class:`fli.search.multi_city.SearchMultiCity`
    loads it in a browser and intercepts the RPC that fills it.

    Args:
        filters: A ``FlightSearchFilters`` with ``trip_type`` MULTI_CITY.

    Returns:
        The base64url ``tfs`` value, unpadded.

    Raises:
        ValueError: *filters* is not a multi-city search, or has fewer than
            two segments.

    """
    if filters.trip_type != TripType.MULTI_CITY:
        raise ValueError(
            f"build_multi_city_tfs is for multi-city searches; got {filters.trip_type!r}. "
            "Use build_tfs for one-way and round-trip."
        )
    if len(filters.flight_segments) < 2:
        raise ValueError("A multi-city itinerary needs at least two segments")
    return _envelope(filters, _encode_segments(filters), TfsTripType.MULTI_CITY)


def page_url(
    tfs: str,
    currency: str | None = None,
    language: str | None = None,
    country: str | None = None,
) -> str:
    """Build the search-page URL for a ``tfs`` value and locale."""
    params = [f"tfs={tfs}", f"hl={language or 'en'}", f"gl={country or 'US'}"]
    if currency:
        params.append(f"curr={currency}")
    return f"{PAGE_URL}?{'&'.join(params)}"


def multi_city_url(
    legs: Sequence[tuple[str, str, str]],
    currency: str | None = None,
    language: str | None = None,
    country: str | None = None,
    carriers: Sequence[str] = (),
    passengers: Sequence[int] = (1,),
    seat: int = 1,
    max_stops: int | None = None,
) -> str:
    """Build a Google Flights URL for a multi-city itinerary.

    This is the plain-URL form, taking bare ``(origin, destination, date)``
    triples rather than a filter object: it is what the CLI prints when it
    cannot fetch the board itself, and what
    :class:`~fli.models.MultiCityBoard` carries as its ``booking_url``. It
    must keep working on an install with no browser extra, so it grows no
    import that needs one.

    :func:`build_multi_city_tfs` is the filter-driven sibling used to address
    the page the browser transport actually loads.

    Args:
        legs: Ordered ``(origin, destination, date)`` triples, IATA codes and
            ``YYYY-MM-DD``. At least two.
        currency: ISO 4217 code appended as ``curr=``.
        language: BCP-47 code appended as ``hl=``.
        country: ISO 3166-1 alpha-2 code appended as ``gl=``.
        carriers: Airline IATA codes or alliance names to restrict every leg
            to, encoded into each segment's carrier include list exactly as
            :func:`build_tfs` does. Without it a filtered search would
            research one set of flights and then link a board showing another.
        passengers: Google's repeated field 8 party codes, as
            :func:`passenger_codes` builds them. Defaults to a single adult.
        seat: Cabin class as the ``SeatType`` value. Defaults to economy.
        max_stops: Stop ceiling applied to every leg, as the ``MaxStops``
            value, or ``None`` for no ceiling.

    The last three exist for the same reason *carriers* does: this URL is
    handed to a user as "Google prices the whole itinerary as one ticket
    here", so a board priced for two in business must not link a board
    priced for one in economy.

    Returns:
        A ``https://www.google.com/travel/flights?tfs=…`` URL.

    Raises:
        ValueError: Fewer than two legs.

    """
    if len(legs) < 2:
        raise ValueError("A multi-city itinerary needs at least two legs")

    segments = b"".join(
        encode_tfs_segment(origin, dest, date, carriers=list(carriers), max_stops=max_stops)
        for origin, dest, date in legs
    )
    tfs = encode_tfs_payload(
        segments,
        trip_type=TfsTripType.MULTI_CITY,
        passengers=list(passengers) or [1],
        seat=seat,
    )
    return page_url(tfs, currency, language, country)


def explore_page_url(
    filters: Any,
    currency: str | None = None,
    language: str | None = None,
    country: str | None = None,
) -> str:
    """Build the Explore page URL whose load fires ``GetExploreDestinations``.

    Verified live 2026-09-20 against the running service: navigating to
    ``/travel/explore?tfs=…`` with a segment carrying *only* a date and an
    origin (field 13) makes the page issue ``GetExploreDestinations``, whose
    body decodes through the existing Explore decoders unchanged — 66
    destinations, 52 of them priced, for ``LHR`` on ``2026-10-16``. A bare
    ``/travel/explore`` with no ``tfs`` fires nothing at all: the empty page
    waits for an origin, so the parameter is not optional.

    Scope, stated because it is narrower than :class:`ExploreSearchFilters`:

    * **Origin must be an airport.** The verified encoding is field 13's
      ``{1: 1, 2: "<IATA>"}`` pair. A knowledge-graph ``ExplorePlace`` origin
      has no tested spelling here.
    * **Destination must be "anywhere".** Writing a mid into field 14 was
      tried live and did *not* narrow the board — ``/m/0250wj`` (Southern
      Europe) returned Edinburgh, Amsterdam and Berlin among 147 results,
      i.e. Google ignored it. Rather than ship a filter that silently does
      nothing, a narrowed destination is refused here.

    Stop ceiling, cabin, party and carrier lists ride in the same ``tfs``
    fields the flights page reads. They are carried through on the strength
    of that shared envelope rather than on a live A/B of each one, so treat
    them as plausible-but-unproven and prefer reporting a wrong result over
    assuming one.

    Args:
        filters: An :class:`~fli.models.ExploreSearchFilters`.
        currency: ISO 4217 code appended as ``curr=``.
        language: BCP-47 code appended as ``hl=``.
        country: ISO 3166-1 alpha-2 code appended as ``gl=``.

    Returns:
        A ``https://www.google.com/travel/explore?tfs=…`` URL.

    Raises:
        SearchUnsupportedError: The origin is not an airport, or the
            destination is anything other than "anywhere".

    """
    from fli.models.google_flights.explore import ExploreRegion

    origin = filters.origin
    if not hasattr(origin, "name") or getattr(origin, "mid", None) is not None:
        raise SearchUnsupportedError(
            "The browser-backed Explore transport addresses its page by an "
            "airport origin; a knowledge-graph place has no verified spelling "
            f"in the page URL. Got origin={origin!r}. Pass an Airport."
        )
    if filters.destination is not ExploreRegion.ANYWHERE:
        raise SearchUnsupportedError(
            "The browser-backed Explore transport can only search 'anywhere': "
            "writing a destination region into the page URL was tested live "
            "and Google ignored it, so honouring "
            f"destination={filters.destination!r} would mean returning a "
            "board that quietly does not match the filter. Search anywhere "
            "and narrow the results yourself."
        )

    stops = filters.stops.value
    carriers = [_iata(a) for a in (getattr(filters, "airlines", None) or [])] + [
        a.value for a in (getattr(filters, "alliances", None) or [])
    ]
    carriers_exclude = [_iata(a) for a in (getattr(filters, "airlines_exclude", None) or [])] + [
        a.value for a in (getattr(filters, "alliances_exclude", None) or [])
    ]
    segment = encode_tfs_segment(
        _iata(origin),
        # ``dest=()`` writes no field 14 at all, which is the verified shape.
        (),
        filters.departure_date,
        max_stops=stops - 1 if stops else None,
        carriers=carriers,
        carriers_exclude=carriers_exclude,
    )
    tfs = encode_tfs_payload(
        segment,
        trip_type=(
            TfsTripType.ONE_WAY if filters.trip_type == TripType.ONE_WAY else TfsTripType.ROUND_TRIP
        ),
        passengers=passenger_codes(filters.passenger_info) or [1],
        seat=filters.seat_type.value,
    )
    params = [f"tfs={tfs}", f"hl={language or 'en'}", f"gl={country or 'US'}"]
    if currency:
        params.append(f"curr={currency}")
    return f"{PAGE_EXPLORE_URL}?{'&'.join(params)}"


def extract_payload(html: str) -> Any | None:
    """Pull the ``ds:1`` payload out of a rendered search page.

    Returns the same structure the RPC used to hand back, so callers can
    keep reading ``payload[2]`` / ``payload[3]`` for flight rows.
    """
    for match in _DS_BLOB.finditer(html):
        blob = match.group(1)
        key = _DS_KEY.search(blob)
        if not key or key.group(1) != "ds:1":
            continue
        data = _DS_DATA.search(blob)
        if not data:
            continue
        try:
            return json.loads(data.group(1))
        except (ValueError, json.JSONDecodeError):
            logger.warning("ds:1 blob is not valid JSON", exc_info=True)
            return None
    return None


def unsupported_filters(filters: Any) -> list[str]:
    """Name the set filters this transport can neither encode nor emulate."""
    named = []
    for attr in _UNSUPPORTED:
        value = getattr(filters, attr, None)
        if value in (None, False, []):
            continue
        # Enum defaults (EmissionsFilter.ALL) are "unset" for our purposes.
        if getattr(value, "name", None) == "ALL":
            continue
        named.append(attr)
    return named


def apply_client_side_filters(flights: list[Any], filters: Any) -> list[Any]:
    """Apply the filters ``tfs`` has no field for, to already-decoded rows.

    Google would have applied these server-side and back-filled the result
    list, so a filtered search returns fewer options here than the old RPC
    did — but every option it does return honours the filter.

    The airline branch is a safety net rather than the only enforcement:
    :func:`build_tfs` now encodes the carrier lists, so Google has already
    filtered and back-filled.

    It is suspended whenever an alliance rides in the include list. Airlines
    and alliances share one include list on the wire, which Google reads as a
    union, so it deliberately returns rows whose carrier is in the alliance
    but not in ``airlines``. There is no alliance-to-member table here to
    reproduce that union locally, and re-applying the airline-only test
    would discard precisely the rows the caller asked for.

    An alliance reaches that list two ways, and both must suspend the test:
    ``--alliance ONEWORLD``, and ``--airlines ONEWORLD`` — the latter because
    ``parse_airlines`` resolves alliance names to the matching ``Airline``
    pseudo-members on purpose, and Google accepts either spelling. No
    ``FlightLeg.airline`` is ever such a pseudo-member, so leaving the test on
    matched nothing at all.

    The exclude branch needs no such guard: it can only drop rows Google has
    already dropped, so the local pass is always a subset.
    """
    # See the note above: an alliance anywhere in the include list makes the
    # wire filter a union this function cannot reproduce, so it must not
    # second-guess it.
    airline_include = getattr(filters, "airlines", None) or []
    alliance_include = bool(getattr(filters, "alliances", None)) or any(
        getattr(a, "name", None) in Alliance.__members__ for a in airline_include
    )
    airlines = set() if alliance_include else {_iata(a) for a in airline_include}
    excluded = {_iata(a) for a in (getattr(filters, "airlines_exclude", None) or [])}
    max_duration = getattr(filters, "max_duration", None)
    price_limit = getattr(filters, "price_limit", None)
    max_price = getattr(price_limit, "max_price", None)

    # These rows describe whichever segment the caller is still choosing, not
    # necessarily the outbound one. `_expand_multi_leg` pins a segment by
    # setting its `selected_flight` and re-fetches, so the flights coming back
    # belong to the first segment that is still unpinned. Filtering a return
    # leg against the outbound window drops valid evening returns and keeps
    # invalid ones, silently and in the caller's favour-looking direction.
    active = next(
        (
            index
            for index, segment in enumerate(filters.flight_segments)
            if getattr(segment, "selected_flight", None) is None
        ),
        max(0, len(filters.flight_segments) - 1),
    )
    segments = filters.flight_segments
    window = getattr(segments[active], "time_restrictions", None) if segments else None

    out = []
    for flight in flights:
        carriers = {_iata(leg.airline) for leg in flight.legs}
        if airlines and not carriers & airlines:
            continue
        if excluded and carriers & excluded:
            continue
        if max_duration is not None and flight.duration and flight.duration > max_duration:
            continue
        if max_price is not None and flight.price and flight.price > max_price:
            continue
        if not _within_window(flight, window):
            continue
        out.append(flight)
    return out


def _within_window(flight: Any, restrictions: Any) -> bool:
    """Check a flight's departure/arrival hours against a segment's window."""
    if restrictions is None:
        return True
    departure = flight.legs[0].departure_datetime.hour
    arrival = flight.legs[-1].arrival_datetime.hour
    checks = (
        (getattr(restrictions, "earliest_departure", None), departure, "min"),
        (getattr(restrictions, "latest_departure", None), departure, "max"),
        (getattr(restrictions, "earliest_arrival", None), arrival, "min"),
        (getattr(restrictions, "latest_arrival", None), arrival, "max"),
    )
    for bound, actual, kind in checks:
        if bound is None:
            continue
        if kind == "min" and actual < bound:
            return False
        if kind == "max" and actual > bound:
            return False
    return True
