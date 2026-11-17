/**
 * The booking form, as data.
 *
 * A dispatcher thinks "Tuesday at nine, two hours". The API takes two UTC
 * instants. Everything between the two lives here with a spec, so the dialog
 * is inputs and nothing else:
 *
 * - the date and time are wall-clock in the ORGANIZATION's zone and become
 *   instants through `lib/datetime`, never through the browser's zone;
 * - the visit's length is a duration, not an end time. "09:00 to 01:00" is
 *   ambiguous about the day and wrong across a clock change; start plus
 *   elapsed minutes is neither, and it is how a plan describes a visit too;
 * - the price is never worked out here. Blank on a new visit means the
 *   server quotes it from the service, and the page shows what came back;
 * - an edit sends only what changed. Re-sending a start the dispatcher never
 *   touched would round its seconds away, which moves the visit, and a moved
 *   plan visit is one the plan stops managing (ADR-020).
 *
 * What may be booked, by whom and in what state is the server's to say. The
 * only checks here are the ones a form needs to build a request at all.
 */

import type { Job, JobRequest, NewJobRequest } from '@/api/types'
import type { IsoDate } from '@/lib/datetime'
import { fieldErrors } from '@/api/errors'
import { addMinutes, formatTime, toIsoDate, zonedInstant } from '@/lib/datetime'
import { centsToDollars, dollarsToCents } from '@/lib/money'

export interface VisitForm {
  customer: string | null
  location: string | null
  service: string | null
  /** Organization-local calendar date. */
  date: IsoDate
  /** Organization-local wall-clock time, "HH:MM". */
  start: string
  /** `null` while the field is empty. A number input can also hand back ''. */
  durationMinutes: number | string | null
  /** Dollars as typed. Blank on a new visit: the server quotes it. */
  price: string
  /** User ids. New visits only; an existing job's crew has its own actions. */
  assignees: string[]
  notes: string
}

/** The inputs a message can be shown under. */
export type VisitField
  = | 'customer'
    | 'location'
    | 'service'
    | 'date'
    | 'start'
    | 'duration'
    | 'price'
    | 'assignees'
    | 'notes'

export interface VisitErrors {
  fields: Partial<Record<VisitField, string[]>>
  /** Messages that belong to no input, shown above the form. */
  general: string[]
}

export const NO_VISIT_ERRORS: VisitErrors = { fields: {}, general: [] }

/** A blank form for a new visit on `date`. */
export function newVisitForm (date: IsoDate): VisitForm {
  return {
    customer: null,
    location: null,
    service: null,
    date,
    start: '09:00',
    durationMinutes: null,
    price: '',
    assignees: [],
    notes: '',
  }
}

/** The form as it stands for an existing job, in the organization's zone. */
export function visitFormFromJob (job: Job, timeZone: string): VisitForm {
  return {
    customer: job.customer,
    location: job.location,
    service: job.service,
    date: toIsoDate(job.scheduled_start, timeZone),
    start: formatTime(job.scheduled_start, timeZone),
    durationMinutes: job.duration_minutes,
    price: centsToDollars(job.price_cents).toFixed(2),
    assignees: [],
    notes: job.notes ?? '',
  }
}

/** The duration as a usable number of minutes, or null if the field holds none. */
export function durationOf (form: VisitForm): number | null {
  const value = form.durationMinutes
  if (value === null || value === '') {
    return null
  }
  const minutes = Number(value)
  return Number.isFinite(minutes) ? minutes : null
}

/**
 * When the visit starts and ends, as the instants the API takes.
 *
 * Null while the date, time or duration is missing. A duration of zero or
 * less is passed through: "a job must end after it starts" is the server's
 * sentence, and it comes back under the duration field.
 */
export function visitSchedule (
  form: VisitForm,
  timeZone: string,
): { scheduled_start: string, scheduled_end: string } | null {
  const minutes = durationOf(form)
  if (!form.date || !form.start || minutes === null) {
    return null
  }

  const start = zonedInstant(form.date, form.start, timeZone)
  return { scheduled_start: start, scheduled_end: addMinutes(start, minutes) }
}

/** Enough is filled in to send. Not a judgement on whether it will be accepted. */
export function isComplete (form: VisitForm): boolean {
  return Boolean(form.customer && form.location && form.service)
    && form.date !== ''
    && form.start !== ''
    && durationOf(form) !== null
}

/**
 * What the form says the price is.
 *
 * `blank` and `invalid` are different answers: a blank price on a new visit
 * is a request for a quote, and a typo must not be sent as one.
 */
