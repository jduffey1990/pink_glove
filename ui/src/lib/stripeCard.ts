/**
 * What the "Card payments" card on the settings page shows, from the
 * organization's Stripe status and who is looking.
 *
 * The *state* is the server's word (`stripe.state`, ADR-023); this only
 * chooses the copy for it and whether to draw the button, which is a
 * courtesy -- the API decides who may connect (owner only). Pure so it has
 * a spec.
 */

import type { StripeStatus } from '@/api/types'

export type StripeCardAction = 'connect' | 'finish' | null

export interface StripeCard {
  state: StripeStatus['state']
  title: string
  text: string
  action: StripeCardAction
  actionLabel: string
  /** The tenant's own dashboard: a Standard account, so Stripe's own site. */
  dashboardUrl: string | null
}

export const STRIPE_DASHBOARD_URL = 'https://dashboard.stripe.com/'

export function stripeCard (status: StripeStatus, isOwner: boolean): StripeCard {
  const whoCan = isOwner ? '' : ' Only the organization owner can do this.'

  switch (status.state) {
    case 'enabled': {
      return {
        state: 'enabled',
        title: 'Connected',
        text: 'Customers can pay invoices online by card. Payments, refunds and payouts are in your Stripe dashboard.',
        action: null,
        actionLabel: '',
        dashboardUrl: STRIPE_DASHBOARD_URL,
      }
    }
    case 'pending': {
      return {
        state: 'pending',
        title: status.details_submitted ? 'Stripe is reviewing your details' : 'Setup not finished',
        text: status.details_submitted
          ? 'Stripe usually finishes within a day. Card payments switch on by themselves when it does.'
          : `Stripe still needs some details before card payments can be switched on.${whoCan}`,
        action: isOwner && !status.details_submitted ? 'finish' : null,
        actionLabel: 'Finish setting up with Stripe',
        dashboardUrl: STRIPE_DASHBOARD_URL,
      }
    }
    default: {
      return {
        state: 'not_connected',
        title: 'Not connected',
        text: `Connect a Stripe account to let customers pay invoices online by card. The account is yours: Stripe pays out to you and you see every payment in your own dashboard.${whoCan}`,
        action: isOwner ? 'connect' : null,
        actionLabel: 'Connect with Stripe',
        dashboardUrl: null,
      }
    }
  }
}
