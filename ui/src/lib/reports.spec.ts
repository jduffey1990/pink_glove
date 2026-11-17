/**
 * The period presets.
 *
 * The case that matters is the same one `datetime.spec.ts` guards: "today"
 * is the organization's, not the browser's and not UTC's. At 04:30 UTC on a
 * Monday it is still Sunday evening in Denver, and "this week" there is the
 * week that is ending, not the one UTC has already started.
 */

import type { Aging } from '@/api/types'
import { describe, expect, it, vi } from 'vitest'
import { todayIn } from './datetime'
import {
  AGING_COLUMNS,
  csvFilename,
  feesPendingNote,
  formatPeriod,
  isComplete,
  openEntriesNote,
  PERIOD_OPTIONS,
  presetPeriod,
} from './reports'

describe('presetPeriod', () => {
  // 2027-06-16 is a Wednesday.
  const WEDNESDAY = '2027-06-16'

  it('this week is Monday to Sunday around today', () => {
    expect(presetPeriod('this-week', WEDNESDAY)).toEqual({
      date_from: '2027-06-14',
      date_to: '2027-06-20',
    })
  })

  it('last week is the Monday to Sunday before that', () => {
    expect(presetPeriod('last-week', WEDNESDAY)).toEqual({
      date_from: '2027-06-07',
      date_to: '2027-06-13',
    })
  })

  it('a Monday starts its own week and a Sunday ends its own', () => {
    expect(presetPeriod('this-week', '2027-06-14').date_from).toBe('2027-06-14')
    expect(presetPeriod('this-week', '2027-06-20')).toEqual({
      date_from: '2027-06-14',
      date_to: '2027-06-20',
    })
  })

  it('this month is the whole month', () => {
    expect(presetPeriod('this-month', WEDNESDAY)).toEqual({
      date_from: '2027-06-01',
      date_to: '2027-06-30',
    })
  })

  it('last month is the whole month before', () => {
    expect(presetPeriod('last-month', WEDNESDAY)).toEqual({
      date_from: '2027-05-01',
      date_to: '2027-05-31',
    })
  })

  it('last month from January is December of the year before', () => {
    expect(presetPeriod('last-month', '2027-01-15')).toEqual({
      date_from: '2026-12-01',
      date_to: '2026-12-31',
    })
  })

  it('last month from the 31st does not skip a short month', () => {
    // Stepping back "one month" from 31 March lands on a day February does
    // not have, and a careless version returns March again.
    expect(presetPeriod('last-month', '2027-03-31')).toEqual({
      date_from: '2027-02-01',
      date_to: '2027-02-28',
    })
    expect(presetPeriod('last-month', '2028-03-31').date_to).toBe('2028-02-29')
  })

  it('a week can straddle a month and a year', () => {
    // 2027-01-01 is a Friday.
    expect(presetPeriod('this-week', '2027-01-01')).toEqual({
      date_from: '2026-12-28',
      date_to: '2027-01-03',
    })
  })

  it('a week holding a DST change is still seven days', () => {
    // 14 March 2027 is the US spring forward, and a Sunday.
    expect(presetPeriod('this-week', '2027-03-14')).toEqual({
      date_from: '2027-03-08',
      date_to: '2027-03-14',
    })
    // 7 November 2027 is the fall back.
    expect(presetPeriod('last-week', '2027-11-10')).toEqual({
      date_from: '2027-11-01',
      date_to: '2027-11-07',
    })
  })

  it('is reckoned from the organization\'s today, not UTC\'s', () => {
    // 04:30 UTC on Monday 21 June is 22:30 on Sunday the 20th in Denver.
    const instant = new Date('2027-06-21T04:30:00Z')

    const denver = presetPeriod('this-week', todayInAt(instant, 'America/Denver'))
    const utc = presetPeriod('this-week', todayInAt(instant, 'UTC'))

    expect(denver).toEqual({ date_from: '2027-06-14', date_to: '2027-06-20' })
    expect(utc).toEqual({ date_from: '2027-06-21', date_to: '2027-06-27' })
  })

  it('is reckoned from the organization\'s today at a month end too', () => {
    // 03:00 UTC on 1 July is still 30 June in Denver.
    const instant = new Date('2027-07-01T03:00:00Z')

    expect(presetPeriod('this-month', todayInAt(instant, 'America/Denver')).date_from)
      .toBe('2027-06-01')
    expect(presetPeriod('this-month', todayInAt(instant, 'UTC')).date_from).toBe('2027-07-01')
  })
})

