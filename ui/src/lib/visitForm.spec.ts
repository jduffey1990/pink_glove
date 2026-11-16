/**
 * The booking form's arithmetic.
 *
 * Three things here fail silently if they are wrong, which is why they have
 * a spec and the dialog does not do them inline: a visit booked for 09:00
 * lands at 09:00 in the organization's zone whatever zone the browser is in;
 * an edit that only changes the note does not also move the visit; and a
 * blank price asks for a quote while a typo does not.
 */

import type { Job } from '@/api/types'
import { AxiosError, type AxiosResponse } from 'axios'
import { describe, expect, it } from 'vitest'
import {
  durationOf,
  isComplete,
  localErrors,
  newVisitForm,
  priceOf,
  toJobPatch,
  toNewJobRequest,
  visitErrors,
  type VisitForm,
  visitFormFromJob,
  visitSchedule,
} from './visitForm'

const DENVER = 'America/Denver'
const NEW_YORK = 'America/New_York'

function filled (overrides: Partial<VisitForm> = {}): VisitForm {
  return {
    ...newVisitForm('2027-06-10'),
    customer: 'customer-1',
    location: 'location-1',
    service: 'service-1',
    durationMinutes: 120,
    ...overrides,
  }
}

/** 09:00:30 for two hours on 10 June 2027 in Denver. The seconds are the point. */
function job (overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    customer: 'customer-1',
    location: 'location-1',
    service: 'service-1',
    scheduled_start: '2027-06-10T15:00:30Z',
    scheduled_end: '2027-06-10T17:00:30Z',
    duration_minutes: 120,
    price_cents: 15_050,
    notes: 'Side gate.',
    ...overrides,
  } as Job
}

function refusal (status: number, data: unknown): AxiosError {
  const response = { status, data } as AxiosResponse
  return new AxiosError('failed', 'ERR_BAD_REQUEST', undefined, undefined, response)
}

describe('newVisitForm', () => {
  it('starts on the day that was asked for, with nothing chosen', () => {
    const form = newVisitForm('2027-06-10')

    expect(form.date).toBe('2027-06-10')
    expect(form.customer).toBeNull()
    expect(form.price).toBe('')
    expect(isComplete(form)).toBe(false)
  })
})

describe('visitFormFromJob', () => {
  it('reads the date and time in the organization zone', () => {
    const form = visitFormFromJob(job({ scheduled_start: '2027-06-15T05:30:00Z' }), DENVER)

    // 05:30Z on the 15th is 23:30 on the 14th in Denver.
    expect(form.date).toBe('2027-06-14')
    expect(form.start).toBe('23:30')
  })

  it('reads the same job differently for an organization elsewhere', () => {
    const form = visitFormFromJob(job({ scheduled_start: '2027-06-15T05:30:00Z' }), NEW_YORK)

    expect(form.date).toBe('2027-06-15')
    expect(form.start).toBe('01:30')
  })

  it('shows the price in dollars and the length in minutes', () => {
    const form = visitFormFromJob(job(), DENVER)

    expect(form.price).toBe('150.50')
    expect(form.durationMinutes).toBe(120)
    expect(form.notes).toBe('Side gate.')
  })
})

describe('durationOf', () => {
  it.each([
    [120, 120],
    ['90', 90],
    ['', null],
    [null, null],
    ['abc', null],
  ])('%j -> %j', (typed, expected) => {
    expect(durationOf(filled({ durationMinutes: typed }))).toBe(expected)
  })
})

