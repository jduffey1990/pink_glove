/**
 * The reports page's own logic: which dates a period means, and how the
 * figures the server sent are labelled.
 *
 * Nothing here adds up hours or money. The totals, the aging buckets and
 * what counts toward either are decided on the server (ADR-023, ADR-030);
 * this turns "last week" into two dates and a field name into a heading.
 *
 * A period is a pair of calendar dates in the organization's timezone, both
 * inclusive, which is what the API takes. "Today" is passed in rather than
 * read from a clock, so the caller has to say whose today it is: the
 * organization's (`todayIn(session.timeZone)`), never the browser's.
 */

import type { IsoDate } from './datetime'
import type { Aging } from '@/api/types'
import { addDays, endOfMonth, formatDayLabel, startOfMonth, startOfWeek } from './datetime'

export interface Period {
  date_from: IsoDate
  date_to: IsoDate
}

export type PeriodPreset = 'this-week' | 'last-week' | 'this-month' | 'last-month' | 'custom'

export const PERIOD_OPTIONS: { value: PeriodPreset, title: string }[] = [
  { value: 'this-week', title: 'This week' },
  { value: 'last-week', title: 'Last week' },
  { value: 'this-month', title: 'This month' },
  { value: 'last-month', title: 'Last month' },
  { value: 'custom', title: 'Custom' },
]

/**
 * The dates a preset covers, given the organization's today.
 *
 * Weeks run Monday to Sunday, as the schedule's do, and a period is always
 * the whole week or month: "this week" on a Wednesday runs to Sunday. The
 * days that have not happened yet hold nothing, and the heading does not
 * change under someone as the week goes on.
 */
export function presetPeriod (
  preset: Exclude<PeriodPreset, 'custom'>,
  today: IsoDate,
): Period {
  switch (preset) {
    case 'this-week': {
      const monday = startOfWeek(today)
      return { date_from: monday, date_to: addDays(monday, 6) }
    }
    case 'last-week': {
      const monday = addDays(startOfWeek(today), -7)
      return { date_from: monday, date_to: addDays(monday, 6) }
    }
    case 'this-month': {
      return { date_from: startOfMonth(today), date_to: endOfMonth(today) }
    }
    case 'last-month': {
      // The day before the first of this month is in last month, whatever
      // its length and whichever year it is in.
      const lastDay = addDays(startOfMonth(today), -1)
      return { date_from: startOfMonth(lastDay), date_to: lastDay }
    }
  }
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

/**
 * Whether there are two dates to ask with.
 *
 * Only that. Whether the range is backwards or too long is the server's to
 * say, in its own words, and the page shows them: the cap differs by report
 * and a copy of it here would be wrong for one of them.
 */
export function isComplete (period: Partial<Period>): period is Period {
  return ISO_DATE.test(period.date_from ?? '') && ISO_DATE.test(period.date_to ?? '')
}

/** e.g. "Mon 14 Jun to Sun 20 Jun", or one day's label when it is one day. */
export function formatPeriod (period: Period): string {
  const from = formatDayLabel(period.date_from)
  return period.date_from === period.date_to
    ? from
    : `${from} to ${formatDayLabel(period.date_to)}`
}

/** The aging fields of a receivables row: every one but the count and the total. */
export type AgingKey = Exclude<keyof Aging, 'invoice_count' | 'total_cents'>

/**
 * Headings for the aging buckets, youngest first.
 *
 * A `Record` over the generated type on purpose: a bucket added, renamed or
 * removed on the server stops this compiling, so the table cannot quietly
 * drop a column of money or head one with the wrong age.
 */
const AGING_LABEL: Record<AgingKey, string> = {
  not_due_cents: 'Not yet due',
  days_1_30_cents: '1-30 days',
  days_31_60_cents: '31-60 days',
  days_61_90_cents: '61-90 days',
  days_over_90_cents: 'Over 90 days',
}

export const AGING_COLUMNS = (Object.keys(AGING_LABEL) as AgingKey[]).map(key => ({
  key,
  label: AGING_LABEL[key],
}))

/** "3 entries are still open and are not counted." Null when there are none. */
export function openEntriesNote (count: number): string | null {
  if (count <= 0) {
    return null
  }
  return count === 1
    ? '1 entry is still open and is not counted.'
    : `${count} entries are still open and are not counted.`
}

/** Why the net may still be too high. Null when every fee is in. */
export function feesPendingNote (count: number): string | null {
  if (count <= 0) {
    return null
  }
  return count === 1
    ? '1 card payment has no fee reported yet, so the net will come down.'
    : `${count} card payments have no fee reported yet, so the net will come down.`
}

/**
 * The name a downloaded report is saved under, e.g.
 * `timesheet_2027-06-14_2027-06-20.csv`.
 *
 * Spelled the same way as the `Content-Disposition` the API sends. The page
 * cannot read that header across origins in development, so the name is
 * built here rather than being right in production only.
 */
export function csvFilename (report: string, ...dates: IsoDate[]): string {
  return `${[report, ...dates].join('_')}.csv`
}
