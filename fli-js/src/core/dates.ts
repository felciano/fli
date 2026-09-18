/**
 * Shared YYYY-MM-DD parsing and formatting helpers.
 *
 * Lives in `core/` so the same canonical implementation backs every part
 * of the package — `models/google-flights/base.ts`, the date-search
 * filters, and `search/dates.ts` — rather than each maintaining its own
 * subtly-different copy.
 */

/** Strict YYYY-MM-DD shape (4 digit year, 2 digit month, 2 digit day). */
export const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Parse a YYYY-MM-DD string into a UTC `Date`.
 *
 * Rejects strings that don't match {@link ISO_DATE_RE} and rejects
 * out-of-range calendar dates (e.g. `2026-02-30` would round-trip to
 * March, so the round-trip check catches it). Throws {@link TypeError}
 * on any malformed input — consistent with the rest of the package.
 */
export function parseIsoDate(s: string): Date {
  if (!ISO_DATE_RE.test(s)) {
    throw new TypeError(`Expected YYYY-MM-DD date, got: ${s}`);
  }
  const parts = s.split("-").map((p) => Number.parseInt(p, 10));
  const [year, month, day] = parts;
  if (year == null || month == null || day == null) {
    throw new TypeError(`Expected YYYY-MM-DD date, got: ${s}`);
  }
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) {
    throw new TypeError(`Invalid date: ${s}`);
  }
  return d;
}

/** Format a `Date` as YYYY-MM-DD in UTC. */
export function formatIsoDate(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Today's date at UTC midnight. */
export function todayUtc(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

/**
 * The earliest date that is still "today" somewhere on Earth: UTC midnight
 * minus one day.
 *
 * Do not "tidy" this back to plain UTC midnight. Travel dates are supplied in
 * the *origin airport's* local timezone, but this package carries no
 * per-airport timezone data, so validation can only reference the clock of the
 * machine it runs on. Real UTC offsets span UTC-12 to UTC+14, so any
 * traveller's local date is within one day of the UTC date: at 17:00 in San
 * Francisco the UTC date has already rolled over, and comparing against plain
 * UTC midnight refuses a same-day evening departure that has not left.
 * Anchoring to `utc_today - 1` therefore never rejects a date that is still
 * today-or-future for the actual traveller.
 *
 * The accepted cost is that one day of genuinely past dates reaches Google,
 * which answers them with a confusing failure rather than an empty result.
 * Ported from `fli/models/google_flights/base.py::earliest_searchable_date`;
 * see KNOWN_ISSUES.md issue 1 for the Python transport's symptom (the JS
 * transports differ, so the surfaced error may not match verbatim).
 */
export function earliestSearchableDate(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 1));
}
