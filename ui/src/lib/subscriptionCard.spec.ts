import type { PlatformBilling } from '@/api/types'
import { describe, expect, it } from 'vitest'
import { subscriptionCard } from './subscriptionCard'

function billing (overrides: Partial<PlatformBilling> = {}): PlatformBilling {
  return {
    state: 'trial',
    level: null,
    notice: null,
    summary: 'Your free trial runs until November 8, 2026.',
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

const STATES: PlatformBilling['state'][] = ['trial', 'active', 'past_due', 'lapsed', 'deactivated']

describe('subscriptionCard', () => {
  it('is absent when billing is not configured on the server', () => {
    expect(subscriptionCard(billing({ state: 'exempt', exempt: false, available_actions: [] }), true)).toBeNull()
  })

  it('says complimentary when comped by hand, with nothing to press', () => {
    const card = subscriptionCard(billing({ state: 'exempt', exempt: true, available_actions: [] }), true)
    expect(card?.title).toBe('Complimentary')
    expect(card?.action).toBeNull()
  })

  it('renders the server sentence verbatim in every state, for both roles', () => {
    for (const state of STATES) {
      for (const isOwner of [true, false]) {
        const card = subscriptionCard(billing({ state, summary: `Server says: ${state}.` }), isOwner)
        expect(card?.state).toBe(state)
        expect(card?.text).toBe(`Server says: ${state}.`)
      }
    }
  })

  it('offers the owner a subscribe button on a trial', () => {
    const card = subscriptionCard(billing(), true)
    expect(card?.action).toBe('subscribe')
    expect(card?.actionLabel).toBe('Subscribe')
    expect(card?.secondary).toBeNull()
  })

  it('offers nobody else a button, in any state', () => {
    for (const state of STATES) {
      const card = subscriptionCard(billing({ state, available_actions: ['subscribe', 'manage'] }), false)
      expect(card?.action).toBeNull()
      expect(card?.secondary).toBeNull()
    }
  })

  it('offers the portal once subscribed', () => {
    const card = subscriptionCard(
      billing({ state: 'active', status: 'active', available_actions: ['manage'] }),
      true,
    )
    expect(card?.title).toBe('Subscribed')
    expect(card?.action).toBe('manage')
    expect(card?.actionLabel).toBe('Manage billing')
  })

  it('offers subscribe first and the portal beside it to a returning customer', () => {
    const card = subscriptionCard(
      billing({ state: 'lapsed', status: 'canceled', available_actions: ['subscribe', 'manage'] }),
      true,
    )
    expect(card?.action).toBe('subscribe')
    expect(card?.secondary).toBe('manage')
    expect(card?.secondaryLabel).toBe('Billing portal')
  })

  it('draws the buttons from available_actions, never from status', () => {
    const card = subscriptionCard(
      billing({ state: 'past_due', status: 'past_due', available_actions: ['manage'] }),
      true,
    )
    expect(card?.action).toBe('manage')
    expect(card?.color).toBe('warning')
  })

  it('is read-only and red when deactivated, and the owner may still subscribe', () => {
    const card = subscriptionCard(billing({ state: 'deactivated' }), true)
    expect(card?.title).toBe('Read-only')
    expect(card?.color).toBe('error')
    expect(card?.action).toBe('subscribe')
  })
})
