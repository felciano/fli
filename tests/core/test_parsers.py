"""Tests for core parser utilities."""

import pytest

from fli.core.parsers import (
    ParseError,
    parse_airlines,
    parse_emissions,
    parse_sort_by,
    resolve_airport,
    resolve_airports,
)
from fli.models import Airline, Airport, EmissionsFilter, SortBy
from fli.models.airport import AIRPORT_NAMES
from fli.models.icao import ICAO_TO_IATA


class TestParseEmissions:
    """Tests for parse_emissions."""

    def test_all(self):
        assert parse_emissions("ALL") == EmissionsFilter.ALL

    def test_less(self):
        assert parse_emissions("LESS") == EmissionsFilter.LESS

    def test_case_insensitive(self):
        assert parse_emissions("all") == EmissionsFilter.ALL
        assert parse_emissions("Less") == EmissionsFilter.LESS

    def test_invalid(self):
        with pytest.raises(ParseError, match="Invalid EmissionsFilter"):
            parse_emissions("NONE")


@pytest.mark.parametrize(
    "code, expected",
    [
        ("STAR_ALLIANCE", Airline.STAR_ALLIANCE),
        ("ONEWORLD", Airline.ONEWORLD),
        ("SKYTEAM", Airline.SKYTEAM),
    ],
)
def test_parse_airlines_alliance(code, expected):
    assert parse_airlines([code]) == [expected]


def test_parse_airlines_alliance_mixed_with_airlines():
    result = parse_airlines(["STAR_ALLIANCE", "AA"])
    assert Airline.STAR_ALLIANCE in result
    assert Airline.AA in result


class TestParseAirlinesSplitting:
    """Tests for parse_airlines accepting comma- and whitespace-separated codes per item.

    Motivated by the documented `--airlines BA,KL` (single token) and
    `--airlines "BA KL"` (quoted) CLI forms, plus the same tolerance now extended
    to MCP callers passing combined strings.
    """

    def test_comma_separated_in_one_item(self):
        result = parse_airlines(["BA,KL"])
        assert result == [Airline.BA, Airline.KL]

    def test_space_separated_in_one_item(self):
        result = parse_airlines(["BA KL"])
        assert result == [Airline.BA, Airline.KL]

    def test_tab_separator(self):
        result = parse_airlines(["BA\tKL"])
        assert result == [Airline.BA, Airline.KL]

    def test_collapses_consecutive_separators(self):
        result = parse_airlines(["BA,,KL", "AA  UA"])
        assert result == [Airline.BA, Airline.KL, Airline.AA, Airline.UA]

    def test_strips_leading_and_trailing_separators(self):
        result = parse_airlines([",BA,", " KL "])
        assert result == [Airline.BA, Airline.KL]

    def test_mixed_forms(self):
        result = parse_airlines(["BA,KL", "LH"])
        assert result == [Airline.BA, Airline.KL, Airline.LH]

    def test_repeated_items_still_work(self):
        # Backwards compat: `--airlines BA --airlines KL` arrives as ["BA", "KL"].
        result = parse_airlines(["BA", "KL"])
        assert result == [Airline.BA, Airline.KL]

    def test_lowercase_in_split_is_uppercased(self):
        result = parse_airlines(["ba,kl"])
        assert result == [Airline.BA, Airline.KL]

    def test_numeric_prefix_in_split(self):
        result = parse_airlines(["BA,3F"])
        assert result == [Airline.BA, Airline._3F]

    def test_invalid_code_in_split_propagates(self):
        with pytest.raises(ParseError, match="Invalid airline code: 'XXX'"):
            parse_airlines(["BA,XXX"])

    @pytest.mark.parametrize("codes", [[","], [" "], [""], ["", " ", ","]])
    def test_raises_when_no_valid_codes(self, codes):
        with pytest.raises(ParseError, match="No valid airline codes"):
            parse_airlines(codes)

    def test_none_input_still_returns_none(self):
        assert parse_airlines(None) is None

    def test_empty_list_still_returns_none(self):
        assert parse_airlines([]) is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("TOP_FLIGHTS", SortBy.TOP_FLIGHTS),
        ("BEST", SortBy.BEST),
        ("CHEAPEST", SortBy.CHEAPEST),
        ("EMISSIONS", SortBy.EMISSIONS),
    ],
)
def test_parse_sort_by(value, expected):
    assert parse_sort_by(value) == expected


def test_parse_sort_by_invalid():
    with pytest.raises(ParseError, match="Invalid sort_by value"):
        parse_sort_by("NONE")


class TestResolveAirports:
    """Tests for resolve_airports (comma-separated multi-airport parsing)."""

    def test_single_code_returns_list(self):
        assert resolve_airports("JFK") == [Airport.JFK]

    def test_multiple_codes_preserve_order(self):
        assert resolve_airports("JFK,LGA,EWR") == [Airport.JFK, Airport.LGA, Airport.EWR]

    def test_whitespace_and_case_insensitive(self):
        assert resolve_airports(" jfk , lga ") == [Airport.JFK, Airport.LGA]

    def test_blank_tokens_are_dropped(self):
        assert resolve_airports("JFK,,LGA") == [Airport.JFK, Airport.LGA]

    @pytest.mark.parametrize("codes", ["", "   ", ",,,"])
    def test_no_parsable_tokens_raises(self, codes):
        with pytest.raises(ParseError) as exc:
            resolve_airports(codes)
        assert "No valid airport codes found" in str(exc.value)

    def test_invalid_token_names_the_offender(self):
        with pytest.raises(ParseError) as exc:
            resolve_airports("JFK,XXX")
        assert "'XXX'" in str(exc.value)


