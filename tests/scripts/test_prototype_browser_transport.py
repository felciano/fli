"""Offline coverage for the browser-transport prototype (ADR 001).

The prototype's browser half cannot run here — the development machine
sandboxes Chromium. Everything else can, and does:

* the ``tfs`` builder is pinned against fli's own ``encode_tfs_payload`` for
  the trip types both can express, so "field 19 = 3, nothing else changed" is
  an assertion rather than a claim;
* the decode path is replayed against the real captured ``GetShoppingResults``
  bodies already under ``tests/search/fixtures/``, which is the same evidence
  ``test_snapshot_fixtures.py`` uses;
* the structural fallback is fed a body whose rows sit somewhere other than
  ``[2]``/``[3]``, standing in for the multi-city shape the ADR flags as its
  central open risk.

Importing the module must not require playwright. That is asserted too.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from fli.search._proto import encode_tfs_payload, encode_tfs_segment

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import prototype_browser_transport as proto  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "search" / "fixtures"
ONE_WAY_FIXTURE = FIXTURE_DIR / "flight_search_jfk_lax_oneway_usd.bin"

LEGS = [
    proto.MultiCityLeg("JFK", "LHR", "2026-11-02"),
    proto.MultiCityLeg("LHR", "CDG", "2026-11-09"),
    proto.MultiCityLeg("CDG", "JFK", "2026-11-16"),
]


def _tfs_bytes(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trip_type", "is_one_way"),
    [(proto.TRIP_TYPE_ROUND_TRIP, False), (proto.TRIP_TYPE_ONE_WAY, True)],
)
def test_envelope_matches_fli_encoder_for_expressible_trip_types(trip_type, is_one_way):
    segments = encode_tfs_segment("JFK", "LHR", "2026-11-02")
    assert proto._encode_tfs_envelope(segments, trip_type=trip_type) == encode_tfs_payload(
        segments, is_one_way=is_one_way
    )


def test_multi_city_envelope_differs_only_in_field_19():
    segments = b"".join(encode_tfs_segment(leg.origin, leg.destination, leg.date) for leg in LEGS)
    multi = _tfs_bytes(proto._encode_tfs_envelope(segments, trip_type=proto.TRIP_TYPE_MULTI_CITY))
    one_way = _tfs_bytes(encode_tfs_payload(segments, is_one_way=True))

    # Field 19, varint: tag byte 0x98 0x01 then the value.
    assert multi[:-1] == one_way[:-1]
    assert multi[-3:] == b"\x98\x01\x03"
    assert one_way[-3:] == b"\x98\x01\x02"


def test_multi_city_tfs_carries_every_leg():
    token = _tfs_bytes(proto.build_multi_city_tfs(LEGS))
    for leg in LEGS:
        assert leg.origin.encode() in token
        assert leg.destination.encode() in token
        assert leg.date.encode() in token


def test_multi_city_url_is_a_search_page_url():
    url = proto.multi_city_url(LEGS, currency="USD", language="en", country="US")
    assert url.startswith("https://www.google.com/travel/flights?tfs=")
    assert "curr=USD" in url and "hl=en" in url and "gl=US" in url


def test_multi_city_needs_two_legs():
    with pytest.raises(ValueError, match="at least two legs"):
        proto.build_multi_city_tfs(LEGS[:1])


@pytest.mark.parametrize("spec", ["JFK-LHR-2026-11-02", "JFK:LHR", "JFK::2026-11-02"])
def test_parse_leg_rejects_malformed_specs(spec):
    with pytest.raises(Exception, match="ORIGIN:DEST"):
        proto.parse_leg(spec)


def test_parse_leg_uppercases_codes():
    assert proto.parse_leg("jfk:lhr:2026-11-02") == proto.MultiCityLeg("JFK", "LHR", "2026-11-02")


# ---------------------------------------------------------------------------
# Decode path, against real captured bodies
# ---------------------------------------------------------------------------


def test_decodes_a_real_captured_shopping_body():
    report = proto.parse_shopping_response_detailed(ONE_WAY_FIXTURE.read_bytes())

    assert report.shape == "known"
    assert report.chunks == 1
    assert report.flights, "expected the captured fixture to decode into itineraries"
    assert report.notes == []

    flight = report.flights[0]
    assert flight.legs
    assert flight.duration > 0
    assert flight.legs[0].flight_number


def test_parse_shopping_response_accepts_text_and_bytes():
    raw = ONE_WAY_FIXTURE.read_bytes()
    assert len(proto.parse_shopping_response(raw)) == len(
        proto.parse_shopping_response(raw.decode("utf-8"))
    )


def test_empty_body_decodes_to_nothing():
    report = proto.parse_shopping_response_detailed(b"")
    assert report.flights == []
    assert report.shape == "none"


def _wrap_as_wrb(inner: object) -> bytes:
    """Re-wrap a payload in the ``wrb.fr`` envelope Google returns."""
    return (")]}'\n\n" + json.dumps([["wrb.fr", None, json.dumps(inner)]])).encode("utf-8")


def test_structural_fallback_finds_rows_the_known_shape_would_miss():
    """Stand-in for the unverified multi-city shape: rows nested per leg."""
    body = ONE_WAY_FIXTURE.read_bytes()
    known = proto.parse_shopping_response_detailed(body)
    rows = [
        row
        for chunk in proto.iter_wrb_chunks(body)
        for row in (proto._rows_known_shape(chunk) or [])
    ]
    assert rows, "fixture precondition: the known shape yields rows"

    # A layout the [2]/[3] reader cannot see: a board per leg, three levels down.
    # Relocate EVERY row, not a 4-row excerpt, and assert full recall rather
    # than an upper bound. `<= len(known.flights)` held at any recall including
    # near-zero, which is what let a walk that found 5 of 28 rows pass as
    # working instrumentation.
    half = len(rows) // 2
    relocated = {"legs": [{"board": [rows[:half]]}, {"board": [rows[half:]]}]}
    report = proto.parse_shopping_response_detailed(_wrap_as_wrb(relocated))

    assert report.shape == "structural"
    assert any("fell back to walking the payload" in note for note in report.notes)
    # Every row the known shape decodes must survive the walk. Wrapper nodes
    # also satisfy the heuristic and are rejected by parse_flight_row, so the
    # walk may see more candidates than there are rows — but never fewer
    # flights.
    assert len(report.flights) == len(known.flights), (
        f"structural walk recall dropped: {len(report.flights)} of {len(known.flights)}"
    )


def test_report_notes_when_every_candidate_row_is_rejected():
    # Row-shaped enough for the heuristic, junk to the decoder.
    fake_row = [["x"] * 10]
    fake_row[0][2] = [["y"] * 25]
    report = proto.parse_shopping_response_detailed(_wrap_as_wrb([None, None, [[fake_row]]]))

    assert report.flights == []
    assert report.rows_seen == 1
    assert report.rows_failed == 1
    assert any("rejected by parse_flight_row" in note for note in report.notes)


# ---------------------------------------------------------------------------
# CLI and dependency posture
# ---------------------------------------------------------------------------


def test_module_imports_without_playwright():
    assert "playwright" not in sys.modules


def test_print_url_needs_no_browser(capsys):
    argv = ["--print-url", "--leg", "JFK:LHR:2026-11-02", "--leg", "LHR:JFK:2026-11-09"]
    assert proto.main(argv) == 0
    assert capsys.readouterr().out.strip().startswith("https://www.google.com/travel/flights?tfs=")


def test_parse_file_mode_decodes_the_fixture(capsys):
    assert proto.main(["--parse-file", str(ONE_WAY_FIXTURE), "--max-results", "3"]) == 0
    out = capsys.readouterr().out
    assert "shape=known" in out
    assert "flights=" in out


def test_missing_legs_is_a_usage_error():
    with pytest.raises(SystemExit):
        proto.main(["--print-url", "--leg", "JFK:LHR:2026-11-02"])
