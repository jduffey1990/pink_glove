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
  formatDuration,
  formatTime,
  isoWeekdayOfDate,
  toIsoDate,
  weekOf,
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
