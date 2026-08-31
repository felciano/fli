#!/usr/bin/env python3
"""Script to generate Airport and Airline enums from CSV data files.

This script reads airport and airline data from CSV files and generates
corresponding Python ``Enum`` classes. The generated enums are used
throughout the application to ensure consistent handling of airport and
airline codes.

The script expects CSV files in the following locations:
- ``data/airports.csv``: Contains airport codes and names
- ``data/airlines.csv``: Contains airline IATA codes and names
- ``data/icao_to_iata.csv``: Maps 4-letter ICAO codes to IATA codes

The generated files are written to:
- ``fli/models/airport.py``: Contains the ``Airport`` enum
- ``fli/models/airline.py``: Contains the ``Airline`` enum
- ``fli/models/icao.py``: Contains the ``ICAO_TO_IATA`` mapping

Output format
-------------

We emit a single dict literal containing every (code → human name) pair
and construct the ``Enum`` programmatically via ``Enum(name, mapping)``.
This is dramatically faster to import than the previous per-member
``class`` body — a 7,883-member ``Airport`` enum import drops from
~310ms to ~70ms because Python parses one dict literal instead of
running 7,883 metaclass-driven attribute assignments. The runtime API
(``Airport.JFK``, ``isinstance``, Pydantic compat, ``__members__``,
iteration) is identical to the previous form.

Duplicate names
---------------

Because the dict *value* is the Enum member value, two codes sharing a
human-readable name would become silent ``Enum`` **aliases** — the second
code would resolve to the first member (``Airport.TRI is Airport.PSC``),
so a search for Bristol TN would confidently return Pasco WA results.
:func:`_disambiguate_names` therefore appends `` (CODE)`` to every member
of a colliding name group, and the generated module wraps the enum in
:func:`enum.unique` so a surviving alias fails loudly at import time
instead of silently returning the wrong airport.

The `` (CODE)`` suffix is deliberately **Python-only**. ``fli-js`` (see
``fli-js/scripts/generate-enums.ts``) keys its enum by IATA code with the
names in a separate record, so it is structurally immune to aliasing and
must NOT receive the suffix — the two ``AIRPORT_NAMES`` maps differ for
the colliding entries on purpose.
"""

import csv
from collections import Counter
from pathlib import Path

PROJECT_DIR = Path(__file__).parents[1].resolve()