/** `todayIn` with the clock pinned, so the test does not depend on when it runs. */
function todayInAt (instant: Date, timeZone: string): string {
  vi.useFakeTimers()
  vi.setSystemTime(instant)
  try {
    return todayIn(timeZone)
  } finally {
    vi.useRealTimers()
  }
}

describe('PERIOD_OPTIONS', () => {
  it('offers the four presets and custom, in that order', () => {
    expect(PERIOD_OPTIONS.map(option => option.value)).toEqual([
      'this-week',
      'last-week',
      'this-month',
      'last-month',
      'custom',
    ])
  })
})

describe('isComplete', () => {
  it('needs both dates', () => {
    expect(isComplete({ date_from: '2027-06-01', date_to: '2027-06-30' })).toBe(true)
    expect(isComplete({ date_from: '2027-06-01' })).toBe(false)
    expect(isComplete({ date_from: '', date_to: '2027-06-30' })).toBe(false)
    expect(isComplete({})).toBe(false)
  })

  it('does not take a half-typed date for a date', () => {
    expect(isComplete({ date_from: '2027-06-0', date_to: '2027-06-30' })).toBe(false)
  })

  it('leaves a backwards range for the server to refuse in its own words', () => {
    expect(isComplete({ date_from: '2027-06-30', date_to: '2027-06-01' })).toBe(true)
  })
})

describe('formatPeriod', () => {
  it('names both ends', () => {
    expect(formatPeriod({ date_from: '2027-06-14', date_to: '2027-06-20' }))
      .toBe('Mon 14 Jun to Sun 20 Jun')
  })

  it('names a single day once', () => {
    expect(formatPeriod({ date_from: '2027-06-14', date_to: '2027-06-14' })).toBe('Mon 14 Jun')
  })
})

describe('AGING_COLUMNS', () => {
  it('lists every bucket the API sends, youngest first', () => {
    expect(AGING_COLUMNS.map(column => column.key)).toEqual([
      'not_due_cents',
      'days_1_30_cents',
      'days_31_60_cents',
      'days_61_90_cents',
      'days_over_90_cents',
    ])
  })

  it('accounts for the whole of a row', () => {
    const row: Aging = {
      invoice_count: 4,
      not_due_cents: 100,
      days_1_30_cents: 20,
      days_31_60_cents: 3,
      days_61_90_cents: 4000,
      days_over_90_cents: 50_000,
      total_cents: 54_123,
    }

    const shown = AGING_COLUMNS.reduce((sum, column) => sum + row[column.key], 0)

    // If a bucket were missing from the table, the columns would not add up
    // to the total printed beside them.
    expect(shown).toBe(row.total_cents)
  })

  it('heads each one with its age', () => {
    expect(AGING_COLUMNS.map(column => column.label)).toEqual([
      'Not yet due',
      '1-30 days',
      '31-60 days',
      '61-90 days',
      'Over 90 days',
    ])
  })
})

describe('openEntriesNote', () => {
  it.each([
    [0, null],
    [1, '1 entry is still open and is not counted.'],
    [3, '3 entries are still open and are not counted.'],
  ])('%i -> %s', (count, expected) => {
    expect(openEntriesNote(count)).toBe(expected)
  })
})

describe('feesPendingNote', () => {
  it.each([
    [0, null],
    [1, '1 card payment has no fee reported yet, so the net will come down.'],
    [2, '2 card payments have no fee reported yet, so the net will come down.'],
  ])('%i -> %s', (count, expected) => {
    expect(feesPendingNote(count)).toBe(expected)
  })
})

describe('csvFilename', () => {
  it('matches the name the API gives a period report', () => {
    expect(csvFilename('timesheet', '2027-06-14', '2027-06-20'))
      .toBe('timesheet_2027-06-14_2027-06-20.csv')
  })

  it('matches the name the API gives a dated report', () => {
    expect(csvFilename('receivables', '2027-06-30')).toBe('receivables_2027-06-30.csv')
  })
})
