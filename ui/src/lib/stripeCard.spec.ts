import type { StripeStatus } from '@/api/types'
import { describe, expect, it } from 'vitest'
import { stripeCard } from './stripeCard'

function status (overrides: Partial<StripeStatus> = {}): StripeStatus {
  return {
    state: 'not_connected',
    connected: false,
    charges_enabled: false,
    details_submitted: false,
    connected_at: null,
    ...overrides,
  }
}

describe('stripeCard', () => {
  it('offers the owner a connect button when nothing is connected', () => {
    const card = stripeCard(status(), true)
    expect(card.state).toBe('not_connected')
    expect(card.action).toBe('connect')
    expect(card.dashboardUrl).toBeNull()
  })

  it('tells anyone else who can, and offers nothing', () => {
    const card = stripeCard(status(), false)
    expect(card.action).toBeNull()
    expect(card.text).toContain('Only the organization owner')
  })

  it('offers to finish an unfinished setup', () => {
    const card = stripeCard(status({ state: 'pending', connected: true }), true)
    expect(card.state).toBe('pending')
    expect(card.action).toBe('finish')
    expect(card.title).toBe('Setup not finished')
  })

  it('waits on Stripe once the details are in, with nothing to press', () => {
    const card = stripeCard(
      status({ state: 'pending', connected: true, details_submitted: true }),
      true,
    )
    expect(card.state).toBe('pending')
    expect(card.action).toBeNull()
    expect(card.title).toContain('reviewing')
  })

  it('is connected once charges are enabled, whoever is looking', () => {
    for (const isOwner of [true, false]) {
      const card = stripeCard(
        status({ state: 'enabled', connected: true, details_submitted: true, charges_enabled: true }),
        isOwner,
      )
      expect(card.state).toBe('enabled')
      expect(card.action).toBeNull()
      expect(card.dashboardUrl).toMatch(/^https:\/\/dashboard\.stripe\.com/)
    }
  })

  it('takes the state from the server, not from the flags', () => {
    // The server says enabled; the flags disagree. The server wins (ADR-023).
    const card = stripeCard(status({ state: 'enabled' }), true)
    expect(card.state).toBe('enabled')
  })
})
