"""Shared building utilities for constructing search filters.

This module provides builder functions used by both the CLI and MCP interfaces
to construct flight search filter objects.
"""

from datetime import datetime, timedelta

from fli.models import Airport, FlightSegment, TimeRestrictions, TripType

#: Trip length used for a round-trip search that names no duration at all.
DEFAULT_TRIP_DURATION = 3

#: Ceiling on ``len(durations) * days_in_range`` for one duration sweep.
#:
#: Since the search transport moved to the public search page, every
#: (duration, departure date) pair costs its own page fetch, and the shared
#: client drains at 10 requests/second. 600 combinations is roughly a minute
#: of sustained traffic — enough for a useful sweep (e.g. 4-7 nights across a
#: 60-day window is 240) and far short of the tens of thousands an unbounded
#: range would issue.
MAX_DURATION_SWEEP_COMBINATIONS = 600


def resolve_duration_sweep(
    *,
    trip_duration: int | None,
    min_duration: int | None,
    max_duration: int | None,
    is_round_trip: bool,
    days_in_range: int,
) -> list[int | None]:
    """Resolve the trip lengths a date search should sweep.

    Owns every rule governing ``--duration`` against
    ``--min-duration``/``--max-duration`` so the CLI and the MCP tool cannot
    drift apart, and refuses a sweep whose request volume would be abusive
    before any network call is made.

    Args:
        trip_duration: Explicit fixed trip length, or None when unset.
        min_duration: Shortest trip length to sweep, or None.
        max_duration: Longest trip length to sweep, or None.
        is_round_trip: Whether the search is a round trip.
        days_in_range: Number of departure dates in the search range,
            used to estimate the sweep's request count.

    Returns:
        The trip lengths to search, in ascending order. ``[None]`` for a
        one-way search; a single-element list when no sweep was requested.

    Raises:
        ParseError: If the duration options contradict each other, or if the
            sweep would exceed :data:`MAX_DURATION_SWEEP_COMBINATIONS`.

    """
    from fli.core.parsers import ParseError

    if min_duration is None and max_duration is None:
        if not is_round_trip:
            return [None]
        return [trip_duration if trip_duration is not None else DEFAULT_TRIP_DURATION]

    if min_duration is None or max_duration is None:
        raise ParseError(
            "--min-duration / min_duration and --max-duration / max_duration must be "
            "given together. An open-ended range would sweep every trip length that "
            "fits the date range and issue thousands of searches."
        )

    if trip_duration is not None:
        raise ParseError(
            "Cannot combine --duration / trip_duration with --min-duration / "
            "--max-duration. Use --duration for one fixed trip length, or the "
            "min/max pair to sweep a range of trip lengths."
        )

    if not is_round_trip:
        raise ParseError(
            "--min-duration / --max-duration sweep round-trip lengths, so they need "
            "--round / is_round_trip. A one-way search has no trip duration."
        )

    if min_duration > max_duration:
        raise ParseError(
            f"--min-duration / min_duration ({min_duration}) cannot exceed "
            f"--max-duration / max_duration ({max_duration})."
        )

    durations = list(range(min_duration, max_duration + 1))
    combinations = len(durations) * max(days_in_range, 0)
    if combinations > MAX_DURATION_SWEEP_COMBINATIONS:
        raise ParseError(
            f"That sweep would issue about {combinations} searches "
            f"({len(durations)} trip lengths x {days_in_range} departure dates), over "
            f"the {MAX_DURATION_SWEEP_COMBINATIONS} limit. Narrow the trip lengths "
            f"(--min-duration / --max-duration) or the date range (--from / --to)."
        )

    return durations


def normalize_date(date_str: str) -> str:
    """Normalize a date string to zero-padded YYYY-MM-DD format.

    Args:
        date_str: Date string in YYYY-MM-DD format (e.g., '2026-4-2' or '2026-04-02')

    Returns:
        Zero-padded date string (e.g., '2026-04-02')

    Raises:
        ValueError: If the date string is not a valid date

    """
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")


def build_time_restrictions(
    departure_window: str | None = None,
    arrival_window: str | None = None,
) -> TimeRestrictions | None:
    """Build a TimeRestrictions object from time window strings.

    Args:
        departure_window: Departure time range in 'HH-HH' format (e.g., '6-20')
        arrival_window: Arrival time range in 'HH-HH' format (e.g., '8-22')

    Returns:
        TimeRestrictions object, or None if no restrictions specified

    """
    if not departure_window and not arrival_window:
        return None

    earliest_departure = None
    latest_departure = None
    earliest_arrival = None
    latest_arrival = None

    if departure_window:
        from fli.core.parsers import parse_time_range

        earliest_departure, latest_departure = parse_time_range(departure_window)

    if arrival_window:
        from fli.core.parsers import parse_time_range

        earliest_arrival, latest_arrival = parse_time_range(arrival_window)

    return TimeRestrictions(
        earliest_departure=earliest_departure,
        latest_departure=latest_departure,
        earliest_arrival=earliest_arrival,
        latest_arrival=latest_arrival,
    )


