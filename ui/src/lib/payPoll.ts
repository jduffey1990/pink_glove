/**
 * Wait for the webhook after a card payment.
 *
 * Stripe sends the customer back to the pay page before it sends us the
 * event, so the page polls for a short while rather than showing "unpaid"
 * to someone who just paid. Pure apart from the clock, so it has a spec.
 */

export type PollOutcome = 'paid' | 'timeout'

export interface PollOptions {
  intervalMs?: number
  timeoutMs?: number
}

const DEFAULTS: Required<PollOptions> = { intervalMs: 2000, timeoutMs: 30_000 }

function sleep (ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/**
 * Call `check` until it answers true or the time is up. The first check is
 * immediate; a check that throws counts as "not yet", because a blip while
 * polling is not a failed payment.
 */
export async function pollUntilPaid (
  check: () => Promise<boolean>,
  options: PollOptions = {},
): Promise<PollOutcome> {
  const { intervalMs, timeoutMs } = { ...DEFAULTS, ...options }
  const deadline = Date.now() + timeoutMs

  for (;;) {
    if (await check().catch(() => false)) {
      return 'paid'
    }
    if (Date.now() + intervalMs > deadline) {
      return 'timeout'
    }
    await sleep(intervalMs)
  }
}
