# Known issues

Defects verified on this fork's `integration` branch and deliberately left
unfixed, each because the fix belongs in its own change rather than bolted onto
the batch that surfaced it. Every entry below was reproduced, not inferred.

## 1. A past date inside the one-day grace window reports a transport failure

**Severity:** medium — misleading error, wrong debugging path.

`earliest_searchable_date()` deliberately anchors to `utc_today - 1` so that no
traveler's genuine same-day search is refused (real UTC offsets span UTC-12 to
UTC+14). The unavoidable cost is that one day of genuinely past dates passes
validation and reaches Google.

Google does not answer those with an empty result. The page returns without its
`ds:1` payload, surfacing as:

```
SearchParseError: Search page carried no ds:1 payload - Google may have changed
the page shape, or served a consent/blocked page instead.
```

That is the signature of the dead `GetShoppingResults` RPC (see upstream #223),
so a human or an agent hitting it will conclude the transport is blocked rather
than that the date was in the past.

**Why it is not fixed here:** the grace window is correct and should stay. The
fix is to recognise this specific case and re-message it, which means deciding
how confidently a no-payload response can be attributed to a past date versus a
genuine block. Surfaced while reviewing upstream PR #215.

## 2. The CLI prints raw Pydantic dumps for validation errors

**Severity:** low — ugly, pre-existing, MCP is unaffected.

`fli/cli/commands/flights.py` catches `(AttributeError, ValueError)` and prints
the exception verbatim, so a clearly-past date yields:

```console
$ fli flights SFO LAX 2020-01-01
Error: 1 validation error for FlightSegment
travel_date
  Value error, Travel date cannot be in the past [type=value_error, ...]
    For further information visit https://errors.pydantic.dev/2.11/v/value_error
```

The MCP surface already formats these to a single actionable line
(`travel_date: Travel date cannot be in the past`) via
`_format_validation_error`, added with upstream PR #215. The CLI does not reuse
it.

**Why it is not fixed here:** reusing the helper is a small change but it alters
CLI output text, which several tests pin, and #215 was scoped to the MCP
surface. Confirmed pre-existing: output is byte-identical with the #215
production changes stashed.
