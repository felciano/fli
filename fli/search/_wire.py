r"""Parsing helpers for Google Flights' FlightsFrontendService wire format.

The Service returns JSONP-flavoured responses of the form::

    )]}'\n\n
    <chunk1_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    <chunk2_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    ...

`GetShoppingResults` and `GetCalendarGraph` happen to emit a single chunk so
the legacy parsers in this package could get away with `lstrip(")]}'")`.
`GetBookingResults` emits two chunks, so we need a proper multi-chunk reader.

Important quirk: the length headers are **not** a dependable frame delimiter.
They count the chunk plus its two surrounding newlines, in characters rather
than UTF-8 bytes, so any response carrying non-ASCII text (accented airport
or airline names) desynchronises a byte-oriented reader — and an ASCII-only
response hides the difference entirely.

Rather than encode a guess about Google's convention, this reader ignores the
announced length and lets the JSON grammar delimit each chunk, which is
correct under either reading. The headers are used only to re-synchronise
after a chunk that fails to parse.

The defect was invisible here until a multi-frame response was captured:
every other endpoint this package has a fixture for returns a single frame
with no length header at all, so nothing exercised the arithmetic.

This module centralises that reader and exposes :func:`iter_wrb_chunks` which
yields the decoded inner JSON of each ``wrb.fr`` chunk.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = ")]}'"

# Framing noise between two chunks: the length header and the whitespace
# around it. Skipped wholesale — the header's value is never trusted.
_FRAMING_CHARS = "0123456789 \t\r\n"

# A chunk boundary in the raw stream: newline, decimal length header,
# newline, then the "[" opening the next chunk. Literal newlines are escaped
# inside JSON strings, so this cannot match within a payload. Used only to
# re-synchronise after an unparseable chunk.
_CHUNK_BOUNDARY = re.compile(r"\n\d+\n(?=\[)")


def iter_wrb_chunks(body: str | bytes) -> Iterator[Any]:
    """Yield the inner JSON object of every ``wrb.fr`` chunk in ``body``.

    Handles both shapes Google emits: a single chunk with no length header
    (``GetShoppingResults`` / ``GetCalendarGraph`` for simple trips) and a
    length-prefixed stream of many (``GetBookingResults``, and multi-city
    searches). The announced lengths are skipped as framing noise rather than
    used as offsets — see the module docstring.
    """
    # ``errors="replace"`` keeps a corrupted transfer from raising here; the
    # affected chunk simply fails to parse and is skipped below.
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body

    text = text.lstrip()
    if text.startswith(_PREFIX):
        text = text[len(_PREFIX) :]
    text = text.lstrip()

    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        # Skip the length header and any surrounding whitespace. A
        # header-less body simply has nothing to skip.
        while cursor < len(text) and text[cursor] in _FRAMING_CHARS:
            cursor += 1
        if cursor >= len(text):
            break
        try:
            outer, cursor = decoder.raw_decode(text, cursor)
        except ValueError:
            logger.warning("Discarding malformed wrb.fr chunk", exc_info=True)
            boundary = _CHUNK_BOUNDARY.search(text, cursor)
            if boundary is None:
                break
            cursor = boundary.end()
            continue
        yield from _chunks_from_outer(outer)


def _chunks_from_outer(outer: Any) -> Iterator[Any]:
    """Walk a top-level chunk list and yield decoded inner-JSON payloads."""
    if not isinstance(outer, list):
        return
    for row in outer:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row[0] != "wrb.fr":
            continue
        inner = row[2]
        if not isinstance(inner, str) or not inner:
            # Payload-less row: Google declined the call and parked an error
            # code in slot 5. Raise instead of yielding nothing, so callers
            # don't report a hard block as "no flights on this route".
            code = row[5][0] if len(row) > 5 and isinstance(row[5], list) and row[5] else None
            if isinstance(code, int):
                from fli.search.exceptions import SearchRejectedError

                raise SearchRejectedError(code)
            continue
        try:
            yield json.loads(inner)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Failed to decode wrb.fr inner JSON payload", exc_info=True)
            continue


def parse_first_wrb_payload(body: str | bytes) -> Any:
    """Return the inner JSON of the first ``wrb.fr`` chunk, or None."""
    for chunk in iter_wrb_chunks(body):
        return chunk
    return None
