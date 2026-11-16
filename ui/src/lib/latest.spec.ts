import { describe, expect, it } from 'vitest'
import { latestOnly } from './latest'

describe('latestOnly', () => {
  it('is current while nothing newer has started', () => {
    const begin = latestOnly()

    const first = begin()

    expect(first()).toBe(true)
  })

  it('retires an earlier load the moment a later one starts', () => {
    const begin = latestOnly()

    const first = begin()
    const second = begin()

    expect(first()).toBe(false)
    expect(second()).toBe(true)
  })

  it('drops a slow answer that lands after a fast one', async () => {
    const begin = latestOnly()
    const shown: string[] = []

    async function load (label: string, delayMs: number): Promise<void> {
      const isCurrent = begin()
      await new Promise(resolve => setTimeout(resolve, delayMs))
      if (isCurrent()) {
        shown.push(label)
      }
    }

    // Asked for in this order; the first is the slow one.
    await Promise.all([load('last week', 20), load('this week', 1)])

    expect(shown).toEqual(['this week'])
  })

  it('keeps separate trackers apart', () => {
    const timesheet = latestOnly()
    const money = latestOnly()

    const hours = timesheet()
    money()
    money()

    expect(hours()).toBe(true)
  })
})
