"""Multi-city flight search CLI command."""

import re
from typing import Annotated

import typer
from rich.markup import escape

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
    MultiCityBoard,
    PassengerInfo,
    TimeRestrictions,
)
from fli.models.google_flights.base import TripType
from fli.search import (
    BrowserOptions,
    SearchClientError,
    SearchFlights,
    SearchMultiCity,
    board_total,
    browser_available,
)
from fli.search._browser import INSTALL_HINT
from fli.search._tfs import multi_city_url, passenger_codes
from fli.search.exceptions import SearchParseError

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
    browser: Annotated[
        bool | None,
        typer.Option(
            "--browser/--no-browser",
            help=(
                "Fetch the real multi-city board through the optional browser "
                "transport. Default: use it when installed, fall back to "
                "per-leg research when not. --no-browser never starts one."
            ),
        ),
    ] = None,
    browser_cdp: Annotated[
        str | None,
        typer.Option(
            "--browser-cdp",
            help=(
                "Attach to a Chrome already listening for CDP, e.g. "
                "http://127.0.0.1:9222, instead of launching one."
            ),
        ),
    ] = None,
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

        # Multi-city over HTTP carries no rows: Google fills that board from
        # the RPC gated since 2026-08. With the optional browser transport we
        # can read the real board; without it we research the legs
        # individually — one-way search does work — and hand back the URL
        # that prices the whole thing as a single ticket. Which of the two
        # the user got is stated on screen either way; a research view that
        # reads like a multi-city answer is worse than no answer.
        board = _try_board(filters, browser=browser, browser_cdp=browser_cdp)
        if board is not None:
            _print_board(board)
            return
        _research_legs(parsed_legs, filters)

    except typer.Exit:
        # Typer's own control flow, not a failure to report. Letting it fall
        # through to the generic handler below turned a deliberate exit into
        # "Unexpected error: Exit" plus a traceback log file.
        raise
    except typer.BadParameter as e:
        # _parse_leg raises this for a malformed --leg. The generic handler
        # below reports the user's typo as an internal error and writes a
        # traceback log file, which is alarming and useless for a usage
        # mistake. Reported like ParseError -- same clean line, same exit
        # code -- rather than re-raised to typer, which would move the
        # message to stderr and the exit code to 2 and so change the
        # command's contract for a defect that is only about noise.
        typer.echo(f"Error: {str(e)}")
        raise typer.Exit(1) from e
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


def _try_board(
    filters: FlightSearchFilters,
    *,
    browser: bool | None,
    browser_cdp: str | None,
) -> MultiCityBoard | None:
    """Fetch the real multi-city board, or say plainly why we are not.

    Never silent. Every path that ends without a board prints one yellow
    line naming the reason first, because the fallback that follows looks
    like a multi-city answer and is not one.

    Args:
        filters: The multi-city search.
        browser: ``True`` to require the browser, ``False`` to forbid it,
            ``None`` to use it when it is installed.
        browser_cdp: A CDP endpoint to attach to instead of launching.

    Returns:
        The board, or ``None`` to fall back to per-leg research.

    Raises:
        typer.Exit: ``--browser`` was given explicitly and the extra is not
            installed.
        SearchClientError: ``--browser`` was given explicitly and the
            browser failed. An explicit request gets an error, not a quiet
            downgrade to a different product.

    """
    if browser is False:
        console.print(
            "[yellow]--no-browser: showing per-leg research instead of the "
            "real multi-city board.[/yellow]"
        )
        return None

    if not browser_available():
        message = (
            "The multi-city board needs the optional browser transport, which "
            f"is not installed — to enable it, {INSTALL_HINT}."
        )
        # ``escape``: the message names ``flights[browser]``, and rich reads
        # square brackets as markup — unescaped it prints ``flights``, which
        # is a different (and wrong) install command.
        if browser is True:
            console.print(f"[red]{escape(message)}[/red]")
            raise typer.Exit(1)
        console.print(f"[yellow]{escape(message)} Showing per-leg research instead.[/yellow]")
        return None

    options = BrowserOptions.from_env()
    if browser_cdp:
        options = options.model_copy(update={"cdp_endpoint": browser_cdp})

    try:
        board = SearchMultiCity(options).search(
            filters,
            currency=None,
            language=None,
            country=None,
        )
    except (SearchClientError, SearchParseError) as exc:
        # SearchParseError is deliberately kept outside SearchClientError, so
        # catching only the latter let a decode failure escape every labelled
        # fallback and land in the command's generic handler, reported as an
        # unexpected error rather than "the board did not decode, here is
        # per-leg research". Catching the parent rather than BrowserDecodeError
        # closes the other half of that hole: SearchMultiCity fetches the
        # search page over plain HTTP before it ever reaches a browser, and an
        # unparseable response there raises SearchParseError itself, which
        # naming only the subclass did not catch.
        if browser is True:
            raise
        console.print(f"[yellow]Could not fetch the multi-city board: {escape(str(exc))}[/yellow]")
        console.print("[yellow]Showing per-leg research instead.[/yellow]")
        return None

    if board is None:
        console.print("[yellow]Google returned no multi-city itineraries for these legs.[/yellow]")
        console.print("[yellow]Showing per-leg research instead.[/yellow]")
        return None
    return board


