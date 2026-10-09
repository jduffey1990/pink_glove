import type { PlatformBilling } from '@/api/types'
import { describe, expect, it } from 'vitest'
import { subscriptionCard } from './subscriptionCard'

const TZ = 'America/Denver'

function billing (overrides: Partial<PlatformBilling> = {}): PlatformBilling {
  return {
    state: 'trial',
    level: null,
    notice: null,
    trial_ends_at: '2026-11-08T15:00:00Z',
    covered_until: '2026-11-08T15:00:00Z',
    grace_ends_at: '2026-11-22T15:00:00Z',
    status: '',
    cancel_at_period_end: false,
    exempt: false,
    available_actions: ['subscribe'],
    ...overrides,
  }
}

describe('subscriptionCard', () => {
  it('is absent when billing is not configured on the server', () => {
    expect(subscriptionCard(billing({ state: 'exempt', exempt: false, available_actions: [] }), true, TZ)).toBeNull()
  })

  it('says complimentary when comped by hand, with nothing to press', () => {
    const card = subscriptionCard(billing({ state: 'exempt', exempt: true, available_actions: [] }), true, TZ)
    expect(card?.title).toBe('Complimentary')
    expect(card?.action).toBeNull()
  })

  it('offers the owner a subscribe button on a trial, with the local date', () => {
    const card = subscriptionCard(billing(), true, TZ)
    expect(card?.state).toBe('trial')
    expect(card?.action).toBe('subscribe')
    expect(card?.actionLabel).toBe('Subscribe')
    expect(card?.text).toContain('8 Nov')
    expect(card?.secondary).toBeNull()
  })

  it('tells anyone else who can, and offers nothing', () => {
    const card = subscriptionCard(billing(), false, TZ)
    expect(card?.action).toBeNull()
    expect(card?.text).toContain('Only the organization owner')
  })

  it('prefers the server sentence when there is one', () => {
    const card = subscriptionCard(
      billing({ level: 'warning', notice: 'Your free trial ends on November 8, 2026. Subscribe to keep scheduling after that.' }),
      true,
      TZ,
    )
    expect(card?.text).toBe('Your free trial ends on November 8, 2026. Subscribe to keep scheduling after that.')
  })

  it('offers the portal once subscribed', () => {
    const card = subscriptionCard(
      billing({ state: 'active', status: 'active', available_actions: ['manage'] }),
      true,
      TZ,
    )
    expect(card?.title).toBe('Subscribed')
    expect(card?.action).toBe('manage')
    expect(card?.actionLabel).toBe('Manage billing')
    expect(card?.text).toContain('Paid through Sun 8 Nov')
  })

  it('offers subscribe first and the portal beside it to a returning customer', () => {
    const card = subscriptionCard(
      billing({ state: 'lapsed', status: 'canceled', available_actions: ['subscribe', 'manage'] }),
      true,
      TZ,
    )
    expect(card?.action).toBe('subscribe')
    expect(card?.secondary).toBe('manage')
  })

  it('draws the buttons from available_actions, never from status', () => {
    // The server says a live subscription may not be subscribed again, whatever the status reads.
    const card = subscriptionCard(
      billing({ state: 'past_due', status: 'past_due', available_actions: ['manage'] }),
      true,
      TZ,
    )
    expect(card?.action).toBe('manage')
    expect(card?.color).toBe('warning')
  })

  it('is read-only and red when deactivated, and the owner may still subscribe', () => {
    const card = subscriptionCard(
      billing({ state: 'deactivated', level: 'error', notice: 'This organization is read-only because its free trial ended.' }),
      true,
      TZ,
    )
    expect(card?.title).toBe('Read-only')
    expect(card?.color).toBe('error')
    expect(card?.action).toBe('subscribe')
  })
})
