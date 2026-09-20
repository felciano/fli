"""What :mod:`fli.search._capture` does when an intercepted body disappoints.

Interception can succeed and still leave nothing usable, in three ways that
have to stay distinguishable because they call for three different actions:

===========================  ===========================  ====================
What came back               What it means                What fli does
===========================  ===========================  ====================
No ``[2]``/``[3]`` slot      Google changed the wire      ``BrowserDecodeError``
Slot present, no rows        Google found no itineraries  empty board
Rows present, none parse     Google changed a row         ``BrowserDecodeError``
===========================  ===========================  ====================

Collapsing the first into the second is the specific bug ``flight_rows``'
``None``-versus-``[]`` contract exists to prevent: it would turn a wire-format
regression into a cheerful "no flights found" that nobody investigates. ADR 001
names the shape change as the failure it most wants reported rather than
swallowed, so each of these is asserted on the message a user would actually
read, not merely on the exception class.

Everything here is offline and deterministic. The real rows are lifted from the
committed capture so that "a row that still parses" means the same thing it
means in production.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fli.search._capture import decode_shopping_capture, merge_progressive_flights
from fli.search._decoders import flight_rows
from fli.search._wire import iter_wrb_chunks
from fli.search.exceptions import BrowserDecodeError

FIXTURE = Path(__file__).parent / "fixtures" / "flight_search_multi_city.bin"


def _wrb(*payloads: object) -> bytes:
    """Frame payloads the way a ``batchexecute`` response frames them.

    Args:
        payloads: One decoded chunk per frame, as Python objects.

    Returns:
        A body ``iter_wrb_chunks`` reads back as those chunks.

    """
    out = b")]}'\n"
    for payload in payloads:
        inner = json.dumps(payload, separators=(",", ":"))
        row = json.dumps([["wrb.fr", None, inner]], separators=(",", ":"))
        out += f"\n{len(row)}\n{row}".encode()
    return out


def _board(rows: list) -> list:
    """Wrap rows the way Google wraps them, at the ``[2]`` slot."""
    return [None, None, [rows]]


@pytest.fixture(scope="module")
def real_rows() -> list:
    """Two rows from the committed capture that genuinely parse."""
    chunk = next(iter(iter_wrb_chunks(FIXTURE.read_bytes())))
    rows = flight_rows(chunk)
    assert rows, "the committed capture must still carry rows"
    return rows[:2]


class TestShapeChange:
    """Slot absent — the wire format moved."""

    def test_it_is_an_error_and_says_the_response_decoded_fine(self):
        with pytest.raises(BrowserDecodeError) as caught:
            decode_shopping_capture(_wrb([["something", "else"]]), context="multi-city LHR→BOS")
        message = str(caught.value)
        assert "[2]/[3]" in message
        assert "row-shape change rather than a transport failure" in message

    def test_it_points_at_the_adr_reopen_trigger(self):
        """The whole point of the message: report it, do not swallow it."""
        with pytest.raises(BrowserDecodeError, match="reopen"):
            decode_shopping_capture(_wrb([None]), context="ctx")

    def test_it_names_what_was_being_searched(self):
        """A bare 'shape changed' with no subject is not a report."""
        with pytest.raises(BrowserDecodeError, match="multi-city LHR→BOS→CDG→LHR"):
            decode_shopping_capture(_wrb([None]), context="multi-city LHR→BOS→CDG→LHR")

    def test_it_quotes_the_chunk_count_and_what_the_slots_held_instead(self):
        with pytest.raises(BrowserDecodeError) as caught:
            decode_shopping_capture(_wrb([None, 1, "x"], [None, 2, "y"]), context="ctx")
        message = str(caught.value)
        assert "2 chunk(s)" in message
        assert "NoneType" in message and "str" in message

    def test_the_slot_description_stays_bounded(self):
        """A diagnosis a user has to scroll past is not a diagnosis."""
        wide = [list(range(50))] * 20
        with pytest.raises(BrowserDecodeError) as caught:
            decode_shopping_capture(_wrb(*wide), context="ctx")
        assert len(str(caught.value)) < 800

    def test_a_body_that_is_not_a_list_at_all_is_still_diagnosed(self):
        with pytest.raises(BrowserDecodeError, match=r"\[2\]/\[3\]"):
            decode_shopping_capture(_wrb({"unexpected": "object"}), context="ctx")


class TestNoItineraries:
    """Slot present and empty — Google's answer, not a failure."""

    def test_an_empty_board_is_returned_not_raised(self):
        assert decode_shopping_capture(_wrb(_board([])), context="ctx") == []

    def test_it_is_not_confused_with_a_shape_change(self):
        """The two cases must not converge; that is the contract's whole job."""
        empty = decode_shopping_capture(_wrb(_board([])), context="ctx")
        with pytest.raises(BrowserDecodeError):
            decode_shopping_capture(_wrb([None]), context="ctx")
        assert empty == []

    def test_one_empty_frame_among_full_ones_does_not_erase_the_board(self, real_rows):
        board = decode_shopping_capture(
            _wrb(_board([]), _board(real_rows), _board([])), context="ctx"
        )
        assert len(board) == len(real_rows)