class TestAirportErrorLabels:
    """The error should name which slot was at fault, not just the bad code."""

    def test_unlabelled_error_keeps_the_bare_message(self):
        with pytest.raises(ParseError, match=r"^Invalid airport code: 'XXX'$"):
            resolve_airports("XXX")

    def test_labelled_error_names_the_slot(self):
        with pytest.raises(ParseError, match=r"^Invalid origin airport code: 'XXX'$"):
            resolve_airports("JFK,XXX", label="origin")

    def test_labelled_empty_error_names_the_slot(self):
        message = r"^No valid destination airport codes found in: ',,'$"
        with pytest.raises(ParseError, match=message):
            resolve_airports(",,", label="destination")


class TestResolveAirportICAO:
    """Four-letter ICAO codes resolve to the same members as their IATA twins."""

    def test_icao_maps_to_iata_member(self):
        # Identity, not equality: proves we returned the real enum member
        # rather than constructing a lookalike.
        assert resolve_airport("KJFK") is Airport.JFK

    @pytest.mark.parametrize(
        ("icao", "expected"),
        [
            ("VTBS", Airport.BKK),
            ("EGLL", Airport.LHR),
            ("CYYZ", Airport.YYZ),
            ("SBGR", Airport.GRU),
        ],
    )
    def test_icao_non_us_prefixes(self, icao, expected):
        assert resolve_airport(icao) is expected

    def test_icao_is_case_insensitive(self):
        assert resolve_airport("kjfk") is Airport.JFK
        assert resolve_airport("KjFk") is Airport.JFK

    def test_icao_tolerates_surrounding_whitespace(self):
        assert resolve_airport(" kjfk ") is Airport.JFK

    def test_three_letter_codes_unchanged(self):
        assert resolve_airport("JFK") is Airport.JFK
        # AAA is the first enum member — guards the boundary.
        assert resolve_airport("AAA") is Airport.AAA

    def test_three_letter_error_message_is_byte_identical(self):
        # Deliberately duplicates TestAirportErrorLabels so the regression
        # guard is visible at the site of the change: the ICAO clause must
        # never leak into the generic three-letter message.
        with pytest.raises(ParseError, match=r"^Invalid airport code: 'XXX'$"):
            resolve_airport("XXX")
        with pytest.raises(ParseError, match=r"^Invalid origin airport code: 'XXX'$"):
            resolve_airport("XXX", label="origin")

    def test_unknown_four_letter_code_says_icao_and_partial_coverage(self):
        with pytest.raises(ParseError) as exc:
            resolve_airport("ZZZZ")
        message = str(exc.value)
        assert "Invalid airport code: 'ZZZZ'" in message
        assert "ICAO" in message
        assert "IATA" in message

    def test_unknown_icao_error_carries_the_label(self):
        with pytest.raises(ParseError) as exc:
            resolve_airport("ZZZZ", label="destination")
        message = str(exc.value)
        # Keeps the existing "Invalid <slot> airport code" prefix that CLI and
        # MCP callers already match on, and explains the ICAO dispatch after it.
        assert message.startswith("Invalid destination airport code: 'ZZZZ'")
        assert "ICAO" in message

    @pytest.mark.parametrize("code", ["JF-K", "JFK1"])
    def test_four_letter_non_alpha_falls_through_to_iata_error(self, code):
        with pytest.raises(ParseError) as exc:
            resolve_airport(code)
        message = str(exc.value)
        assert message == f"Invalid airport code: '{code}'"
        assert "ICAO" not in message

    def test_no_airport_member_name_is_four_characters(self):
        # The whole dispatch rests on this: if an airports.csv refresh ever
        # adds a four-character code it must fail here loudly rather than
        # silently route that code into the ICAO table.
        assert [code for code in AIRPORT_NAMES if len(code) == 4] == []

    def test_every_icao_target_resolves(self):
        for icao, iata in ICAO_TO_IATA.items():
            assert resolve_airport(icao).name == iata


class TestResolveAirportsICAO:
    """The multi-airport slot inherits ICAO support by delegation."""

    def test_resolve_airports_accepts_icao(self):
        assert resolve_airports("KJFK,KLAX") == [Airport.JFK, Airport.LAX]

    def test_resolve_airports_mixes_icao_and_iata(self):
        assert resolve_airports(" kjfk , LGA ") == [Airport.JFK, Airport.LGA]

    def test_resolve_airports_bad_icao_names_the_slot(self):
        with pytest.raises(ParseError) as exc:
            resolve_airports("KJFK,ZZZZ", label="origin")
        assert "Invalid origin airport code: 'ZZZZ'" in str(exc.value)


class TestICAODataCorrectness:
    """Guard the mapping's data, not just its plumbing.

    A wrong target is the worst failure mode here: it resolves successfully
    and silently searches the wrong airport, which is exactly the bug class
    the enum de-aliasing work removed.
    """

    def test_rand_airport_is_keyed_by_its_own_icao(self):
        """Rand Airport is FAGM. FAJS is a retired O. R. Tambo code."""
        assert resolve_airport("FAGM") is Airport.QRA

    def test_retired_or_tambo_code_does_not_resolve_to_a_different_airport(self):
        """FAJS must never silently mean Rand Airport."""
        with pytest.raises(ParseError):
            resolve_airport("FAJS")

    def test_or_tambo_resolves_from_its_current_icao(self):
        assert resolve_airport("FAOR") is Airport.JNB