def build_flight_segments(
    origin: Airport | list[Airport],
    destination: Airport | list[Airport],
    departure_date: str,
    return_date: str | None = None,
    time_restrictions: TimeRestrictions | None = None,
    return_time_restrictions: TimeRestrictions | None = None,
) -> tuple[list[FlightSegment], TripType]:
    """Build flight segments for a search request.

    Args:
        origin: Departure airport(s) - single Airport or list for multi-airport search
        destination: Arrival airport(s) - single Airport or list for multi-airport search
        departure_date: Outbound travel date in YYYY-MM-DD format
        return_date: Return travel date in YYYY-MM-DD format (optional)
        time_restrictions: Time restrictions to apply to segments
        return_time_restrictions: Time restrictions for the return leg only.
            ``None`` (the default) means the return leg inherits
            ``time_restrictions``, which is what every caller relied on before
            per-leg windows existed. Requires ``return_date``.

    Returns:
        Tuple of (list of FlightSegment objects, TripType)

    Raises:
        ParseError: If a return window is given without a return date.

    """
    from fli.core.parsers import ParseError

    if return_time_restrictions is not None and not return_date:
        raise ParseError(
            "A return-leg departure window (--return-time / return_departure_window) "
            "only applies to a round trip. Add a return date (--return / return_date), "
            "or drop the return window."
        )

    departure_date = normalize_date(departure_date)

    # Normalize to lists for uniform handling
    origins = origin if isinstance(origin, list) else [origin]
    destinations = destination if isinstance(destination, list) else [destination]

    segments = [
        FlightSegment(
            departure_airport=[[apt, 0] for apt in origins],
            arrival_airport=[[apt, 0] for apt in destinations],
            travel_date=departure_date,
            time_restrictions=time_restrictions,
        )
    ]

    trip_type = TripType.ONE_WAY

    if return_date:
        return_date = normalize_date(return_date)
        trip_type = TripType.ROUND_TRIP
        segments.append(
            FlightSegment(
                departure_airport=[[apt, 0] for apt in destinations],
                arrival_airport=[[apt, 0] for apt in origins],
                travel_date=return_date,
                # Inheriting the outbound window is the historical contract:
                # anything else would silently stop filtering the return leg
                # of every existing round-trip search.
                time_restrictions=(
                    return_time_restrictions
                    if return_time_restrictions is not None
                    else time_restrictions
                ),
            )
        )

    return segments, trip_type


def build_multi_city_segments(
    legs: list[tuple[Airport, Airport, str]],
    time_restrictions: TimeRestrictions | None = None,
) -> tuple[list[FlightSegment], TripType]:
    """Build flight segments for a multi-city search.

    Args:
        legs: List of (origin, destination, date) tuples for each leg
        time_restrictions: Time restrictions to apply to all segments

    Returns:
        Tuple of (list of FlightSegment objects, TripType.MULTI_CITY)

    Note:
        Multi-city searches with distinct city pairs may time out due to
        limitations of the Google Flights API endpoint.  Round-trip-style
        multi-city (same origin and final destination) works reliably.


    """
    segments = [
        FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=normalize_date(date),
            time_restrictions=time_restrictions,
        )
        for origin, destination, date in legs
    ]

    return segments, TripType.MULTI_CITY


def build_date_search_segments(
    origin: Airport | list[Airport],
    destination: Airport | list[Airport],
    start_date: str,
    trip_duration: int | None = None,
    is_round_trip: bool = False,
    time_restrictions: TimeRestrictions | None = None,
) -> tuple[list[FlightSegment], TripType]:
    """Build flight segments for a date range search.

    Args:
        origin: Departure airport(s) - single Airport or list for multi-airport search
        destination: Arrival airport(s) - single Airport or list for multi-airport search
        start_date: Start date of the search range in YYYY-MM-DD format
        trip_duration: Duration of the trip in days (for round trips)
        is_round_trip: Whether to search for round-trip flights
        time_restrictions: Time restrictions to apply to segments

    Returns:
        Tuple of (list of FlightSegment objects, TripType)

    """
    start_date = normalize_date(start_date)

    # Normalize to lists for uniform handling
    origins = origin if isinstance(origin, list) else [origin]
    destinations = destination if isinstance(destination, list) else [destination]

    segments = [
        FlightSegment(
            departure_airport=[[apt, 0] for apt in origins],
            arrival_airport=[[apt, 0] for apt in destinations],
            travel_date=start_date,
            time_restrictions=time_restrictions,
        )
    ]

    trip_type = TripType.ONE_WAY

    if is_round_trip:
        trip_type = TripType.ROUND_TRIP
        return_date = (
            datetime.strptime(start_date, "%Y-%m-%d")
            + timedelta(days=trip_duration or DEFAULT_TRIP_DURATION)
        ).strftime("%Y-%m-%d")

        segments.append(
            FlightSegment(
                departure_airport=[[apt, 0] for apt in destinations],
                arrival_airport=[[apt, 0] for apt in origins],
                travel_date=return_date,
                time_restrictions=time_restrictions,
            )
        )

    return segments, trip_type
