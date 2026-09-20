"""The decode half of the browser transport, offline against a real capture.

``tests/search/fixtures/flight_search_multi_city.bin`` is a verbatim
``GetShoppingResults`` body intercepted in a browser. The point of these tests
is that it needs **no new decoder**: the same
``iter_wrb_chunks`` → ``flight_rows`` → ``parse_flight_row`` pipeline that
serves the HTTP path reads it unchanged. If that ever stops being true, this
is where it shows up, and it shows up without a network call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fli.search._decoders import flight_rows, parse_flight_row
from fli.search._wire import iter_wrb_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "flight_search_multi_city.bin"


@pytest.fixture(scope="module")
def captured_body() -> bytes:
    return FIXTURE.read_bytes()


class TestCapturedMultiCityBody:
    def test_pipeline_reads_an_intercepted_body_unchanged(self, captured_body):
        """The whole bargain of intercepting rather than scraping."""
        chunks = list(iter_wrb_chunks(captured_body))
        assert len(chunks) == 9

        rows = [row for chunk in chunks for row in (flight_rows(chunk) or [])]
        assert len(rows) == 83

        flights = [parse_flight_row(row) for row in rows]
        assert len(flights) == 83, "every row decodes; no rejects"

    def test_every_chunk_carries_rows_in_the_known_shape(self, captured_body):
        """``shape=known``: multi-city reuses the one-way row positions.

        This closes ADR 001's central open risk for the first leg. A ``None``
        here would mean the row shape changed and the structural walk in the
        prototype would have to come back.
        """
        for index, chunk in enumerate(iter_wrb_chunks(captured_body)):
            assert flight_rows(chunk) is not None, f"chunk {index} lost the [2]/[3] shape"

    def test_frames_are_progressive_resends_of_one_growing_board(self, captured_body):
        """83 rows is 11 itineraries re-sent, not 83 distinct options.

        Measured, and the reason a chunk-merge rule is needed at all: the
        frames are snapshots of one board filling in, and later frames revise
        prices *downward*. Concatenating them without merging reports the same
        itinerary many times, at a stale price.
        """
        flights = [
            parse_flight_row(row)
            for chunk in iter_wrb_chunks(captured_body)
            for row in (flight_rows(chunk) or [])
        ]

        def identity(flight):
            return tuple(
                (leg.airline, leg.flight_number, leg.departure_datetime, leg.arrival_datetime)
                for leg in flight.legs
            )

        by_legs = {identity(flight) for flight in flights}
        by_legs_and_price = {(identity(f), f.price) for f in flights}

        assert len(flights) == 83
        assert len(by_legs) == 11, "11 distinct itineraries"
        assert len(by_legs_and_price) == 14, "3 of them were re-priced mid-capture"

    def test_a_revised_price_is_the_later_one(self, captured_body):
        """Last write wins, because the freshest price is the right one."""
        seen: dict[tuple, float | None] = {}
        first_seen: dict[tuple, float | None] = {}
        for chunk in iter_wrb_chunks(captured_body):
            for row in flight_rows(chunk) or []:
                flight = parse_flight_row(row)
                key = tuple(
                    (leg.airline, leg.flight_number, leg.departure_datetime) for leg in flight.legs
                )
                first_seen.setdefault(key, flight.price)
                seen[key] = flight.price

        revised = {k: (first_seen[k], v) for k, v in seen.items() if first_seen[k] != v}
        assert revised, "the fixture is the one that captured a re-price"
        for before, after in revised.values():
            assert after < before, "every revision in this capture is downward"

        assert min(price for price in seen.values() if price is not None) == 1394.0

    def test_every_row_is_leg_one_only(self, captured_body):
        """The board is first-leg options, not complete itineraries.

        Multi-leg rows here are *connections within leg 1* (LHR-KEF-BOS), not
        the second leg of the trip. Whatever ships on top of this transport
        must not present these as priced itineraries.
        """
        flights = [
            parse_flight_row(row)
            for chunk in iter_wrb_chunks(captured_body)
            for row in (flight_rows(chunk) or [])
        ]
        origins = {flight.legs[0].departure_airport.name for flight in flights}
        destinations = {flight.legs[-1].arrival_airport.name for flight in flights}
        dates = {flight.legs[0].departure_datetime.date().isoformat() for flight in flights}

        assert origins == {"LHR"}
        assert destinations == {"BOS"}
        assert dates == {"2026-10-16"}


class TestFlightRowsContract:
    """``None`` means the shape changed; ``[]`` means Google found nothing.

    Collapsing the two turns a wire-format regression into a quiet "no
    flights found", which is the bug this distinction exists to prevent.
    """

    def test_absent_slots_return_none(self):
        assert flight_rows([None, None]) is None
        assert flight_rows([None, None, None, None]) is None
        assert flight_rows("not a list") is None
        assert flight_rows([]) is None

    def test_present_but_empty_slot_returns_an_empty_list(self):
        assert flight_rows([None, None, [[]], None]) == []

    def test_both_slots_are_concatenated_in_order(self):
        rows = flight_rows([None, None, [["a", "b"]], [["c"]]])
        assert rows == ["a", "b", "c"]

    def test_one_slot_is_enough(self):
        assert flight_rows([None, None, None, [["only"]]]) == ["only"]
