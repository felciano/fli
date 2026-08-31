"""Tests for ``scripts/generate_enums.py``.

Two jobs here:

1. Unit-test ``_disambiguate_names``, the helper that stops duplicate human
   names from collapsing into silent ``Enum`` aliases.
2. Guard against *drift* — assert the committed ``fli/models/airport.py`` and
   ``fli/models/airline.py`` still match what the generator would produce from
   the CSVs. Without this, a generator fix that is never regenerated (or a
   hand-edited enum module) goes unnoticed.
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_enums.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_enums", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate_enums = _load_module()


def _csv_rows(filename: str, code_col: str, name_col: str) -> list[tuple[str, str]]:
    with open(REPO_ROOT / "data" / filename, encoding="utf-8", newline="") as fh:
        return [
            (row[code_col].strip().upper(), row[name_col].strip()) for row in csv.DictReader(fh)
        ]


class TestDisambiguateNames:
    def test_appends_code_to_every_member_of_a_colliding_group(self):
        entries = [("A", "X"), ("B", "X"), ("C", "Y")]
        assert generate_enums._disambiguate_names(entries) == [
            ("A", "X (A)"),
            ("B", "X (B)"),
            ("C", "Y"),
        ]

    def test_leaves_unique_names_untouched(self):
        entries = [("A", "X"), ("B", "Y"), ("C", "Z")]
        assert generate_enums._disambiguate_names(entries) == entries

    def test_empty_input_round_trips(self):
        assert generate_enums._disambiguate_names([]) == []

    def test_uses_raw_iata_code_not_sanitized_identifier(self):
        entries = [("1S", "Sabre"), ("1W", "Sabre")]
        assert generate_enums._disambiguate_names(entries) == [
            ("1S", "Sabre (1S)"),
            ("1W", "Sabre (1W)"),
        ]

    def test_real_airport_names_become_globally_unique(self):
        names = [
            name
            for _, name in generate_enums._disambiguate_names(
                _csv_rows("airports.csv", "Code", "Name")
            )
        ]
        assert len(set(names)) == len(names)

    def test_real_airline_names_become_globally_unique(self):
        names = [
            name
            for _, name in generate_enums._disambiguate_names(
                _csv_rows("airlines.csv", "IATA", "Airline")
            )
        ]
        assert len(set(names)) == len(names)


class TestCommittedEnumsMatchCsv:
    """The generated modules must not drift from the CSVs they come from."""

    def test_airport_module_matches_csv(self):
        from fli.models.airport import AIRPORT_NAMES

        entries = generate_enums._disambiguate_names(_csv_rows("airports.csv", "Code", "Name"))
        expected = {generate_enums._sanitize_code(code): name for code, name in entries}
        assert AIRPORT_NAMES == expected

    def test_airline_module_matches_csv(self):
        from fli.models.airline import AIRLINE_NAMES

        entries = _csv_rows("airlines.csv", "IATA", "Airline")
        entries.extend(
            [
                ("ONEWORLD", "Oneworld"),
                ("SKYTEAM", "SkyTeam"),
                ("STAR_ALLIANCE", "Star Alliance"),
            ]
        )
        entries = generate_enums._disambiguate_names(entries)
        expected = {
            generate_enums._sanitize_code(code, allow_digit_prefix=True): name
            for code, name in entries
        }
        assert AIRLINE_NAMES == expected
