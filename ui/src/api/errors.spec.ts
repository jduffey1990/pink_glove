import { AxiosError, type AxiosResponse } from 'axios'
import { describe, expect, it } from 'vitest'
import { bodyOf, errorDetail, fieldErrors, statusOf } from './errors'

function failure (status: number, data: unknown): AxiosError {
  const response = { status, data } as AxiosResponse
  return new AxiosError('failed', 'ERR_BAD_REQUEST', undefined, undefined, response)
}

describe('statusOf', () => {
  it('reads the status of an API failure', () => {
    expect(statusOf(failure(409, {}))).toBe(409)
  })

  it('is null for anything that is not one', () => {
    expect(statusOf(new Error('boom'))).toBeNull()
    expect(statusOf(new AxiosError('network down'))).toBeNull()
  })
})

describe('errorDetail', () => {
  it('prefers the server\'s detail', () => {
    expect(errorDetail(failure(409, { detail: 'You are already clocked in.' })))
      .toBe('You are already clocked in.')
  })

  it('falls back to the first field error', () => {
    expect(errorDetail(failure(400, { reason: ['A reason is required to mark a job cancelled.'] })))
      .toBe('A reason is required to mark a job cancelled.')
  })

  it('is null when the server said nothing usable', () => {
    expect(errorDetail(failure(500, '<html>Server Error</html>'))).toBeNull()
    expect(errorDetail(failure(400, { allowed: [] }))).toBeNull()
    expect(errorDetail(new Error('boom'))).toBeNull()
  })
})

describe('bodyOf', () => {
  it('returns the body of a conflict so its fields can be read', () => {
    expect(bodyOf<{ allowed: string[] }>(failure(409, { allowed: ['scheduled'] }))?.allowed)
      .toEqual(['scheduled'])
  })
})

describe('fieldErrors', () => {
  it('keeps every message under the field the server named', () => {
    const error = failure(400, {
      location: ['That location belongs to a different customer.'],
      scheduled_end: ['A job must end after it starts.', 'Another complaint.'],
    })

    expect(fieldErrors(error)).toEqual({
      location: ['That location belongs to a different customer.'],
      scheduled_end: ['A job must end after it starts.', 'Another complaint.'],
    })
  })

  it('wraps a bare string, which is how a 409 carries its detail', () => {
    expect(fieldErrors(failure(409, { detail: 'This job is complete.', status: 'complete' })))
      .toEqual({ detail: ['This job is complete.'], status: ['complete'] })
  })

  it('drops what is not a message', () => {
    expect(fieldErrors(failure(409, { detail: 'No.', allowed: ['scheduled'], count: 3 })))
      .toEqual({ detail: ['No.'], allowed: ['scheduled'] })
  })

  it('is empty when there is nothing to read', () => {
    expect(fieldErrors(new Error('boom'))).toEqual({})
    expect(fieldErrors(failure(500, '<html>'))).toEqual({})
  })
})
