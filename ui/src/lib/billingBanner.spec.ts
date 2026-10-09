import type { BillingNotice } from '@/api/types'
import { describe, expect, it } from 'vitest'
import { billingBanner } from './billingBanner'

function notice (overrides: Partial<BillingNotice> = {}): BillingNotice {
  return { state: 'trial', level: null, notice: null, ...overrides }
}

describe('billingBanner', () => {
  it('draws nothing when the server has nothing to say', () => {
    expect(billingBanner(notice(), true)).toBeNull()
    expect(billingBanner(null, true)).toBeNull()
  })

  it('draws the server sentence at the server level', () => {
    const banner = billingBanner(
      notice({ state: 'lapsed', level: 'warning', notice: 'Your free trial ended on October 8, 2026.' }),
      false,
    )
    expect(banner?.level).toBe('warning')
    expect(banner?.text).toBe('Your free trial ended on October 8, 2026.')
    expect(banner?.icon).toBe('mdi-alert-outline')
  })

  it('offers the settings link to admins and above only', () => {
    const lapsed = notice({ state: 'deactivated', level: 'error', notice: 'Read-only.' })
    expect(billingBanner(lapsed, true)?.showSettingsLink).toBe(true)
    expect(billingBanner(lapsed, false)?.showSettingsLink).toBe(false)
  })

  it('never composes a sentence of its own', () => {
    // A level with no sentence is nothing to show, not a default message.
    expect(billingBanner(notice({ state: 'lapsed', level: 'warning' }), true)).toBeNull()
  })
})
