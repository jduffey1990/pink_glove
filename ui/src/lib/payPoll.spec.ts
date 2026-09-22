import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { pollUntilPaid } from './payPoll'

describe('pollUntilPaid', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('stops as soon as the check says paid', async () => {
    const answers = [false, false, true]
    const check = vi.fn(async () => answers.shift() ?? true)

    const outcome = pollUntilPaid(check, { intervalMs: 1000, timeoutMs: 30_000 })
    await vi.advanceTimersByTimeAsync(2000)

    await expect(outcome).resolves.toBe('paid')
    expect(check).toHaveBeenCalledTimes(3)
  })

  it('gives up at the deadline', async () => {
    const check = vi.fn(async () => false)

    const outcome = pollUntilPaid(check, { intervalMs: 1000, timeoutMs: 5000 })
    await vi.advanceTimersByTimeAsync(6000)

    await expect(outcome).resolves.toBe('timeout')
    // Immediately, then every second up to and including the deadline; the
    // one after would land past it, so it is not made.
    expect(check).toHaveBeenCalledTimes(6)
  })

  it('treats a failed check as not yet, not as failure', async () => {
    const check = vi
      .fn<() => Promise<boolean>>()
      .mockRejectedValueOnce(new Error('blip'))
      .mockResolvedValueOnce(true)

    const outcome = pollUntilPaid(check, { intervalMs: 1000, timeoutMs: 30_000 })
    await vi.advanceTimersByTimeAsync(1000)

    await expect(outcome).resolves.toBe('paid')
  })

  it('checks once, immediately, before waiting', async () => {
    const check = vi.fn(async () => true)

    await expect(pollUntilPaid(check)).resolves.toBe('paid')
    expect(check).toHaveBeenCalledTimes(1)
  })
})
