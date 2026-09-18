/**
 * Validation tests for DateSearchFilters.
 * Mirrors tests/models/test_date_search_filters_validation.py.
 */

import { describe, expect, test } from "bun:test";
import { Airport } from "../../src/models/airport.ts";
import { FlightSegment, type PassengerInfo } from "../../src/models/google-flights/base.ts";
import {
  DateSearchFilters,
  MAX_PAST_FROM_DATE_DAYS,
} from "../../src/models/google-flights/dates.ts";

/**
 * Today's date in UTC - the anchor the validators reference.
 *
 * Captured once per call and every other date in a test is derived from it, so
 * a test can never straddle a UTC midnight rollover.
 */
function utcToday(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

/** Format `anchor` shifted by `days` as YYYY-MM-DD. */
function shift(anchor: Date, days: number): string {
  const d = new Date(
    Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), anchor.getUTCDate() + days),
  );
  return d.toISOString().slice(0, 10);
}

const passengerInfo: PassengerInfo = {
  adults: 1,
  children: 0,
  infants_in_seat: 0,
  infants_on_lap: 0,
};

/** A one-way segment 30 days out, so the segment validator is never what fires. */
function basicSegments(anchor: Date): FlightSegment[] {
  return [
    new FlightSegment({
      departure_airport: [[[Airport.PHX, 0]]],
      arrival_airport: [[[Airport.SFO, 0]]],
      travel_date: shift(anchor, 30),
    }),
  ];
}

describe("DateSearchFilters validation", () => {
  test("rejects a to_date that is past everywhere on Earth", () => {
    // The floor is "not before utc_today - 1", so the still-rejected boundary
    // is two days back.
    const anchor = utcToday();
    expect(
      () =>
        new DateSearchFilters({
          passenger_info: passengerInfo,
          flight_segments: basicSegments(anchor),
          from_date: shift(anchor, -7),
          to_date: shift(anchor, -2),
        }),
    ).toThrow(/To date cannot be in the past/);
  });

  test("rejects a past to_date even when the dates arrive reversed", () => {
    const anchor = utcToday();
    expect(
      () =>
        new DateSearchFilters({
          passenger_info: passengerInfo,
          flight_segments: basicSegments(anchor),
          from_date: shift(anchor, -2),
          to_date: shift(anchor, -7),
        }),
    ).toThrow(/To date cannot be in the past/);
  });

  test("accepts a to_date of today", () => {
    // Deliberate change of public behaviour: a range ending "today" used to
    // raise "To date must be in the future". It is still live for any traveller
    // west of the machine running the search.
    const anchor = utcToday();
    const filters = new DateSearchFilters({
      passenger_info: passengerInfo,
      flight_segments: basicSegments(anchor),
      from_date: shift(anchor, -1),
      to_date: shift(anchor, 0),
    });
    expect(filters.to_date).toBe(shift(anchor, 0));
  });

  test("accepts a to_date of yesterday in UTC", () => {
    // The newly-allowed boundary; utc_today - 2 is still rejected above.
    const anchor = utcToday();
    const filters = new DateSearchFilters({
      passenger_info: passengerInfo,
      flight_segments: basicSegments(anchor),
      from_date: shift(anchor, -2),
      to_date: shift(anchor, -1),
    });
    expect(filters.to_date).toBe(shift(anchor, -1));
  });

  test("from_date clamp cannot push from_date past the to_date", () => {
    // to_date may now be as early as yesterday UTC, so clamping from_date to
    // plain "today" would invert the range; the clamp must cap at to_date.
    const anchor = utcToday();
    const filters = new DateSearchFilters({
      passenger_info: passengerInfo,
      flight_segments: basicSegments(anchor),
      from_date: shift(anchor, -(MAX_PAST_FROM_DATE_DAYS + 4)),
      to_date: shift(anchor, -1),
    });
    expect(filters.from_date).toBe(shift(anchor, -1));
    expect(filters.parsed_from_date.getTime()).toBeLessThanOrEqual(
      filters.parsed_to_date.getTime(),
    );
  });

  test("a from_date inside MAX_PAST_FROM_DATE_DAYS is left untouched", () => {
    const anchor = utcToday();
    const filters = new DateSearchFilters({
      passenger_info: passengerInfo,
      flight_segments: basicSegments(anchor),
      from_date: shift(anchor, -3),
      to_date: shift(anchor, 37),
    });
    expect(filters.from_date).toBe(shift(anchor, -3));
  });

  test("clamps an old from_date to today, not to the rejection floor", () => {
    // Pins the clamp target: it stays plain UTC today rather than sliding to
    // earliestSearchableDate(), which would widen MAX_PAST_FROM_DATE_DAYS.
    const anchor = utcToday();
    const filters = new DateSearchFilters({
      passenger_info: passengerInfo,
      flight_segments: basicSegments(anchor),
      from_date: shift(anchor, -7),
      to_date: shift(anchor, 37),
    });
    expect(filters.from_date).toBe(shift(anchor, 0));
  });
});
