/**
 * The timezone helpers.
 *
 * The case that matters is the one the backend has its own test for: a job at
 * 23:30 on the 14th in Denver is the 15th in UTC, and it belongs under the
 * 14th. If these functions disagree with the API's filters, the week view
 * shows jobs on the wrong day and nothing errors.
 */

import { describe, expect, it } from 'vitest'
import {
  addDays,
  addMinutes,
  endOfMonth,
  formatDuration,
  formatHoursMinutes,
  formatTime,
  isoWeekdayOfDate,
  startOfMonth,
  toIsoDate,
  weekOf,
  zonedInstant,
} from './datetime'

const DENVER = 'America/Denver'
const NEW_YORK = 'America/New_York'

describe('toIsoDate', () => {
  it('files a late-evening instant under its local day', () => {
    // 2027-06-15T05:30Z is 23:30 on the 14th in Denver.
    expect(toIsoDate('2027-06-15T05:30:00Z', DENVER)).toBe('2027-06-14')
  })

  it('gives a different day for two zones at the same instant', () => {
    const instant = '2027-06-15T03:00:00Z'

    expect(toIsoDate(instant, DENVER)).toBe('2027-06-14')
    expect(toIsoDate(instant, NEW_YORK)).toBe('2027-06-14')
    expect(toIsoDate(instant, 'UTC')).toBe('2027-06-15')
  })

  it('handles an early-morning instant that is still the previous day west of UTC', () => {
    expect(toIsoDate('2027-01-01T04:00:00Z', DENVER)).toBe('2026-12-31')
  })
})

describe('formatTime', () => {
  it('shows the organization wall-clock time, not the browser one', () => {
    expect(formatTime('2027-06-15T15:00:00Z', DENVER)).toBe('09:00')
    expect(formatTime('2027-06-15T15:00:00Z', NEW_YORK)).toBe('11:00')
  })

  it('reads the same either side of a DST change', () => {
    // The backend keeps "every Tuesday 9am" at 9am local across the March
    // transition; 16:00Z before, 15:00Z after.
    expect(formatTime('2027-03-09T16:00:00Z', DENVER)).toBe('09:00')
    expect(formatTime('2027-03-16T15:00:00Z', DENVER)).toBe('09:00')
  })
})

describe('addDays', () => {
  it('moves forward across a month boundary', () => {
    expect(addDays('2027-01-31', 1)).toBe('2027-02-01')
  })

  it('moves backward across a year boundary', () => {
    expect(addDays('2027-01-01', -1)).toBe('2026-12-31')
  })

  it('crosses a spring-forward day without losing one', () => {
    // 2027-03-14 is the US spring forward. A 23-hour day is still one day.
    expect(addDays('2027-03-13', 1)).toBe('2027-03-14')
    expect(addDays('2027-03-14', 1)).toBe('2027-03-15')
  })

  it('crosses a fall-back day without gaining one', () => {
    expect(addDays('2027-11-06', 1)).toBe('2027-11-07')
    expect(addDays('2027-11-07', 1)).toBe('2027-11-08')
  })

  it('handles a leap day', () => {
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(addDays('2028-02-29', 1)).toBe('2028-03-01')
  })
})

describe('isoWeekdayOfDate', () => {
  it.each([
    ['2027-09-13', 1],
    ['2027-09-14', 2],
    ['2027-09-18', 6],
    ['2027-09-19', 7],
  ])('%s is ISO weekday %i', (date, expected) => {
    expect(isoWeekdayOfDate(date)).toBe(expected)
  })
})

describe('weekOf', () => {
  it('starts on Monday', () => {
    const week = weekOf('2027-09-15')

    expect(week).toHaveLength(7)
    expect(week[0]).toBe('2027-09-13')
    expect(week[6]).toBe('2027-09-19')
  })

  it('treats Sunday as the end of its week, not the start', () => {
    expect(weekOf('2027-09-19')[0]).toBe('2027-09-13')
  })

  it('treats Monday as its own start', () => {
    expect(weekOf('2027-09-13')[0]).toBe('2027-09-13')
  })

  it('spans a DST change without duplicating or dropping a day', () => {
    const week = weekOf('2027-03-14')

    expect(new Set(week).size).toBe(7)
    expect(week[0]).toBe('2027-03-08')
  })
})

describe('formatDuration', () => {
  it.each([
    [null, '—'],
    [45, '45 m'],
    [60, '1 h'],
    [150, '2 h 30 m'],
  ])('%s -> %s', (minutes, expected) => {
    expect(formatDuration(minutes)).toBe(expected)
  })
})

