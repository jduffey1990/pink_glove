import type { BillableJob } from '@/api/types'
import { describe, expect, it } from 'vitest'
import {
  groupByCustomer,
  selectedIn,
  selectedTotal,
  toggle,
  toggleAll,
} from './billable'

function job (id: string, customer: string, amount_cents = 15_000): BillableJob {
  return {
    id,
    customer,
    customer_name: `Customer ${customer}`,
    service_name: 'Standard clean',
    description: `Standard clean -- ${id}`,
    scheduled_start: '2027-06-14T15:00:00Z',
    status: 'complete',
    amount_cents,
  } as BillableJob
}

describe('groupByCustomer', () => {
  it('puts each customer\'s visits together', () => {
    const groups = groupByCustomer([
      job('a', 'dana'),
      job('b', 'victor'),
      job('c', 'dana'),
    ])

    expect(groups).toHaveLength(2)
    expect(groups[0].id).toBe('dana')
    expect(groups[0].jobs.map(j => j.id)).toEqual(['a', 'c'])
    expect(groups[1].jobs.map(j => j.id)).toEqual(['b'])
  })

  it('keeps the order the server sent, which is the order they will bill', () => {
    const groups = groupByCustomer([job('older', 'dana'), job('newer', 'dana')])

    expect(groups[0].jobs.map(j => j.id)).toEqual(['older', 'newer'])
  })

  it('carries the customer name through', () => {
    expect(groupByCustomer([job('a', 'dana')])[0].name).toBe('Customer dana')
  })

  it('handles an empty list', () => {
    expect(groupByCustomer([])).toEqual([])
  })
})

describe('selection', () => {
  const dana = groupByCustomer([job('a', 'dana', 10_000), job('b', 'dana', 5000)])[0]

  it('reports only this group\'s ticked visits', () => {
    expect(selectedIn(dana, new Set(['a']))).toEqual(['a'])
  })

  it('ignores a tick belonging to another customer', () => {
    // The bug worth preventing: billing one customer for another's visit.
    expect(selectedIn(dana, new Set(['someone-elses-job']))).toEqual([])
    expect(selectedTotal(dana, new Set(['someone-elses-job']))).toBe(0)
  })

  it('adds up what was ticked, in cents', () => {
    expect(selectedTotal(dana, new Set(['a']))).toBe(10_000)
    expect(selectedTotal(dana, new Set(['a', 'b']))).toBe(15_000)
    expect(selectedTotal(dana, new Set())).toBe(0)
  })
})

describe('toggle', () => {
  it('ticks and unticks', () => {
    expect([...toggle(new Set(), 'a')]).toEqual(['a'])
    expect([...toggle(new Set(['a']), 'a')]).toEqual([])
  })

  it('returns a new set rather than mutating', () => {
    const before = new Set(['a'])
    const after = toggle(before, 'b')

    expect(before.has('b')).toBe(false)
    expect(after.has('b')).toBe(true)
  })
})

describe('toggleAll', () => {
  const dana = groupByCustomer([job('a', 'dana'), job('b', 'dana')])[0]

  it('ticks every visit in the group when some are off', () => {
    expect([...toggleAll(new Set(['a']), dana)].toSorted()).toEqual(['a', 'b'])
  })

  it('unticks them all when they are already on', () => {
    expect([...toggleAll(new Set(['a', 'b']), dana)]).toEqual([])
  })

  it('leaves another customer\'s selection alone', () => {
    const next = toggleAll(new Set(['theirs']), dana)

    expect(next.has('theirs')).toBe(true)
  })

  it('does nothing to an empty group', () => {
    const empty = { id: 'nobody', name: 'Nobody', jobs: [] }

    expect([...toggleAll(new Set(['a']), empty)]).toEqual(['a'])
  })
})