describe('visitSchedule', () => {
  it('books the wall-clock time in the organization zone', () => {
    expect(visitSchedule(filled({ start: '09:00' }), DENVER)).toEqual({
      scheduled_start: '2027-06-10T15:00:00.000Z',
      scheduled_end: '2027-06-10T17:00:00.000Z',
    })
  })

  it('books a different instant for an organization in another zone', () => {
    expect(visitSchedule(filled({ start: '09:00' }), NEW_YORK)?.scheduled_start)
      .toBe('2027-06-10T13:00:00.000Z')
  })

  it('runs a late visit past midnight rather than ending before it starts', () => {
    const schedule = visitSchedule(filled({ start: '23:00', durationMinutes: 120 }), DENVER)

    expect(schedule).toEqual({
      scheduled_start: '2027-06-11T05:00:00.000Z',
      scheduled_end: '2027-06-11T07:00:00.000Z',
    })
  })

  it('keeps a visit its real length across a clock change', () => {
    // 01:00 for three hours on the morning the clocks go forward.
    const schedule = visitSchedule(
      filled({ date: '2027-03-14', start: '01:00', durationMinutes: 180 }),
      DENVER,
    )

    expect(schedule).toEqual({
      scheduled_start: '2027-03-14T08:00:00.000Z',
      scheduled_end: '2027-03-14T11:00:00.000Z',
    })
  })

  it('is null until there is a date, a time and a length', () => {
    expect(visitSchedule(filled({ durationMinutes: null }), DENVER)).toBeNull()
    expect(visitSchedule(filled({ start: '' }), DENVER)).toBeNull()
    expect(visitSchedule(filled({ date: '' }), DENVER)).toBeNull()
  })

  it('passes a zero length through for the server to refuse', () => {
    const schedule = visitSchedule(filled({ durationMinutes: 0 }), DENVER)

    expect(schedule?.scheduled_end).toBe(schedule?.scheduled_start)
  })
})

describe('priceOf', () => {
  it('tells a blank from a typo', () => {
    expect(priceOf(filled({ price: '' }))).toBe('blank')
    expect(priceOf(filled({ price: ' '.repeat(3) }))).toBe('blank')
    expect(priceOf(filled({ price: 'abc' }))).toBe('invalid')
  })

  it('converts dollars to whole cents', () => {
    expect(priceOf(filled({ price: '150' }))).toEqual({ cents: 15_000 })
    expect(priceOf(filled({ price: '0.07' }))).toEqual({ cents: 7 })
    expect(priceOf(filled({ price: '0' }))).toEqual({ cents: 0 })
  })
})

describe('localErrors', () => {
  it('flags a price that is not a number', () => {
    expect(localErrors(filled({ price: 'abc' })).fields.price).toHaveLength(1)
  })

  it('says nothing about a blank or a real one', () => {
    expect(localErrors(filled({ price: '' })).fields).toEqual({})
    expect(localErrors(filled({ price: '150' })).fields).toEqual({})
  })
})

describe('toNewJobRequest', () => {
  it('builds the booking', () => {
    expect(toNewJobRequest(filled({ notes: '  Bring a ladder. ' }), DENVER)).toEqual({
      customer: 'customer-1',
      location: 'location-1',
      service: 'service-1',
      scheduled_start: '2027-06-10T15:00:00.000Z',
      scheduled_end: '2027-06-10T17:00:00.000Z',
      notes: 'Bring a ladder.',
    })
  })

  it('leaves the price out when it is blank, so the server quotes it', () => {
    expect(toNewJobRequest(filled({ price: '' }), DENVER)).not.toHaveProperty('price_cents')
  })

  it('sends a price that was typed, in cents', () => {
    expect(toNewJobRequest(filled({ price: '89.99' }), DENVER)?.price_cents).toBe(8999)
  })

  it('sends a price of nothing as zero, not as a request for a quote', () => {
    expect(toNewJobRequest(filled({ price: '0' }), DENVER)?.price_cents).toBe(0)
  })

  it('books the crew in the same request', () => {
    const request = toNewJobRequest(filled({ assignees: ['user-1', 'user-2'] }), DENVER)

    expect(request?.assignees).toEqual(['user-1', 'user-2'])
  })

  it('leaves the crew out when nobody was chosen', () => {
    expect(toNewJobRequest(filled(), DENVER)).not.toHaveProperty('assignees')
  })

  it.each([
    ['no customer', { customer: null }],
    ['no location', { location: null }],
    ['no service', { service: null }],
    ['no date', { date: '' }],
    ['no start', { start: '' }],
    ['no length', { durationMinutes: '' }],
    ['a price that is not a number', { price: 'abc' }],
  ])('is null with %s', (_, overrides) => {
    expect(toNewJobRequest(filled(overrides as Partial<VisitForm>), DENVER)).toBeNull()
  })
})

