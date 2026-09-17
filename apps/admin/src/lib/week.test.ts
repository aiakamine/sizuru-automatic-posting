import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  formatJstIsoDate,
  formatJstMonthDay,
  formatJstTime,
  formatJstWeekRangeLabel,
  fromDatetimeLocalValue,
  isSameJstDay,
  jstWeekDays,
  jstWeekStart,
  toDatetimeLocalValue,
  toDueAtIsoString,
} from "./week";

describe("jstWeekStart", () => {
  it("returns the same Monday 00:00 JST for any weekday within that week", () => {
    // 2026-09-16 (Wed) 07:00 JST
    const wednesday = new Date("2026-09-16T07:00:00+09:00");
    const start = jstWeekStart(wednesday);
    expect(start.toISOString()).toBe(new Date("2026-09-14T00:00:00+09:00").toISOString());
  });

  it("treats Sunday 23:59 JST as the end of the week that started the previous Monday", () => {
    const sundayNight = new Date("2026-09-20T23:59:00+09:00");
    const start = jstWeekStart(sundayNight);
    expect(start.toISOString()).toBe(new Date("2026-09-14T00:00:00+09:00").toISOString());
  });

  it("treats Monday 00:00 JST as the start of the new week, not the previous one", () => {
    const mondayMidnight = new Date("2026-09-21T00:00:00+09:00");
    const start = jstWeekStart(mondayMidnight);
    expect(start.toISOString()).toBe(new Date("2026-09-21T00:00:00+09:00").toISOString());
  });

  it("returns the same result regardless of the host browser/runtime timezone", () => {
    const instant = new Date("2026-09-20T23:59:00+09:00"); // TZ-agnostic instant (has offset)
    const original = process.env.TZ;
    try {
      process.env.TZ = "America/New_York";
      const inNewYork = jstWeekStart(instant).toISOString();
      process.env.TZ = "Asia/Tokyo";
      const inTokyo = jstWeekStart(instant).toISOString();
      process.env.TZ = "UTC";
      const inUtc = jstWeekStart(instant).toISOString();
      expect(inNewYork).toBe(inTokyo);
      expect(inNewYork).toBe(inUtc);
    } finally {
      process.env.TZ = original;
    }
  });

  it("returns the same result under a far-behind-UTC host timezone (America/Los_Angeles)", () => {
    // 2026-09-16T07:00+09:00 JST is 2026-09-15T15:00-07:00 in Los Angeles (PDT):
    // a naive `getHours()`-based implementation would compute the wrong JST calendar day here.
    const instant = new Date("2026-09-16T07:00:00+09:00");
    const original = process.env.TZ;
    try {
      process.env.TZ = "America/Los_Angeles";
      const start = jstWeekStart(instant).toISOString();
      process.env.TZ = "Asia/Tokyo";
      expect(start).toBe(jstWeekStart(instant).toISOString());
    } finally {
      process.env.TZ = original;
    }
  });

  it("returns the same result under a far-ahead-of-UTC host timezone (Pacific/Kiritimati, UTC+14)", () => {
    // Kiritimati is 5 hours ahead of JST, so the local calendar day there can already be
    // one day ahead of the JST calendar day for the same instant.
    const instant = new Date("2026-09-16T23:30:00+09:00");
    const original = process.env.TZ;
    try {
      process.env.TZ = "Pacific/Kiritimati";
      const start = jstWeekStart(instant).toISOString();
      process.env.TZ = "Asia/Tokyo";
      expect(start).toBe(jstWeekStart(instant).toISOString());
    } finally {
      process.env.TZ = original;
    }
  });

  it("rolls over into the next ISO year at the year boundary (2026-12-28..2027-01-03 week)", () => {
    // 2026-12-31 (Thu) JST falls in the Mon-Sun week 2026-12-28..2027-01-03.
    const newYearsEve = new Date("2026-12-31T12:00:00+09:00");
    const start = jstWeekStart(newYearsEve);
    expect(start.toISOString()).toBe(new Date("2026-12-28T00:00:00+09:00").toISOString());
  });

  it("keeps the following Monday in the new year as the start of the next week", () => {
    const newYearMonday = new Date("2027-01-04T00:00:00+09:00");
    const start = jstWeekStart(newYearMonday);
    expect(start.toISOString()).toBe(new Date("2027-01-04T00:00:00+09:00").toISOString());
  });
});

describe("jstWeekDays", () => {
  it("returns 7 consecutive JST midnights starting from the week start", () => {
    const start = new Date("2026-09-14T00:00:00+09:00");
    const days = jstWeekDays(start);
    expect(days).toHaveLength(7);
    expect(days[0]?.toISOString()).toBe(new Date("2026-09-14T00:00:00+09:00").toISOString());
    expect(days[6]?.toISOString()).toBe(new Date("2026-09-20T00:00:00+09:00").toISOString());
  });

  it("includes the leap day when the week contains 2028-02-29", () => {
    // 2028-02-28 (Mon) .. 2028-03-05 (Sun); 2028 is a leap year.
    const start = new Date("2028-02-28T00:00:00+09:00");
    const days = jstWeekDays(start);
    expect(formatJstIsoDate(days[1] as Date)).toBe("2028-02-29");
  });
});