def _disambiguate_names(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Append IATA codes to duplicate names so every Enum value is unique.

    Python's ``Enum`` treats members with the same value as aliases — the
    second silently becomes an alias for the first. This function detects
    duplicate human-readable names and appends `` (CODE)`` to *all*
    members of each duplicate group (including the first) so every entry
    gets its own distinct Enum member. Suffixing the whole group (rather
    than only the later members) keeps the output independent of CSV row
    order.

    Entries with unique names are left untouched. The raw IATA code is
    used, not the sanitised Python identifier, so a digit-prefixed
    airline renders as ``"Sabre (1S)"`` rather than ``"Sabre (_1S)"``.

    Args:
        entries: ``(iata_code, human_name)`` pairs in CSV order.

    Returns:
        The same pairs with colliding names made unique.

    Raises:
        ValueError: If names are still not unique afterwards — e.g. a
            source name that already ends in a `` (XXX)`` suffix
            colliding with a generated one. Failing at generation time
            beats re-introducing a silent alias.

    """
    name_counts = Counter(name for _, name in entries)
    duplicates = {name for name, count in name_counts.items() if count > 1}
    if not duplicates:
        return entries

    result = [(code, f"{name} ({code})" if name in duplicates else name) for code, name in entries]

    still_duplicated = sorted(
        name for name, count in Counter(name for _, name in result).items() if count > 1
    )
    if still_duplicated:
        raise ValueError(
            "Names remain ambiguous after disambiguation — fix the source CSV: "
            + ", ".join(still_duplicated)
        )
    return result


def _sanitize_code(code: str, allow_digit_prefix: bool = False) -> str:
    """Return a valid Python identifier for an IATA code.

    Airline codes that start with a digit (e.g. ``3F``) are prefixed
    with an underscore to keep them Python-legal. Airport codes never
    start with a digit, so the prefix is gated on the caller.
    """
    sanitized = "".join(c if c.isalnum() else "_" for c in code)
    if allow_digit_prefix and sanitized and sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized


def _write_enum_module(
    output_path: Path,
    enum_name: str,
    doc: str,
    source_csv: str,
    entries: list[tuple[str, str]],
) -> None:
    """Write a Python module that defines ``enum_name`` from a dict literal.

    The generated module exposes two public names at module scope:

    * ``<enum_name>`` — the ``Enum`` class users import (e.g. ``Airport``).
      Identical public surface to a class-body-defined Enum: attribute
      access, value lookup, isinstance checks, Pydantic field typing,
      and iteration all behave the same way. Built through the
      :class:`enum.Enum` functional API so the metaclass only walks the
      mapping once at module load instead of once per member, cutting
      import time ~5x for a 7,883-entry enum.

    * ``<ENUM_NAME>_NAMES`` — the underlying ``dict[str, str]`` of
      ``IATA-code → human-readable-name``. Public so callers that want
      raw dict semantics (cheap ``in`` checks, iteration without paying
      Enum-member overhead, JSON dumping) can use it directly. The
      ``Enum`` stays the canonical type for typed APIs; the dict is the
      fast path.

    The enum is wrapped in :func:`enum.unique`, which raises
    ``ValueError`` at *import* time if any two members share a value.
    That is a deliberate sharp edge: hand-editing the generated dict, or
    refreshing ``data/airports.csv`` without re-running this script, will
    break ``import fli`` outright rather than silently resolving one IATA
    code to a different airport. The supported path — running this
    script — always disambiguates first, so it cannot fire from normal
    use.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_name = f"{enum_name.upper()}_NAMES"
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(f'"""{doc}\n\n')
        fh.write(f"Auto-generated from {source_csv}.\n\n")
        fh.write("Exports:\n\n")
        fh.write(
            f"* :data:`{enum_name}` — the ``Enum`` class for typed APIs.\n"
            f"* :data:`{map_name}` — the underlying ``dict[code, name]``\n"
            f"  for callers that want raw dict speed.\n"
        )
        fh.write('"""\n\n')
        fh.write("from enum import Enum, unique\n\n")
        fh.write(
            "# A single dict literal — Python parses this in one pass.\n"
            "# Defining the same data as ``class <Enum>(Enum):`` members\n"
            "# costs one metaclass call per member; ``Enum(name, mapping)``\n"
            "# below walks the dict once instead.\n"
        )
        fh.write(f"{map_name}: dict[str, str] = {{\n")
        for code, name in entries:
            sanitized = _sanitize_code(code, allow_digit_prefix=(enum_name == "Airline"))
            # Use repr() for proper escaping of any quotes/backslashes.
            fh.write(f"    {sanitized!r}: {name!r},\n")
        fh.write("}\n\n")
        fh.write(
            "# ``unique`` rejects alias members: two codes sharing a value\n"
            "# would make one silently resolve to the other. Regenerate with\n"
            "# ``make generate-enums`` rather than editing this dict by hand.\n"
        )
        fh.write(
            f"{enum_name} = unique(Enum({enum_name!r}, {map_name}))\n"
            f'{enum_name}.__doc__ = """{doc}"""\n'
        )


def generate_airport_enum() -> None:
    """Generate ``Airport`` enum from ``data/airports.csv``."""
    csv_path = PROJECT_DIR / "data" / "airports.csv"
    out_path = PROJECT_DIR / "fli" / "models" / "airport.py"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    try:
        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            entries = [(row["Code"].strip().upper(), row["Name"].strip()) for row in reader]
    except (KeyError, csv.Error) as e:
        raise ValueError(f"Error reading CSV file: {e}") from e

    entries = _disambiguate_names(entries)
    _write_enum_module(
        out_path,
        enum_name="Airport",
        doc="Airport IATA codes.",
        source_csv="data/airports.csv",
        entries=entries,
    )
    print(f"Generated {len(entries)} Airport members in {out_path}")


def generate_airline_enum() -> None:
    """Generate ``Airline`` enum from ``data/airlines.csv``.

    Three manual aliases — ``ONEWORLD``, ``SKYTEAM``, ``STAR_ALLIANCE`` —
    are appended after the CSV-derived entries. Those values are not
    actual IATA airline codes, but Google's flight-search API accepts
    them in the ``airlines`` filter as alliance pseudo-codes and the
    parser surfaces them as :class:`Airline` members. Keep them in sync
    with :data:`fli.models.google_flights.base.Alliance`.
    """
    csv_path = PROJECT_DIR / "data" / "airlines.csv"
    out_path = PROJECT_DIR / "fli" / "models" / "airline.py"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    try:
        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            entries = [(row["IATA"].strip().upper(), row["Airline"].strip()) for row in reader]
    except (KeyError, csv.Error) as e:
        raise ValueError(f"Error reading CSV file: {e}") from e

    # Append alliance pseudo-codes. Position matters only for
    # iteration order — value-based lookups (``Airline("Oneworld")``)
    # work regardless.
    entries.extend(
        [
            ("ONEWORLD", "Oneworld"),
            ("SKYTEAM", "SkyTeam"),
            ("STAR_ALLIANCE", "Star Alliance"),
        ]
    )

    entries = _disambiguate_names(entries)
    _write_enum_module(
        out_path,
        enum_name="Airline",
        doc="Airline IATA codes.",
        source_csv="data/airlines.csv",
        entries=entries,
    )
    print(f"Generated {len(entries)} Airline members in {out_path}")


def _validate_icao_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate ICAO→IATA pairs, raising rather than emitting a broken table.

    Generating a Python module (instead of reading the CSV at runtime) buys
    exactly this: a typo'd target is caught here, at generation time, rather
    than surfacing to a user as a nonsensical ``Invalid airport code: 'JFX'``
    for the input ``KJFK``.

    Args:
        rows: ``(icao_code, iata_code)`` pairs in CSV order.

    Returns:
        The same rows, unchanged, when every check passes.

    Raises:
        ValueError: If a key is not four letters, a key repeats, or an IATA
            target is not a member of the generated ``Airport`` enum.

    """
    from fli.models.airport import AIRPORT_NAMES

    bad_keys = sorted({icao for icao, _ in rows if not (len(icao) == 4 and icao.isalpha())})
    if bad_keys:
        raise ValueError(
            "ICAO keys must be exactly four letters — fix data/icao_to_iata.csv: "
            + ", ".join(bad_keys)
        )

    duplicates = sorted(
        {icao for icao, count in Counter(icao for icao, _ in rows).items() if count > 1}
    )
    if duplicates:
        raise ValueError(
            "Duplicate ICAO keys — fix data/icao_to_iata.csv: " + ", ".join(duplicates)
        )

    unknown = sorted({iata for _, iata in rows if iata not in AIRPORT_NAMES})
    if unknown:
        raise ValueError(
            "ICAO target is not an Airport code — fix data/icao_to_iata.csv: " + ", ".join(unknown)
        )

    return rows


def generate_icao_map() -> None:
    """Generate ``ICAO_TO_IATA`` in ``fli/models/icao.py`` from the CSV.

    Emitted as a plain ``dict`` rather than an ``Enum``: nothing here is a
    typed domain value, it is a lookup consulted once inside
    :func:`fli.core.parsers.resolve_airport` before the real ``Airport``
    lookup runs.
    """
    csv_path = PROJECT_DIR / "data" / "icao_to_iata.csv"
    out_path = PROJECT_DIR / "fli" / "models" / "icao.py"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    try:
        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = [
                (row["ICAO"].strip().upper(), row["IATA"].strip().upper())
                for row in reader
                if row["ICAO"].strip()
            ]
    except (KeyError, csv.Error) as e:
        raise ValueError(f"Error reading CSV file: {e}") from e

    rows = _validate_icao_rows(rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write('"""ICAO (4-letter) to IATA (3-letter) airport code mapping.\n\n')
        fh.write("Auto-generated from data/icao_to_iata.csv — do not hand-edit;\n")
        fh.write("run ``make generate-enums`` instead.\n\n")
        fh.write(
            "Coverage is a curated subset of major airports, not the full ICAO\n"
            "register: every key here resolves, but a valid ICAO code missing\n"
            "from the table is a coverage gap rather than an unknown airport.\n"
            "Every value is guaranteed to be a member name of\n"
            ":class:`fli.models.airport.Airport` — the generator refuses to\n"
            "emit a target it cannot find there.\n"
        )
        fh.write('"""\n\n')
        fh.write("ICAO_TO_IATA: dict[str, str] = {\n")
        for icao, iata in rows:
            fh.write(f"    {icao!r}: {iata!r},\n")
        fh.write("}\n")
    print(f"Generated {len(rows)} ICAO mappings in {out_path}")


if __name__ == "__main__":
    generate_airport_enum()
    generate_airline_enum()
    generate_icao_map()