describe('toJobPatch', () => {
  const original = job()
  const before = visitFormFromJob(original, DENVER)

  function patch (changes: Partial<VisitForm>) {
    return toJobPatch(original, before, { ...before, ...changes }, DENVER)
  }

  it('sends nothing when nothing changed', () => {
    expect(patch({})).toEqual({})
  })

  it('does not move a visit whose note was the only thing edited', () => {
    // The job starts at 09:00:30. The form says 09:00. Sending the form's
    // start back would shift it thirty seconds and pin a plan visit.
    expect(patch({ notes: 'Front door.' })).toEqual({ notes: 'Front door.' })
  })

  it('sends both ends when the day changes', () => {
    expect(patch({ date: '2027-06-11' })).toEqual({
      scheduled_start: '2027-06-11T15:00:00.000Z',
      scheduled_end: '2027-06-11T17:00:00.000Z',
    })
  })

  it('sends both ends when the time changes, keeping the length', () => {
    expect(patch({ start: '13:30' })).toEqual({
      scheduled_start: '2027-06-10T19:30:00.000Z',
      scheduled_end: '2027-06-10T21:30:00.000Z',
    })
  })

  it('sends only the end for a longer visit, measured from the real start', () => {
    expect(patch({ durationMinutes: 180 })).toEqual({
      scheduled_end: '2027-06-10T18:00:30.000Z',
    })
  })

  it('takes a move and a new length together', () => {
    expect(patch({ start: '10:00', durationMinutes: 60 })).toEqual({
      scheduled_start: '2027-06-10T16:00:00.000Z',
      scheduled_end: '2027-06-10T17:00:00.000Z',
    })
  })

  it('sends a changed price in cents and leaves an unchanged one out', () => {
    expect(patch({ price: '175' })).toEqual({ price_cents: 17_500 })
    expect(patch({ price: ' 150.50 ' })).toEqual({})
  })

  it('sends a changed location or service', () => {
    expect(patch({ location: 'location-2', service: 'service-2' })).toEqual({
      location: 'location-2',
      service: 'service-2',
    })
  })

  it('never sends the customer or the crew', () => {
    const sent = patch({ customer: 'customer-2', assignees: ['user-1'] })

    expect(sent).toEqual({})
  })

  it.each([
    ['a blanked price', { price: '' }],
    ['a price that is not a number', { price: 'abc' }],
    ['a blanked length', { durationMinutes: '' }],
    ['a move with no length', { start: '10:00', durationMinutes: null }],
    ['a cleared location', { location: null }],
    ['a cleared service', { service: null }],
  ])('is null for %s, which cannot be sent', (_, changes) => {
    expect(patch(changes as Partial<VisitForm>)).toBeNull()
  })
})

describe('visitErrors', () => {
  it('puts each message under the input it is about', () => {
    const errors = visitErrors(refusal(400, {
      location: ['That location belongs to a different customer.'],
      price_cents: ['Ensure this value is greater than or equal to 0.'],
      assignees: ['One of those people is not active staff in this organization.'],
    }))

    expect(errors.fields).toEqual({
      location: ['That location belongs to a different customer.'],
      price: ['Ensure this value is greater than or equal to 0.'],
      assignees: ['One of those people is not active staff in this organization.'],
    })
    expect(errors.general).toEqual([])
  })

  it('shows an end-before-start refusal under the length, which is what sets the end', () => {
    const errors = visitErrors(refusal(400, { scheduled_end: ['A job must end after it starts.'] }))

    expect(errors.fields.duration).toEqual(['A job must end after it starts.'])
  })

  it('shows a missing square footage under the location it belongs to', () => {
    const errors = visitErrors(refusal(400, {
      square_feet: ['This service is priced per square foot and the location has none.'],
    }))

    expect(errors.fields.location).toHaveLength(1)
  })

  it('joins two refusals that land on the same input', () => {
    const errors = visitErrors(refusal(400, {
      location: ['Service location belongs to a different organization.'],
      square_feet: ['No square footage.'],
    }))

    expect(errors.fields.location).toEqual([
      'Service location belongs to a different organization.',
      'No square footage.',
    ])
  })

  it('never drops a message it has no input for', () => {
    const errors = visitErrors(refusal(400, {
      non_field_errors: ['Something about the whole form.'],
      detail: 'Something else.',
    }))

    expect(errors.fields).toEqual({})
    expect(errors.general).toEqual(['Something about the whole form.', 'Something else.'])
  })

  it('is empty for a failure with nothing to read', () => {
    expect(visitErrors(new Error('network down'))).toEqual({ fields: {}, general: [] })
  })
})
