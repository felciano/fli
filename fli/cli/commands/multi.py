"""Multi-city flight search CLI command."""

import re
from typing import Annotated

import typer

from fli.cli.console import console
from fli.cli.errors import report_cli_error
from fli.cli.utils import display_flight_results, validate_time_range
from fli.core import (
    build_multi_city_segments,
    normalize_date,
    parse_airlines,
    parse_cabin_class,
    parse_max_stops,
    parse_sort_by,
    resolve_airport,
)
from fli.core.parsers import ParseError
from fli.models import (
    Airport,
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
    TimeRestrictions,
)
from fli.models.google_flights.base import TripType
from fli.search import SearchClientError, SearchFlights
from fli.search._tfs import multi_city_url

# 3 letters = IATA, 4 = ICAO. Deliberately not ``+``: the comma is the
# field separator here, so a longer token is a malformed leg, not an
# airport, and must fail as a leg-format error.
LEG_PATTERN = re.compile(r"^([A-Za-z]{3,4}),([A-Za-z]{3,4}),(\d{4}-\d{1,2}-\d{1,2})$")


def _parse_leg(value: str) -> tuple[str, str, str]:
    """Parse a leg string in ORIGIN,DEST,DATE format.

    ORIGIN and DEST may each be a 3-letter IATA or 4-letter ICAO code; the
    codes are only shape-checked here and validated by
    :func:`fli.core.parsers.resolve_airport`.

    Returns:
        Tuple of (origin, destination, date).

    Raises:
        typer.BadParameter: If the format is invalid.

    """
    match = LEG_PATTERN.match(value)
    if not match:
        raise typer.BadParameter(
            f"Invalid leg format: '{value}'. Expected ORIGIN,DEST,DATE (e.g., SEA,HKG,2026-12-26)"
        )
    return match.group(1).upper(), match.group(2).upper(), match.group(3)


def multi(
    legs: Annotated[
        list[str],
        typer.Option(
            "--leg",
            "-l",
            help=(
                "Flight leg in ORIGIN,DEST,DATE format, where ORIGIN and DEST are "
                "IATA or ICAO codes (repeatable, minimum 2)"
            ),
        ),
    ],
    departure_window: Annotated[
        str | None,
        typer.Option(
            "--time",
            "-t",
            help="Departure time window in 24h format (e.g., 6-20)",
            callback=validate_time_range,
        ),
    ] = None,
    airlines: Annotated[
        list[str] | None,
        typer.Option(
            "--airlines",
            "-a",
            help="Airline IATA codes (e.g., BA,KL or repeated --airlines BA --airlines KL)",
        ),
    ] = None,
    cabin_class: Annotated[
        str,
        typer.Option(
            "--class",
            "-c",
            help="Cabin class (ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST)",
        ),
    ] = "ECONOMY",
    max_stops: Annotated[
        str,
        typer.Option(
            "--stops",
            "-s",
            help="Maximum stops (ANY, 0 for non-stop, 1 for one stop, 2+ for two stops)",
        ),
    ] = "ANY",
    sort_by: Annotated[
        str,
        typer.Option(
            "--sort",
            "-o",
            help="Sort results by (CHEAPEST, DURATION, DEPARTURE_TIME, ARRIVAL_TIME)",
        ),
    ] = "CHEAPEST",
    passengers: Annotated[
        int,
        typer.Option(
            "--passengers",
            "-p",
            help="Number of adult passengers",
            min=1,
        ),
    ] = 1,
    children: Annotated[
        int,
        typer.Option(
            "--children",
            help="Number of children (aged 2-11)",
            min=0,
        ),
    ] = 0,
    infants_in_seat: Annotated[
        int,
        typer.Option(
            "--infants-in-seat",
            help="Number of infants occupying their own seat",
            min=0,
        ),
    ] = 0,
    infants_on_lap: Annotated[
        int,
        typer.Option(
            "--infants-on-lap",
            help="Number of infants travelling on an adult's lap",
            min=0,
        ),
    ] = 0,
):
    """Search for multi-city flights with multiple legs.

    Each leg specifies an origin, destination, and date. At least two legs are required.

    Example:
        fli multi --leg SEA,HKG,2026-12-26 --leg PEK,SEA,2027-01-02
        fli multi -l SEA,NRT,2026-12-26 -l NRT,HKG,2026-12-30 -l HKG,SEA,2027-01-05 -c BUSINESS
        fli multi -l SEA,NRT,2026-12-26 -l HKG,SEA,2027-01-05 --passengers 2

    """
    try:
        if len(legs) < 2:
            typer.echo("Error: multi-city search requires at least 2 legs")
            raise typer.Exit(1)

        # Parse and validate each leg
        parsed_legs = []
        for leg_str in legs:
            origin, destination, date = _parse_leg(leg_str)
            date = normalize_date(date)
            origin_airport = resolve_airport(origin)
            destination_airport = resolve_airport(destination)
            parsed_legs.append((origin_airport, destination_airport, date))

        # Parse shared filter parameters
        seat_type = parse_cabin_class(cabin_class)
        stops = parse_max_stops(max_stops)
        parsed_airlines = parse_airlines(airlines)
        sort = parse_sort_by(sort_by)

        # Build time restrictions
        time_restrictions = None
        if departure_window:
            time_restrictions = TimeRestrictions(
                earliest_departure=departure_window[0],
                latest_departure=departure_window[1],
            )

        # Build multi-city segments using shared builder
        segments, trip_type = build_multi_city_segments(
            legs=parsed_legs,
            time_restrictions=time_restrictions,
        )

        # Create search filters
        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(
                adults=passengers,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
            ),
            flight_segments=segments,
            stops=stops,
            seat_type=seat_type,
            airlines=parsed_airlines,
            sort_by=sort,
        )

        # Multi-city cannot be searched as one request: Google serves those
        # results over the RPC gated since 2026-08, so the search page carries
        # no rows to read. Rather than refuse, research the legs individually —
        # one-way search does work — and hand back the URL that prices the
        # whole thing as a single ticket, which is usually cheaper than the sum
        # of one-ways on the same flights.
        _research_legs(parsed_legs, filters)

    except ParseError as e:
        typer.echo(f"Error: {str(e)}")
        raise typer.Exit(1) from e
    except (AttributeError, ValueError) as e:
        typer.echo(f"Error: {str(e)}")
        raise typer.Exit(1) from e
    except SearchClientError as e:
        raise report_cli_error(e, command="multi") from e
    except Exception as e:  # noqa: BLE001 — fall back to clean reporting
        raise report_cli_error(e, command="multi") from e


