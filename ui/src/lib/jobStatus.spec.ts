import { describe, expect, it } from 'vitest'
import { canMoveTo, JOB_STATUS_OPTIONS, statusLabel } from './jobStatus'

describe('job status presentation', () => {
  it('labels a status', () => {
    expect(statusLabel('no_access')).toBe('No access')
  })

  it('offers every status once, in workflow order', () => {
    expect(JOB_STATUS_OPTIONS.map(option => option.value)).toEqual([
      'scheduled',
      'en_route',
      'in_progress',
      'complete',
      'cancelled',
      'no_access',
    ])
  })
})

describe('canMoveTo', () => {
  it("answers from the server's list, not from the current status", () => {
    // A cleaner on a scheduled job: the machine allows cancelling, the server
    // does not offer it to them, so neither does the page.
    const job = {
      next_statuses: [
        { status: 'en_route' as const, reason_required: false },
        { status: 'no_access' as const, reason_required: true },
      ],
    }

    expect(canMoveTo(job, 'en_route')).toBe(true)
    expect(canMoveTo(job, 'cancelled')).toBe(false)
  })

  it('offers nothing on a finished job', () => {
    expect(canMoveTo({ next_statuses: [] }, 'no_access')).toBe(false)
  })
})
