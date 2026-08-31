"""Regression tests for silent alias collapse in the Airport/Airline enums.

Python's ``Enum`` treats two members that share a *value* as aliases: the
second silently becomes the first. Both enums are built from a
``dict[code, human-name]`` where the human name is the member value, so any
duplicate name in ``data/airports.csv`` / ``data/airlines.csv`` used to make
one IATA code resolve to a completely different airport (``Airport.TRI`` ->
``Airport.PSC``, i.e. Bristol TN answered with Pasco WA).

These tests pin the invariant "every catalogued IATA code owns its own enum
member" so the bug class cannot silently return.
"""

from __future__ import annotations

import enum

import pytest

from fli.models.airline import AIRLINE_NAMES, Airline
from fli.models.airport import AIRPORT_NAMES, Airport

# Real-world (shadowed code, code that used to shadow it) pairs.
SHADOWED_AIRPORTS = [
    ("OKA", "NAH"),  # Naha, Okinawa vs Naha, Indonesia
    ("TRI", "PSC"),  # Tri-Cities TN vs Tri-Cities WA
    ("MLH", "BSL"),  # EuroAirport Basel-Mulhouse-Freiburg
    ("NTL", "NCL"),  # Newcastle AU vs Newcastle UK
    ("YYG", "YHG"),  # Charlottetown PE vs Charlottetown NL
    ("SMA", "AJU"),  # Santa Maria (four airports share the name)
]

SHADOWED_AIRLINES = [
    ("W9", "W6"),  # Wizz Air UK vs Wizz Air
    ("Z0", "N0"),  # Norse Atlantic Airways
]


def test_airport_enum_has_no_aliases():
    assert len(Airport.__members__) == len(list(Airport))


def test_airline_enum_has_no_aliases():
    assert len(Airline.__members__) == len(list(Airline))


def test_every_airport_code_is_its_own_member():
    offenders = [code for code in AIRPORT_NAMES if Airport[code].name != code]
    assert offenders == []


def test_every_airline_code_is_its_own_member():
    offenders = [code for code in AIRLINE_NAMES if Airline[code].name != code]
    assert offenders == []


@pytest.mark.parametrize(("shadowed", "canonical"), SHADOWED_AIRPORTS)
def test_known_shadowed_airports_resolve_to_themselves(shadowed: str, canonical: str):
    assert Airport[shadowed].name == shadowed
    assert Airport[shadowed] is not Airport[canonical]


@pytest.mark.parametrize(("shadowed", "canonical"), SHADOWED_AIRLINES)
def test_known_shadowed_airlines_resolve_to_themselves(shadowed: str, canonical: str):
    assert Airline[shadowed].name == shadowed
    assert Airline[shadowed] is not Airline[canonical]


def test_enum_unique_guard_holds():
    """``enum.unique`` raises if any member is an alias of another."""
    assert enum.unique(Airport) is Airport
    assert enum.unique(Airline) is Airline