def _research_legs(
    parsed_legs: list[tuple[Airport, Airport, str]],
    filters: FlightSearchFilters,
) -> None:
    """Search each leg as a one-way and print the combined research view.

    Deliberately not presented as a multi-city result. The per-leg total is a
    sum of independent one-way fares, which is a different product from the
    single multi-city ticket Google will sell for the same legs. Neither is
    reliably cheaper: on a four-leg test itinerary the cheapest nonstop
    one-ways came to $1,785 against $1,395 for the multi-city ticket, while
    the cheapest-at-any-number-of-stops sum landed within a dollar of it. The
    URL printed at the end is what prices the real thing.
    """
    search_client = SearchFlights()
    per_leg_cheapest: list[float | None] = []

    for index, (origin, destination, date) in enumerate(parsed_legs, start=1):
        leg_filters = filters.model_copy(deep=True)
        leg_filters.trip_type = TripType.ONE_WAY
        leg_filters.flight_segments = [
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[destination, 0]],
                travel_date=date,
                time_restrictions=filters.flight_segments[index - 1].time_restrictions,
            )
        ]
        header = f"Leg {index}: {origin.name} to {destination.name} on {date}"
        console.print(f"\n[bold]{header}[/bold]")
        try:
            results = search_client.search(leg_filters)
        except SearchClientError as exc:
            console.print(f"  [yellow]leg search failed: {exc}[/yellow]")
            per_leg_cheapest.append(None)
            continue
        if not results:
            console.print("  [yellow]No flights found for this leg.[/yellow]")
            per_leg_cheapest.append(None)
            continue
        display_flight_results(results, trip_type=TripType.ONE_WAY)
        priced = [r.price for r in results if r.price is not None]
        per_leg_cheapest.append(min(priced) if priced else None)

    if not any(p is not None for p in per_leg_cheapest):
        console.print("\n[yellow]No flights found.[/yellow]")
        raise typer.Exit(1)

    url = multi_city_url(
        [(o.name.lstrip("_"), d.name.lstrip("_"), date) for o, d, date in parsed_legs],
    )
    console.print("\n[bold]Multi-city fare[/bold]")
    if all(p is not None for p in per_leg_cheapest) and per_leg_cheapest:
        total = sum(p for p in per_leg_cheapest if p is not None)
        console.print(f"  Cheapest on each leg, booked separately: [bold]{total:,.0f}[/bold]")
        console.print(
            "  [dim]That is a sum of independent one-way fares, which is a different "
            "product from a single multi-city ticket — compare it, do not assume "
            "either is cheaper.[/dim]"
        )
    console.print(f"  Google prices the whole itinerary as one ticket here:\n  [cyan]{url}[/cyan]")