def _print_board(board: MultiCityBoard) -> None:
    """Print a fetched board, labelled for what it is.

    Args:
        board: The board to show.

    """
    first_leg = board.legs[board.board_leg_index]
    console.print(
        f"\n[bold]Multi-city board — leg {board.board_leg_index + 1}: "
        f"{first_leg[0]} to {first_leg[1]} on {first_leg[2]}[/bold]"
    )
    console.print(
        "[dim]These are options for that leg only. Each price is for the "
        "ENTIRE multi-city trip on one ticket, not for this leg — so do not "
        "add them up or compare them with a one-way fare.[/dim]"
    )
    # MULTI_CITY, not ONE_WAY: with ONE_WAY every option was headed "One-way
    # Flight Option N", directly contradicting the caveat printed just above
    # it. The rows are single ``FlightResult``s either way, so the only thing
    # this changes is the label — and the label was the wrong one.
    display_flight_results(board.results, trip_type=TripType.MULTI_CITY)

    console.print("\n[bold]Multi-city fare[/bold]")
    cheapest = board_total(board)
    if cheapest is not None:
        console.print(f"  Cheapest entire-trip fare on this board: [bold]{cheapest:,.0f}[/bold]")
    console.print("  Google prices the whole itinerary as one ticket here:")
    # soft_wrap: rich breaks a long URL across three lines, and a URL broken
    # across three lines cannot be copied out of a terminal.
    console.print(f"  [cyan]{board.booking_url}[/cyan]", soft_wrap=True)


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
    console.print(
        "\n[bold]Per-leg research[/bold] [dim](not a multi-city result: each leg "
        "is searched as an independent one-way)[/dim]"
    )
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
        except (SearchClientError, SearchParseError) as exc:
            # SearchParseError descends from Exception, not SearchClientError,
            # so catching only the latter let one unparseable leg abort the
            # whole command and discard the legs that did search cleanly --
            # the same escape already fixed for BrowserDecodeError in
            # _try_board. A route Google serves no board for raises exactly
            # this, which makes it routine rather than exotic in a
            # multi-city itinerary.
            console.print(f"  [yellow]leg search failed: {escape(str(exc))}[/yellow]")
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
        carriers=[a.name.lstrip("_") for a in (filters.airlines or [])],
        passengers=passenger_codes(filters.passenger_info) or [1],
        seat=filters.seat_type.value,
        max_stops=filters.stops.value,
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
    console.print("  Google prices the whole itinerary as one ticket here:")
    console.print(f"  [cyan]{url}[/cyan]", soft_wrap=True)
