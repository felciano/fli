# 001 — Add an optional browser-backed transport for requests the HTTP transport cannot serve

**Status:** Proposed. Blocked on the two unverified items in *Open risks* below —
the shape of a multi-city `GetShoppingResults` response, and whether headless
Chrome clears Google's attestation — and on the dependency question in
*Consequences*, which is a maintainer call rather than a technical one.
**Date:** 2026-09-18.

---

## Context

Since 2026-08, Google's `GetShoppingResults` RPC has been gated behind an
attestation header (upstream issue #223). Fli's response was to move its search
transport to the public search page: fetch the HTML with the rate-limited
`curl-cffi` client, pull the inlined `ds:1` blob out with `extract_payload`
(`fli/search/_tfs.py:197`), and hand it to the existing decoders. That works for
one-way and round-trip search and for the date sweep. Booking options were
*not* moved: `SearchFlights.get_booking_options` still POSTs to
`BOOKING_URL` (`GetBookingResults`), the same `FlightsFrontendService`
family whose gating is this record's premise. It works today because only
`GetShoppingResults` and `GetCalendarGraph` are gated — not because it is
on a different transport.

It does not work for multi-city, and `fli` says so rather than guessing:
`build_tfs` raises `SearchUnsupportedError` for `TripType.MULTI_CITY`
(`fli/search/_tfs.py:137`). The comment above the raise explains why field 19 —
the trip type — cannot simply be set: sending `2` (one-way) makes Google ignore
every segment past the first and serve the first leg's one-way board, which
decodes cleanly into wrong results; sending `3` renders the right board in a
browser but inlines no flight rows.

That comment is accurate today, not stale. Fetching the multi-city search page
server-side through fli's own client and running `extract_payload` over it gives:

| Request | Payload elements | `payload[2]` | `payload[3]` |
|---|---|---|---|
| One-way (field 19 = 2) | 32 | list, 4 rows | list, 10 rows |
| Multi-city (field 19 = 3) | 24 | `None` | `None` |

No carrier name and no price from the rendered board appears anywhere in the
1.9 MB of returned HTML. The board *scaffolding* is server-rendered; the rows are
not. There is nothing on that page to parse, so no amount of decoder work on the
HTTP path can recover them.

Three further things were established by direct observation, and they are what
makes a browser-backed path look cheap rather than speculative:

1. **Fli can already construct the request.** A `tfs` token built with fli's own
   `encode_tfs_segment` and field 19 set to `3` produced a working Google Flights
   multi-city board in a real browser, showing all four legs of a test itinerary
   priced "entire trip" from $1,395. The encoder is correct; only the read-back
   is missing.
2. **The client-side call is the endpoint fli was originally built on.** Hooking
   `fetch`/`XMLHttpRequest` in the page and triggering a re-search captured
   `POST /_/FlightsFrontendUi/browserinfo` immediately followed by
   `POST /_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetShoppingResults`.
   The second is character-for-character `SearchFlights.BASE_URL`
   (`fli/search/flights.py:100`). The `browserinfo` call ahead of it is almost
   certainly the attestation step that produces the
   `x-goog-batchexecute-bgr` signature.
3. **Fli already owns the decoder for that response.** `iter_wrb_chunks`
   (`fli/search/_wire.py`) documents handling "the older `GetShoppingResults` /
   `GetCalendarGraph` shape"; `parse_flight_row` and `parse_booking_chunk`
   (`fli/search/_decoders.py`) were written against it; `tests/search/fixtures/*.bin`
   are real captured `GetShoppingResults` bodies (`scripts/capture_fixtures.py`
   POSTs to `SearchFlights.BASE_URL`), replayed offline by
   `tests/search/test_snapshot_fixtures.py`. The parsing half of a browser-backed
   transport is already built and already covered by offline tests — **for the
one-way and round-trip row shape**. Every fixture in
`tests/search/fixtures/` is a one-way, round-trip or booking capture; none
is multi-city, and `scripts/capture_fixtures.py` has no multi-city case. So
this reads on the evidence available, not on multi-city, which is the open
risk recorded below. Do not read this paragraph as saying only transport
work remains.

Reading the rendered DOM was also tried, and yielded 11 fully structured options
(times, carriers, stops, layover airports, CO₂, entire-trip price) — so a second,
weaker extraction route exists.

Multi-city is the concrete case. Upstream #226 (Explore) is a second: it is
behind the same gate, verified rather than assumed — see *Correction, twice
over* below, which also records how this was briefly and wrongly recorded as
settled the other way.

## Decision

Keep the search-page HTTP transport as the **default and only** path for every
request it can serve — one-way, round-trip, date sweeps, and booking
options (which remain on the RPC, ungated so far).

Add a **browser-backed transport as an optional, separately installed path**,
selected only for requests the HTTP transport provably cannot serve. Today that
is multi-city search.

That is the only *whole request class* the HTTP transport must refuse, but
it is not the only thing it cannot honour. `_UNSUPPORTED` in
`fli/search/_tfs.py:86` names three filters — `emissions`, `bags`,
`exclude_basic_economy` — that it drops with a warning rather than
encoding, and a browser could in principle set those toggles. Routing on
trip type alone therefore under-serves an installed extra. Widening the
seam to those filters is deliberately **not** decided here: it trades a
seconds-per-request path for three filters the HTTP path degrades
gracefully on, and should be its own record if anyone wants it.

Within that transport, prefer **intercepting the in-page `GetShoppingResults`
response** and feeding its body to the existing `_wire` / `_decoders` pipeline.
Reading the rendered DOM is the fallback, behind the same interface, used only if
interception proves unworkable.

The browser driver is not added to the base dependencies. Absent the extra,
multi-city continues to raise `SearchUnsupportedError`, with the message
extended to name the optional path.

## Rationale / alternatives rejected

**Do nothing; keep refusing multi-city.** The cheapest option and the one in
force. Rejected because the board demonstrably exists, fli can already build the
request that produces it, and the answer is being thrown away at the read step —
this is a capability gap in fli, not a limit on the data. It stays the right
answer if the open risks below resolve badly.

**Decode multi-city out of the search page HTML.** Refuted by measurement, not
argument: `payload[2]` and `payload[3]` are `None`, the payload is 24 elements
against one-way's 32, and no row content appears in 1.9 MB of HTML.

**Call `GetShoppingResults` directly from Python, reproducing the attestation.**
This is the arms race in its most brittle form: reimplementing the
`x-goog-batchexecute-bgr` signature means tracking whatever Google's
`browserinfo` step computes, and breaking on every rotation. It is also precisely
what the gate exists to prevent. A browser produces that signature as a side
effect of being a browser, which is a far more stable position to hold.

**Split a multi-city trip into per-leg one-way searches.** This is the documented
workaround today and it stays available, but it answers a different question.
Google prices a multi-city itinerary as one trip — the test itinerary quoted
"entire trip" from $1,395 — so a sum of per-leg fares is not the same product and
can be materially wrong in either direction.

**Replace the HTTP transport with a browser everywhere.** Rejected on latency and
on dependency weight; see *Consequences*. A page load per date would make the
date sweep, whose whole design assumes cheap fetches at 10 req/sec, unusable.

**Make DOM scraping the primary browser strategy.** It works — 11 structured
options were read from the rendered board — but it discards the decoders and
fixtures fli already has, and puts fli's correctness at the mercy of Google's
front-end markup, which changes far more often than its wire format. Kept as the
fallback, not the plan.

## Consequences

**A browser dependency lands against a library whose appeal is `uv add flights`.**
A driver plus a browser binary is hundreds of megabytes against a package that is
currently a pure HTTP client. `docs/index.md` and `README.md:14` both sell fli as "no scraping, no
browser automation, no HTML parsing". The "no HTML parsing" half is already
untrue on the default path — `extract_payload` regex-scans ~1.9 MB of
rendered search-page HTML for the `ds:1` blob — so this decision does not
introduce the problem so much as make it undeniable. Both files, not just
one, become untrue for one code
path and will have to be qualified. Confining this to an optional extra keeps the
default install honest, but "optional" is a real maintenance posture, not a
disclaimer: the import must stay lazy, and the non-extra path must stay tested.

**Latency goes from one HTTP fetch to a page load plus a client-side round trip —
seconds, not the ~100 ms order of a single fetch.** (That contrast is an
order-of-magnitude estimate; neither figure was measured for this record.) The
practical consequence is concrete: since the transport moved to the search page,
the date sweep costs one fetch per departure date and is capped at
`MAX_DURATION_SWEEP_COMBINATIONS = 600` (`fli/core/builders.py:22`) on the
assumption of the client's 10 req/sec budget. That assumption does not survive a
browser, so sweeps stay on the HTTP path permanently. This transport can never be
a general fallback for "the HTTP path failed".

**It is a move in an arms race the gating exists to win.** Google closed the RPC
to automated clients; driving a browser to obtain the same data is a response
Google can in turn respond to, with headless detection, behavioural
fingerprinting, or friction that lands on the HTTP default path as well. Adding
this raises fli's profile as a target, and the cost of that is not paid only by
the new code path.

**Fragility relocates rather than disappearing.** The HTTP path is fragile to the
search page's shape (`ds:1`, payload indices). The browser path is fragile to the
wire shape, to the `fetch`/XHR hook surface, to headless detection, and to driver
and browser version drift. Fli ends up maintaining two fragile transports instead
of one, with two independent ways to break and a routing layer that has to tell
which broke.

**CI and test coverage get worse in a specific, known way.** The parse half can
keep using the offline fixtures under `tests/search/fixtures/`. The browser half
cannot: it needs a real browser, which means either a browser in CI or a skip
marker and a path that is only ever exercised by hand.

**New surface to maintain:** a transport-selection seam, capability routing
(which requests may use which transport), and error mapping — today's
`SearchUnsupportedError` message has to become "install the extra" without
becoming a lie when the extra is installed and still fails.

**Against those costs, the upside is narrow and worth stating plainly:**
multi-city becomes answerable at all rather than refused; the response decoders
and their offline fixtures already exist, so the new work is transport, not
parsing; and #226 (Explore) is behind the same gate, so the same mechanism
serves it. That last clause was asserted, then retracted, then re-established
by controlled test — see *Correction, twice over* below.

### Open risks

**The shape of a multi-city `GetShoppingResults` response is unverified. This is
the central risk to the whole decision.** Two attempts to capture the response
body through a hooked `fetch`/XHR returned nothing — the call may run in a
worker, or the result may have been served from cache. `parse_flight_row` was
written against the one-way shape (a flat rows array); a multi-city response
plausibly carries a per-leg board instead. If the shapes diverge substantially,
the "decoders already exist" argument — the main thing making this cheap — weakens,
and the fallback is DOM extraction with all of its fragility.

**Whether headless Chrome clears Google's attestation as reliably as headed
Chrome is unverified.** If it does not, the transport needs a visible browser or
a user profile, which changes what "optional extra in a library" can reasonably
mean.

**Nothing in the browser half has been executed on the development machine.** It
runs inside a sandbox that blocks launching Chromium: `playwright install
chromium` succeeds, but `chromium.launch()` fails with `TargetClosedError` /
`kill EPERM`. Verification of both risks above has to happen outside the sandbox.

## Verification update (2026-09-19)

The browser half has now been run, over CDP against a Chrome for Testing
instance with a fresh, signed-out profile. Two of the three open risks are
resolved and the third has moved.

**Attestation: cleared.** A fresh signed-out profile received a real
`GetShoppingResults` response — 236,409 bytes, intercepted and saved as
`tests/search/fixtures/flight_search_multi_city.bin`. This was the harder of
the two cases; no signed-in or privileged profile was needed. The only
obstacle was Google's cookie consent interstitial, which a fresh profile hits
and a normal one does not; dismissing it once ("Reject all") persists in the
profile directory. Any browser transport must handle that wall explicitly —
it is not an error, and it silently prevents the app from ever loading.

**Interception: works.** `page.on("response")` at page level saw the call;
the body came back readable. No worker-level interception was needed.

**Decode: fails, and the reason is narrower than feared.** The multi-city
body does *not* have an unfamiliar row shape. It never reached row parsing at
all: `iter_wrb_chunks` returned zero chunks, so `parse_flight_row` was never
called. The response is a nine-frame length-prefixed stream
(`)]}'\n\n22977\n[["wrb.fr"...`), where every captured one-way and
round-trip fixture is a single frame with no length header at all. Walking
the frames shows the reader's slice is consistently one character short:
each frame's declared length overshoots the next header by exactly 5
characters across all nine frames, and the gap text begins mid-header
(`'6415\n'` where the header is `16415`). The body carries 106 non-ASCII
characters against a 114-byte/character difference, which is consistent with
a byte-versus-character length desync rather than a structural difference.

That matters for scope: the fix is at the wire-framing layer in
`fli/search/_wire.py`, not a decoder rewrite, so the "the decoders already
exist" argument survives — but it is now "the decoders exist and the framer
needs work", which is a different estimate.

It also revisits a dismissal. `ADOPTING.md` records upstream PR #224's second
claim (chunk desync on non-ASCII) as not reproducing post-#230. That dismissal
looks correct for the transport it was tested on and wrong in general:
post-#230 one-way responses carry no length headers, so there is nothing to
desync. Multi-city responses do. #224 may have been describing this exact
defect from the other side.

Because the body is committed as a fixture, closing this needs no browser:
the framing can be fixed and replayed offline.

### Central risk closed (2026-09-19, same day)

The multi-city row shape is **identical** to the one-way shape. With the
framing fixed, the captured body decodes to `chunks=9 rows=83 failed=0
flights=83 shape=known` — `shape=known` meaning the rows sat at the usual
`[2]`/`[3]` positions all along and the structural fallback was never needed.
The cheapest decoded itinerary (LHR-KEF FI451 > KEF-BOS FI633, $1,394) matches
the rendered board exactly, so the decode is cross-validated against what a
human sees.

The framing defect was a unit error in `iter_wrb_chunks`: the length headers
count characters, not UTF-8 bytes, and the module docstring asserted the
opposite. It went undetected because every other captured endpoint returns a
single frame with no header at all, so nothing exercised the arithmetic. Fixed
in `fli/search/_wire.py`, which is production code and benefits every caller,
not just this path; all seven pre-existing fixtures decode unchanged.

So the "decoders already exist" argument is now demonstrated rather than
assumed, and the remaining work for multi-city is transport only. That
strengthens the case for this record, and it also means a maintainer who
rejects the browser dependency still keeps the `_wire.py` fix.

### Correction, twice over: Explore *is* a second consumer (2026-09-19)

This record was drafted expecting upstream #226 (Explore) to sit behind the
same `bgr` gate. That expectation was first recorded as **disproved**, and
then the disproof was itself wrong. Both steps are left here, because the
mistake is more instructive than the conclusion.

**The wrong finding.** A real `GetExploreDestinations` call was captured from
a browser and replayed over plain HTTP with the `bgr` header omitted: HTTP
200, 48 KB, real data. That reads as conclusive, and this record said so —
that Explore needed no browser, and that multi-city therefore carried the
decision alone.

**Why it was wrong.** Replaying a request the browser has just made appears
to hit a short-lived cache keyed on the request, so the replay succeeds on
the strength of the browser's own signed call seconds earlier. The successful
replay was a cache hit, not an ungated endpoint.

**The controlled test.** Capture a *fresh* request and replay it twice within
the same second, varying only the header. Reproduced twice:

    replay WITHOUT bgr                        REJECTED error 13
    replay WITH bgr (copied from the browser) HTTP 200, chunks decoded

Confirmed from the other direction: `SearchExplore` as #226 builds it is
rejected with error 13 live, as are all five rungs of the escalation ladder
its own module docstring proposes, including the exact query parameters
captured from a working page load.

**So the original expectation holds and this decision has two consumers, not
one.** Explore is `bgr`-gated exactly as multi-city is, and #226's data path
is already verified against two real captures. The dependency, the latency
and the arms-race exposure are still real costs, but they are amortised over
two features.

The methodological lesson is worth more than the restored argument: a replay
that reuses a request a browser just issued cannot distinguish "ungated" from
"cached". Any future gate test on this API must vary only the header, on a
fresh request, in the same breath.

## Trigger for revisiting

Reopen if the `x-goog-batchexecute-bgr` gate extends to
`GetBookingResults`. `get_booking_options` still calls that RPC directly, so
the same rotation that killed `GetShoppingResults` would take booking options
with it — on the default install, not just on this optional path — and the
scope decided here ('multi-city only') would have to be re-decided under
pressure rather than weighed.


Reopen this record if any of the following becomes true:

- **The multi-city response does not reuse the one-way row shape** and cannot be
  handled by a modest addition to `_decoders.py`. Then the choice is DOM-only
  extraction or declining the feature, and neither is what this record decided.
- **The gate on `GetShoppingResults` lifts** — `scripts/capture_fixtures.py`
  starts succeeding again over plain HTTP. The browser path then has no reason to
  exist for multi-city and should be deleted, not kept "just in case".
- **Google server-renders multi-city rows into the search page again** —
  `payload[2]` / `payload[3]` come back non-`None` for field 19 = 3. The HTTP
  path can then serve multi-city directly.
- **Headless attestation proves unreliable** and the transport needs a headed
  browser or a persistent user profile to work.
- **Browser-driven traffic starts affecting the HTTP default path** — new
  challenges, blocks or friction on requests that used to succeed. The default
  path matters more than this one, and this one goes if it endangers it.
