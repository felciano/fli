# Adopting this fork's work upstream

A map from this fork's commits back to the open `punitarani/fli` PRs they
supersede, written so a returning maintainer can decide what to take without
re-deriving any of it.

Nothing here asks for trust. Every claim below was reproduced against the live
service or the test suite, and the section that matters most — [Two upstream PRs
do not do what they say](#two-upstream-prs-do-not-do-what-they-say) — is the one
to check first, because merging those two verbatim would close the issues
without fixing them.

## Why this is worth your time

`flights` on PyPI is currently unusable, for two independent reasons:

1. **The CLI dies on import.** `fli/cli/utils.py` does `from click import
   Context, Parameter`, and `click` was never declared. It arrived transitively
   via Typer until Typer dropped it. A fresh `pipx install flights` today
   resolves Typer 0.27.2 and every command raises
   `ModuleNotFoundError: No module named 'click'`. CI cannot see this because
   `uv.lock` pins Typer 0.16.0, which still carries `click`.
2. **Search returns nothing for everyone.** `GetShoppingResults` now requires a
   browser-signed `x-goog-batchexecute-bgr` header (upstream issue #223, with
   duplicates #200, #220, #142).

Four commits below fix (1). PR #230 — which is *not* this fork's work — fixes (2).

## Prerequisite: merge #230 first, from its own author

Commits `b932aee..7f03157` on this branch are **byte-identical** to
[#230](https://github.com/punitarani/fli/pull/230) by @Princeu3 (verified:
`git diff pr230 7f03157` is empty). They are present here only because
everything after them depends on a working search.

Merge #230 from its own PR. This fork claims no credit for it, and the 16
commits below apply on top.

## The 16 commits

Ordered as they appear on the branch. "Author" is the commit author; upstream
contributors whose work was reimplemented carry `Co-Authored-By` trailers.

| # | Commit | Supersedes | Upstream author | Notes |
|---|--------|-----------|-----------------|-------|
| 1 | `a978b84` | [#210](https://github.com/punitarani/fli/pull/210) | @hishamank | Cherry-picked **verbatim**, authorship intact |
| 2 | `fc17461` | — | — | #210 as-is fails `ruff format --check` |
| 3 | `1651b94` | — | — | Fixes a defect #210's own review flagged and left: `query.passengers` raised `KeyError` on the error path |
| 4 | `e42fad5` | [#103](https://github.com/punitarani/fli/pull/103), [#194](https://github.com/punitarani/fli/pull/194) | Alexey Guskov (@kvasdopil), @etcook | Child/infant counts. #103 no longer applies (fails on 9 of its 15 files); reimplemented and extended to `multi` and `get_booking_options`, which postdate it |
| 5 | `1a36190` | [#160](https://github.com/punitarani/fli/pull/160) | Milo Quinn (@miloquinn) | Closes #131. **See caveat below** |
| 6 | `90593ca` | [#193](https://github.com/punitarani/fli/pull/193) | @gateway | **See caveat below** |
| 7 | `4bd0f90` | [#199](https://github.com/punitarani/fli/pull/199) | @LGull | Comma-separated multi-airport in the CLI |
| 8 | `4fbbd41` | [#222](https://github.com/punitarani/fli/pull/222) *(part)* | Carter Temm (@cartertemm) | Takes only the undeclared-dependency half — see [What was deliberately not taken](#what-was-deliberately-not-taken) |
| 9 | `01ec0da` | — | — | Follow-up to #5: the JSON serializer leaked the new suffix |
| 10 | `f460858` | — | — | Airport errors now name the slot; scopes the dependency guard |
| 11 | `e1c9af7` | [#215](https://github.com/punitarani/fli/pull/215) | Scott J. Goldman (@scottjg) | UTC-safe date validation + actionable MCP errors |
| 12 | `bc25eba` | [#111](https://github.com/punitarani/fli/pull/111) | Trevin Chow (@tmchow) | ICAO codes. Mapping payload carried over verbatim; surrounding code reimplemented |
| 13 | `37a5354` | [#195](https://github.com/punitarani/fli/pull/195) | Roberto Reale | `--min-duration`/`--max-duration` sweep |
| 14 | `06015b4` | [#196](https://github.com/punitarani/fli/pull/196) | Roberto Reale | `--return-time` for the return leg |
| 15 | `5eb5761` | — | — | Follow-up to #12: a wrong row in the ICAO data — see below |
| 16 | `34896e5` | — | — | `KNOWN_ISSUES.md` |

**Closeable on merge:** #210, #198, #103, #194, #160, #193, #199, #215, #111,
#195, #196 — eleven PRs, plus #222 partially. Issue #131 is fixed by #5.

## Two upstream PRs do not do what they say

Both were confirmed in-session, not inferred. This is the main reason to prefer
these commits over the originals.

### #193 (`curl-cffi` SSRF) changes nothing at runtime

The advisory is real — `GHSA-qw2m-4pqf-rmpp` / `CVE-2026-33752`, HIGH,
redirect-based SSRF, patched in 0.15.0 — but **0.15's remediation is opt-in**.
`allow_redirects=True` still follows private-IP redirects on 0.13, 0.15 and
0.16 alike. `fli` passed an explicit `allow_redirects=True` at every call site,
so merging #193 satisfies a scanner and leaves behaviour byte-for-byte
identical. Proven here: with the lock upgraded but the call-site edits reverted,
the behavioural test still leaked the sentinel.

`90593ca` also adopts `allow_redirects="safe"` (`CurlFollow.SAFE`) as a
client-level default, so new call sites inherit it and cannot silently regress.

### #160 (enum aliases) was never regenerated

The helper is correct, but upstream never re-ran the generator, so merging it
verbatim changes nothing at runtime. Verified before the fix: 44 alias groups,
**92 affected IATA codes**, `Airport.TRI` resolving to `Airport.PSC` — a Bristol,
TN search silently returning Pasco, WA results.

`1a36190` regenerates the modules, adds a generation-time post-condition, and
emits `unique(Enum(...))` so a future data refresh committed without
regenerating fails loudly at import instead of returning the wrong airport.

## A data defect worth knowing about

`5eb5761` fixes a row introduced by the #111 mapping: `FAJS,QRA`. Both halves
are wrong. `FAJS` is O. R. Tambo's *retired* ICAO code (changed to `FAOR` on
10 January 2013, and `FAOR,JNB` is already present), and Rand Airport's ICAO is
`FAGM`. The effect was `resolve_airport("FAJS")` succeeding and returning a
small general-aviation field instead of Johannesburg's main international
airport.

The generator's validation could not catch it — it checks key shape, duplicate
keys, and that the target exists, none of which a plausible-but-wrong *pairing*
violates. If you take #111 from its own PR rather than from here, this row needs
fixing there too.

## What was deliberately not taken

- **#222's `typer.Context` / `typer.CallbackParam` swap.** Sound in principle,
  but #210's `click` declaration is already here and verified end-to-end against
  Typer 0.27.2. Only #222's undeclared-dependency half was taken. Worth revisiting
  as its own change.
- **#224, #208, #205, #201** — all four decode or retry the `wrb.fr` error
  envelope. #230 supersedes them: it added `SearchRejectedError`/
  `SearchUnsupportedError`, and `get_booking_options` now fails with a named
  error. All four also conflict heavily with #230 in `_wire.py`/`exceptions.py`.
  #224's second claim (chunk desync on non-ASCII) did not reproduce post-#230:
  ZRH→GRU returns 11 results cleanly.
- **#226 (Explore)** — not evaluated. It is built on `GetExploreDestinations`,
  another batchexecute RPC, so it may be behind the same `bgr` gate that killed
  `GetShoppingResults`. Worth confirming before spending review time.

## Release notes

**Breaking, and it warrants a minor bump rather than a patch.** `1a36190`
changes `.value` for 104 enum members: shared names now carry a disambiguating
`" (CODE)"` suffix. Value-based construction changes accordingly —
`Airport('Tri-Cities Airport')` now raises `ValueError` where it previously
returned `Airport.PSC`. Silently-wrong to loudly-failing is an improvement, but
it is still a break.

`fli-js` is structurally unaffected (it keys by IATA code), so the two libraries
now differ deliberately for those 104 entries.

Also note: `DateSearchFilters` with `to_date == today` is now accepted where it
previously raised, and that error string changed from "To date must be in the
future" to "To date cannot be in the past".

## Verification

- **Full suite: 995 passed, 5 xfailed**, including `tests/search/`, which
  exercises the live service and could not run at all before #230.
- `ruff check` and `ruff format --check` clean.
- Live end-to-end, re-run after each batch: LHR→PIT round trip returns real
  priced itineraries with working booking URLs. Passenger counts verified to
  reach Google as *fares*, not just as accepted flags — 3 adults prices at
  exactly 3× the 1-adult fare, while 2 adults + 1 child comes in below it.

`KNOWN_ISSUES.md` records three verified defects left unfixed on purpose, the
most significant being that `fli dates` silently ignores `--airlines` /
`--exclude-airlines`.