class TestRowsThatNoLongerParse:
    """Rows where they belong, contents changed underneath them."""

    def test_every_row_rejected_is_an_error(self):
        with pytest.raises(BrowserDecodeError) as caught:
            decode_shopping_capture(_wrb(_board([["junk"], ["more junk"]])), context="ctx")
        assert "2 row(s) where they belong" in str(caught.value)

    def test_it_says_contents_changed_not_position(self):
        """Distinct from the shape change: different cause, different fix."""
        with pytest.raises(BrowserDecodeError, match="contents\nchanged|contents changed"):
            decode_shopping_capture(_wrb(_board([["junk"]])), context="ctx")

    def test_it_carries_the_first_failure_for_debugging(self):
        with pytest.raises(BrowserDecodeError) as caught:
            decode_shopping_capture(_wrb(_board([["junk"]])), context="ctx")
        message = str(caught.value)
        assert "First failure:" in message
        assert any(name in message for name in ("TypeError", "ValueError", "KeyError"))

    def test_a_few_bad_rows_among_good_ones_are_skipped_not_fatal(self, real_rows):
        """Google returns half-populated advert rows; they are not an outage."""
        board = decode_shopping_capture(
            _wrb(_board([real_rows[0], ["junk"], real_rows[1], [{}]])), context="ctx"
        )
        assert len(board) == 2

    def test_the_two_decode_errors_do_not_read_alike(self):
        """A support ticket has to be routable from the message alone."""
        with pytest.raises(BrowserDecodeError) as shape:
            decode_shopping_capture(_wrb([None]), context="ctx")
        with pytest.raises(BrowserDecodeError) as contents:
            decode_shopping_capture(_wrb(_board([["junk"]])), context="ctx")
        assert "[2]/[3]" in str(shape.value)
        assert "[2]/[3]" not in str(contents.value)


class TestPartialCaptureIsDiagnosable:
    """The measured cause of a board that disagrees with the website.

    Interception takes whatever frames arrived before the response closed, so
    a slow connection yields a smaller, occasionally staler board. That is not
    an error and must not raise — but a user comparing against Google and
    seeing a different price is diagnosed entirely from this log line, so its
    contents are pinned.
    """

    def test_the_debug_line_reports_frames_rows_rejected_and_itineraries(self, caplog, real_rows):
        with caplog.at_level(logging.DEBUG, logger="fli.search._capture"):
            decode_shopping_capture(
                _wrb(_board(real_rows), _board([*real_rows, ["junk"]])),
                context="multi-city LHR→BOS→CDG→LHR",
            )
        line = next(r.getMessage() for r in caplog.records if "capture for" in r.getMessage())
        assert "multi-city LHR→BOS→CDG→LHR" in line
        assert "2 frame(s)" in line
        assert "5 raw row(s)" in line
        assert "1 rejected" in line
        assert "2 itinerar(ies)" in line

    def test_a_price_revision_is_counted_so_a_stale_price_is_explainable(self, caplog):
        body = FIXTURE.read_bytes()
        with caplog.at_level(logging.DEBUG, logger="fli.search._capture"):
            decode_shopping_capture(body, context="fixture")
        revisions = [r.getMessage() for r in caplog.records if "price revision" in r.getMessage()]
        assert revisions == ["merged board: 3 price revision(s) across frames"]

    def test_a_board_with_nothing_revised_logs_no_revision_noise(self, caplog, real_rows):
        with caplog.at_level(logging.DEBUG, logger="fli.search._capture"):
            merge_progressive_flights(
                decode_shopping_capture(_wrb(_board(real_rows)), context="ctx")
            )
        assert not [r for r in caplog.records if "price revision" in r.getMessage()]

    def test_decoding_is_silent_at_the_default_level(self, caplog, real_rows):
        """Diagnosis at DEBUG, nothing at INFO — this runs per search."""
        with caplog.at_level(logging.INFO, logger="fli.search._capture"):
            decode_shopping_capture(_wrb(_board(real_rows)), context="ctx")
        assert caplog.records == []