describe("isSameJstDay", () => {
  it("is true for two instants on the same JST calendar day", () => {
    const a = new Date("2026-09-16T00:05:00+09:00");
    const b = new Date("2026-09-16T23:55:00+09:00");
    expect(isSameJstDay(a, b)).toBe(true);
  });

  it("is false when the JST calendar day differs, even if UTC day is the same", () => {
    // 2026-09-16T23:30+09:00 is 2026-09-16T14:30Z; +30min later is still 09-16 in UTC
    // but crosses into 09-17 in JST once past 15:00Z
    const a = new Date("2026-09-16T23:59:00+09:00");
    const b = new Date("2026-09-17T00:01:00+09:00");
    expect(isSameJstDay(a, b)).toBe(false);
  });

  it("is false for the exact 23:59:59.999 -> 00:00:00.000 JST midnight boundary (1ms apart)", () => {
    const lastMsOfDay = new Date("2026-09-16T23:59:59.999+09:00");
    const firstMsOfNextDay = new Date("2026-09-17T00:00:00.000+09:00");
    expect(isSameJstDay(lastMsOfDay, firstMsOfNextDay)).toBe(false);
  });

  it("is true for two instants exactly at 00:00:00.000 JST (same day column)", () => {
    const midnight = new Date("2026-09-16T00:00:00.000+09:00");
    const sameMidnight = new Date("2026-09-16T00:00:00.000+09:00");
    expect(isSameJstDay(midnight, sameMidnight)).toBe(true);
  });
});

describe("formatJstTime", () => {
  it("formats as zero-padded HH:mm in JST", () => {
    expect(formatJstTime(new Date("2026-09-16T07:05:00+09:00"))).toBe("07:05");
  });
});

describe("formatJstMonthDay", () => {
  it("formats as M/D", () => {
    expect(formatJstMonthDay(new Date("2026-09-16T07:05:00+09:00"))).toBe("9/16");
  });
});

describe("formatJstWeekRangeLabel", () => {
  it("formats the week as 'YYYY/M/D – M/D'", () => {
    const start = new Date("2026-09-14T00:00:00+09:00");
    expect(formatJstWeekRangeLabel(start)).toBe("2026/9/14 – 9/20");
  });
});

describe("toDatetimeLocalValue / fromDatetimeLocalValue", () => {
  it("round-trips an instant through the JST wall-clock representation", () => {
    const instant = new Date("2026-09-16T07:05:00+09:00");
    const local = toDatetimeLocalValue(instant);
    expect(local).toBe("2026-09-16T07:05");
    expect(fromDatetimeLocalValue(local).toISOString()).toBe(instant.toISOString());
  });

  it("rejects a calendar-invalid date (e.g. February 30) instead of silently rolling over to March", () => {
    expect(() => fromDatetimeLocalValue("2026-02-30T10:00")).toThrow(/invalid/);
  });

  it("rejects an out-of-range time (e.g. 25:00)", () => {
    expect(() => fromDatetimeLocalValue("2026-09-16T25:00")).toThrow(/invalid/);
  });

  it("rejects a nonexistent day (e.g. April 31)", () => {
    expect(() => fromDatetimeLocalValue("2026-04-31T10:00")).toThrow(/invalid/);
  });

  it("accepts February 29 in a leap year (2028)", () => {
    expect(() => fromDatetimeLocalValue("2028-02-29T10:00")).not.toThrow();
    expect(fromDatetimeLocalValue("2028-02-29T10:00").toISOString()).toBe(
      new Date("2028-02-29T10:00:00+09:00").toISOString(),
    );
  });

  it("rejects February 29 in a non-leap year (2027)", () => {
    expect(() => fromDatetimeLocalValue("2027-02-29T10:00")).toThrow(/invalid/);
  });
});

describe("formatJstIsoDate", () => {
  it("formats as YYYY-MM-DD in JST", () => {
    expect(formatJstIsoDate(new Date("2026-09-16T23:59:00+09:00"))).toBe("2026-09-16");
  });

  it("crosses to the next JST day even when the UTC day is still the same", () => {
    // 2026-09-16T15:30Z = 2026-09-17T00:30+09:00
    expect(formatJstIsoDate(new Date("2026-09-16T15:30:00Z"))).toBe("2026-09-17");
  });
});

describe("toDueAtIsoString", () => {
  it("formats an instant as an ISO string with the +09:00 offset", () => {
    const instant = new Date("2026-09-16T07:00:00+09:00");
    expect(toDueAtIsoString(instant)).toBe("2026-09-16T07:00:00+09:00");
  });

  it("pads single-digit components", () => {
    const instant = new Date("2026-01-05T03:04:05+09:00");
    expect(toDueAtIsoString(instant)).toBe("2026-01-05T03:04:05+09:00");
  });
});