export function priceOf (form: VisitForm): { cents: number } | 'blank' | 'invalid' {
  if (form.price.trim() === '') {
    return 'blank'
  }
  const cents = dollarsToCents(form.price)
  return Number.isNaN(cents) ? 'invalid' : { cents }
}

/**
 * What the form can say without asking the server, shown as it is typed.
 *
 * Only whether the price can be read as a number at all. `existing` is an
 * edit: a job always has a price, so a blank one there is not a request for
 * a quote and cannot be sent.
 */
export function localErrors (form: VisitForm, { existing = false } = {}): VisitErrors {
  const price = priceOf(form)

  if (price === 'invalid') {
    return { fields: { price: ['Enter an amount in dollars, like 150 or 150.00.'] }, general: [] }
  }
  if (price === 'blank' && existing) {
    return { fields: { price: ['Enter a price. Use 0 for a visit that is not charged.'] }, general: [] }
  }
  return NO_VISIT_ERRORS
}

/**
 * The request that books a new visit, or null while the form is incomplete.
 *
 * `price_cents` is left out when the price is blank, which is how the API is
 * asked to quote. `assignees` goes in the same request so the visit and its
 * crew are booked together or not at all.
 */
export function toNewJobRequest (form: VisitForm, timeZone: string): NewJobRequest | null {
  const schedule = visitSchedule(form, timeZone)
  const price = priceOf(form)
  if (!isComplete(form) || schedule === null || price === 'invalid') {
    return null
  }

  return {
    customer: form.customer as string,
    location: form.location as string,
    service: form.service as string,
    ...schedule,
    ...(price === 'blank' ? {} : { price_cents: price.cents }),
    ...(form.assignees.length > 0 ? { assignees: form.assignees } : {}),
    notes: form.notes.trim(),
  }
}

/**
 * The PATCH for an edit: only the fields whose inputs changed.
 *
 * `before` is the form as `visitFormFromJob` built it, so "changed" means the
 * dispatcher changed it, not that a round trip through "HH:MM" lost the
 * seconds. A longer or shorter visit that starts when it always did sends the
 * end alone, measured from the job's real start.
 *
 * Empty when nothing changed, and null when what changed cannot be sent yet.
 */
export function toJobPatch (
  job: Job,
  before: VisitForm,
  form: VisitForm,
  timeZone: string,
): JobRequest | null {
  const patch: JobRequest = {}

  if (form.location !== before.location) {
    if (!form.location) {
      return null
    }
    patch.location = form.location
  }

  if (form.service !== before.service) {
    if (!form.service) {
      return null
    }
    patch.service = form.service
  }

  const moved = form.date !== before.date || form.start !== before.start
  const resized = durationOf(form) !== durationOf(before)

  if (moved) {
    const schedule = visitSchedule(form, timeZone)
    if (schedule === null) {
      return null
    }
    Object.assign(patch, schedule)
  } else if (resized) {
    const minutes = durationOf(form)
    if (minutes === null) {
      return null
    }
    patch.scheduled_end = addMinutes(job.scheduled_start, minutes)
  }

  if (form.price.trim() !== before.price.trim()) {
    const price = priceOf(form)
    // A job always has a price, so blanking the field is not a request for a
    // quote here; there is nothing to send until it holds a number again.
    if (price === 'blank' || price === 'invalid') {
      return null
    }
    patch.price_cents = price.cents
  }

  if (form.notes.trim() !== before.notes.trim()) {
    patch.notes = form.notes.trim()
  }

  return patch
}

/** Where each of the API's field names is shown on the form. */
const FIELD_FOR: Record<string, VisitField> = {
  customer: 'customer',
  location: 'location',
  // The pricing 400 names the input it was missing, which is the location's.
  square_feet: 'location',
  service: 'service',
  scheduled_start: 'start',
  // The form has no end input: the end is the start plus the duration.
  scheduled_end: 'duration',
  price_cents: 'price',
  assignees: 'assignees',
  notes: 'notes',
}

/**
 * A refused save, sorted into the inputs it is about.
 *
 * The messages are the server's own (ADR-023). Anything keyed to a field the
 * form does not have is shown above the form rather than dropped, so a
 * refusal is never silent.
 */
export function visitErrors (error: unknown): VisitErrors {
  const errors: VisitErrors = { fields: {}, general: [] }

  for (const [key, messages] of Object.entries(fieldErrors(error))) {
    const field = FIELD_FOR[key]
    if (field) {
      errors.fields[field] = [...(errors.fields[field] ?? []), ...messages]
    } else {
      errors.general.push(...messages)
    }
  }

  return errors
}