describe('zonedInstant', () => {
  it('turns a wall-clock time in the organization zone into the instant', () => {
    expect(zonedInstant('2027-06-10', '09:00', DENVER)).toBe('2027-06-10T15:00:00.000Z')
    expect(zonedInstant('2027-06-10', '09:00', NEW_YORK)).toBe('2027-06-10T13:00:00.000Z')
    expect(zonedInstant('2027-06-10', '09:00', 'UTC')).toBe('2027-06-10T09:00:00.000Z')
  })

  it('uses the offset in force on that date, not today\'s', () => {
    // Winter is UTC-7 in Denver, summer UTC-6.
    expect(zonedInstant('2027-01-12', '09:00', DENVER)).toBe('2027-01-12T16:00:00.000Z')
    expect(zonedInstant('2027-07-12', '09:00', DENVER)).toBe('2027-07-12T15:00:00.000Z')
  })

  it('files a late-evening visit under the next UTC day', () => {
    // The mirror of the toIsoDate case: 23:30 on the 14th is the 15th in UTC.
    expect(zonedInstant('2027-06-14', '23:30', DENVER)).toBe('2027-06-15T05:30:00.000Z')
  })

  it('handles zones east of UTC, and ones off the hour', () => {
    expect(zonedInstant('2027-06-10', '09:00', 'Europe/Berlin')).toBe('2027-06-10T07:00:00.000Z')
    expect(zonedInstant('2027-06-10', '09:00', 'Asia/Kolkata')).toBe('2027-06-10T03:30:00.000Z')
    expect(zonedInstant('2027-06-10', '00:15', 'Pacific/Auckland'))
      .toBe('2027-06-09T12:15:00.000Z')
  })

  it('is right on both sides of the day the clocks go forward', () => {
    // 14 March 2027, Denver: 02:00 becomes 03:00.
    expect(zonedInstant('2027-03-14', '01:30', DENVER)).toBe('2027-03-14T08:30:00.000Z')
    expect(zonedInstant('2027-03-14', '03:00', DENVER)).toBe('2027-03-14T09:00:00.000Z')
    expect(zonedInstant('2027-03-14', '09:00', DENVER)).toBe('2027-03-14T15:00:00.000Z')
    expect(zonedInstant('2027-03-13', '23:00', DENVER)).toBe('2027-03-14T06:00:00.000Z')
  })

  it('puts a time the clocks skip just after the gap, as the backend does', () => {
    // 02:30 does not exist that morning. Python's fold=0 keeps the
    // pre-change offset (UTC-7), which is 09:30Z, read on the wall as 03:30.
    const instant = zonedInstant('2027-03-14', '02:30', DENVER)

    expect(instant).toBe('2027-03-14T09:30:00.000Z')
    expect(formatTime(instant, DENVER)).toBe('03:30')
  })

  it('takes the first pass of a time that happens twice, as the backend does', () => {
    // 7 November 2027, Denver: 01:30 happens at 07:30Z and again at 08:30Z.
    expect(zonedInstant('2027-11-07', '01:30', DENVER)).toBe('2027-11-07T07:30:00.000Z')
    expect(zonedInstant('2027-11-07', '02:00', DENVER)).toBe('2027-11-07T09:00:00.000Z')
    expect(zonedInstant('2027-11-07', '00:59', DENVER)).toBe('2027-11-07T06:59:00.000Z')
  })

  it.each([
    ['2027-06-10', '09:00'],
    ['2027-01-01', '00:00'],
    ['2027-03-14', '03:15'],
    ['2027-11-07', '12:45'],
    ['2028-02-29', '23:59'],
  ])('round-trips %s %s through toIsoDate and formatTime', (date, time) => {
    const instant = zonedInstant(date, time, DENVER)

    expect(toIsoDate(instant, DENVER)).toBe(date)
    expect(formatTime(instant, DENVER)).toBe(time)
  })

  it('ignores seconds on the time', () => {
    expect(zonedInstant('2027-06-10', '09:00:00', DENVER)).toBe('2027-06-10T15:00:00.000Z')
  })
})

describe('addMinutes', () => {
  it('adds elapsed time, so a visit across a clock change keeps its length', () => {
    // 01:00 for three hours on spring-forward day ends at 05:00 on the wall.
    const start = zonedInstant('2027-03-14', '01:00', DENVER)
    const end = addMinutes(start, 180)

    expect(end).toBe('2027-03-14T11:00:00.000Z')
    expect(formatTime(end, DENVER)).toBe('05:00')
  })

  it('crosses midnight without help', () => {
    const end = addMinutes(zonedInstant('2027-06-10', '23:00', DENVER), 120)

    expect(toIsoDate(end, DENVER)).toBe('2027-06-11')
    expect(formatTime(end, DENVER)).toBe('01:00')
  })
})

describe('formatHoursMinutes', () => {
  it.each([
    [0, '0 m'],
    [59, '0 m'],
    [60, '1 m'],
    [2700, '45 m'],
    [3600, '1 h'],
    [5400, '1 h 30 m'],
    // A minute is never rounded up into existence.
    [5459, '1 h 30 m'],
    [7200, '2 h'],
    // A long week: hours do not roll over into days.
    [144_000, '40 h'],
    [360_060, '100 h 1 m'],
  ])('%i seconds -> %s', (seconds, expected) => {
    expect(formatHoursMinutes(seconds)).toBe(expected)
  })
})

describe('startOfMonth and endOfMonth', () => {
  it.each([
    ['2027-06-14', '2027-06-01', '2027-06-30'],
    ['2027-01-01', '2027-01-01', '2027-01-31'],
    ['2027-12-31', '2027-12-01', '2027-12-31'],
    ['2027-02-10', '2027-02-01', '2027-02-28'],
    // 2028 is a leap year.
    ['2028-02-10', '2028-02-01', '2028-02-29'],
    // The months a DST change falls in are still whole months.
    ['2027-03-14', '2027-03-01', '2027-03-31'],
    ['2027-11-07', '2027-11-01', '2027-11-30'],
  ])('%s is in %s to %s', (date, first, last) => {
    expect(startOfMonth(date)).toBe(first)
    expect(endOfMonth(date)).toBe(last)
  })
})
