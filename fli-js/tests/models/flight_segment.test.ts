/**
 * Validation tests for FlightSegment.
 * Mirrors tests/models/test_flight_segment_validation.py.
 */

import { describe, expect, test } from "bun:test";
import { Airport } from "../../src/models/airport.ts";
import { FlightSegment } from "../../src/models/google-flights/base.ts";

/** Today's date at UTC midnight - the anchor the validator references. */
function utcToday(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

/** Format UTC today shifted by `days` as YYYY-MM-DD. */
function shiftFromToday(days: number): string {
  const t = utcToday();
  const d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate() + days));
  return d.toISOString().slice(0, 10);
}

function futureDate(daysAhead = 30): string {
  return shiftFromToday(daysAhead);
}

describe("FlightSegment validation", () => {
  test("rejects travel dates that are past everywhere on Earth", () => {
    // utc_today - 1 is deliberately still accepted (see the test below), so the
    // still-rejected boundary sits one day further back. This test is what
    // stops the grace period widening beyond the single day that real UTC
    // offsets require.
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.JFK, 0]]],
          arrival_airport: [[[Airport.LAX, 0]]],
          travel_date: shiftFromToday(-2),
        }),
    ).toThrow(/past/);
  });

  test("accepts the day before the UTC date", () => {
    // Travel dates are local to the origin airport, but the validator can only
    // see the machine clock. At 17:00 in San Francisco the UTC date has already
    // rolled over, so a same-day evening SFO departure looks like "yesterday".
    // Rejecting it blinds every westward user to same-day flights for the last
    // hours of their day, so utc_today - 1 must remain searchable.
    // Mirrors test_flight_segment_accepts_yesterday_in_utc.
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.SFO, 0]]],
          arrival_airport: [[[Airport.LAX, 0]]],
          travel_date: shiftFromToday(-1),
        }),
    ).not.toThrow();
  });

  test("accepts today's date", () => {
    const today = new Date().toISOString().slice(0, 10);
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.JFK, 0]]],
          arrival_airport: [[[Airport.LAX, 0]]],
          travel_date: today,
        }),
    ).not.toThrow();
  });

  test("rejects same departure and arrival", () => {
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.JFK, 0]]],
          arrival_airport: [[[Airport.JFK, 0]]],
          travel_date: futureDate(),
        }),
    ).toThrow(/different/);
  });

  test("rejects empty departure airport list", () => {
    expect(
      () =>
        new FlightSegment({
          departure_airport: [],
          arrival_airport: [[[Airport.LAX, 0]]],
          travel_date: futureDate(),
        }),
    ).toThrow(/must be specified/);
  });

  test("rejects empty arrival airport list", () => {
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.JFK, 0]]],
          arrival_airport: [],
          travel_date: futureDate(),
        }),
    ).toThrow(/must be specified/);
  });

  test("parsed_travel_date returns a Date", () => {
    const seg = new FlightSegment({
      departure_airport: [[[Airport.JFK, 0]]],
      arrival_airport: [[[Airport.LAX, 0]]],
      travel_date: futureDate(60),
    });
    expect(seg.parsed_travel_date).toBeInstanceOf(Date);
  });

  test("rejects invalid date format", () => {
    expect(
      () =>
        new FlightSegment({
          departure_airport: [[[Airport.JFK, 0]]],
          arrival_airport: [[[Airport.LAX, 0]]],
          travel_date: "12/25/2026",
        }),
    ).toThrow();
  });
});
