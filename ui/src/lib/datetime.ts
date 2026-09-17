/**
 * Dates, in the organization's timezone.
 *
 * The backend stores UTC and reckons days in `Organization.timezone`
 * (CLAUDE.md invariant 8). The UI has to agree, or a dispatcher in Denver
 * looking at "Monday" sees a job that the API filed under Sunday.
 *
 * So nothing here uses the browser's local timezone. Every function takes the
 * organization's zone explicitly, and the week grid, the day headings and the
 * `date_from`/`date_to` query values are all computed in it. `Intl` does the
 * conversion; no date library is pulled in for what amounts to six functions.
 */

/** A calendar date with no time and no zone: exactly what the API's filters take. */
export type IsoDate = string // YYYY-MM-DD

const WEEKDAY_INDEX: Record<string, number> = {
  Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7,
}

function parts (instant: Date, timeZone: string): Record<string, string> {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    weekday: 'short',
    hour12: false,
  })

  return Object.fromEntries(
    formatter.formatToParts(instant).map(part => [part.type, part.value]),
  )
}

/** The calendar date `instant` falls on, in `timeZone`. */
export function toIsoDate (instant: Date | string, timeZone: string): IsoDate {
  const date = typeof instant === 'string' ? new Date(instant) : instant
  const p = parts(date, timeZone)
  return `${p.year}-${p.month}-${p.day}`
}

/** Today's date in `timeZone` -- which is not always today's date here. */
export function todayIn (timeZone: string): IsoDate {
  return toIsoDate(new Date(), timeZone)
}

/** ISO weekday (1 = Monday .. 7 = Sunday) of `instant` in `timeZone`. */
export function isoWeekday (instant: Date | string, timeZone: string): number {
  const date = typeof instant === 'string' ? new Date(instant) : instant
  return WEEKDAY_INDEX[parts(date, timeZone).weekday] ?? 1
}

/**
 * Shift a calendar date by whole days.
 *
 * Done on the date itself rather than by adding 24 hours to an instant,
 * because a DST day is 23 or 25 hours long and "the next day" is neither.
 */
export function addDays (date: IsoDate, days: number): IsoDate {
  const [year, month, day] = date.split('-').map(Number)
  // UTC arithmetic on a date-only value: no zone is involved, so no transition
  // can shift the result.
  const shifted = new Date(Date.UTC(year, month - 1, day + days))
  return shifted.toISOString().slice(0, 10)
}

/** The Monday of the week containing `date`. Weeks start Monday here. */
export function startOfWeek (date: IsoDate): IsoDate {
  const weekday = isoWeekdayOfDate(date)
  return addDays(date, -(weekday - 1))
}

/** ISO weekday of a calendar date. No zone involved: the value is date-only. */
export function isoWeekdayOfDate (date: IsoDate): number {
  const [year, month, day] = date.split('-').map(Number)
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  return weekday === 0 ? 7 : weekday
}

/** The seven dates of the week containing `date`, Monday first. */
export function weekOf (date: IsoDate): IsoDate[] {
  const monday = startOfWeek(date)
  return Array.from({ length: 7 }, (_, index) => addDays(monday, index))
}

/** e.g. "09:00". Wall-clock time in the organization's zone. */
export function formatTime (instant: Date | string, timeZone: string): string {
  const date = typeof instant === 'string' ? new Date(instant) : instant
  const p = parts(date, timeZone)
  return `${p.hour}:${p.minute}`
}

/**
 * e.g. "Tue 16 Sep".
 *
 * Takes a calendar date, not an instant, so no zone is involved -- the
 * conversion already happened in `toIsoDate`. Formatted as UTC to stop the
 * browser's zone shifting a date-only value by a day.
 */
export function formatDayLabel (date: IsoDate): string {
  const [year, month, day] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC',
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  }).format(new Date(Date.UTC(year, month - 1, day)))
}

/** e.g. "Tue 16 Sep, 09:00". */
export function formatDateTime (instant: Date | string, timeZone: string): string {
  const date = typeof instant === 'string' ? new Date(instant) : instant
  return `${formatDayLabel(toIsoDate(date, timeZone))}, ${formatTime(date, timeZone)}`
}

/** "2 h 30 m", for a duration in minutes. */
export function formatDuration (minutes: number | null): string {
  if (minutes === null) {
    return '—'
  }
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours === 0) {
    return `${rest} m`
  }
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} m`
}
