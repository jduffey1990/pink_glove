/**
 * What the "Your pink glove subscription" card on the settings page shows,
 * from the organization's billing block and who is looking.
 *
 * The *state* and the sentence are the server's (`billing.state`,
 * `billing.notice`, ADR-023), and which buttons the API will accept is its
 * `available_actions`; this only chooses the copy around them and whether
 * to draw a button, which is a courtesy -- the API decides who may subscribe
 * (owner only). Pure so it has a spec.
 */

import type { PlatformBilling } from '@/api/types'
import { formatDayLabel, toIsoDate } from '@/lib/datetime'

export type SubscriptionAction = 'subscribe' | 'manage'

export interface SubscriptionCard {
  state: PlatformBilling['state']
  title: string
  /** The server's notice when it has one, otherwise one line of state. */
  text: string
  /** The one button to draw, and its label. */
  action: SubscriptionAction | null
  actionLabel: string
  /** A second, quieter way in: the portal beside a Subscribe button, for a returning customer. */
  secondary: SubscriptionAction | null
  icon: string
  color: string
}

const WHO_CAN = ' Only the organization owner can change this.'

function on (instant: string | null, timeZone: string): string {
  return instant ? formatDayLabel(toIsoDate(instant, timeZone)) : ''
}

/**
 * `null` means no card at all: billing is not configured on the server, so
 * there is nothing to show and nothing to ask.
 */
export function subscriptionCard (
  billing: PlatformBilling,
  isOwner: boolean,
  timeZone: string,
): SubscriptionCard | null {
  if (billing.state === 'exempt' && !billing.exempt) {
    return null
  }

  const can = (action: SubscriptionAction) => billing.available_actions.includes(action)
  const subscribe = isOwner && can('subscribe')
  const manage = isOwner && can('manage')
  const action: SubscriptionAction | null = subscribe ? 'subscribe' : (manage ? 'manage' : null)
  const secondary: SubscriptionAction | null = subscribe && manage ? 'manage' : null
  const actionLabel = action === 'subscribe' ? 'Subscribe' : (action === 'manage' ? 'Manage billing' : '')
  const whoCan = isOwner ? '' : WHO_CAN

  const base = { action, actionLabel, secondary, state: billing.state }

  switch (billing.state) {
    case 'exempt': {
      return {
        ...base,
        title: 'Complimentary',
        text: 'This organization uses pink glove at no charge.',
        icon: 'mdi-gift-outline',
        color: 'success',
      }
    }
    case 'trial': {
      return {
        ...base,
        title: 'Free trial',
        text: billing.notice
          ?? `Your free trial runs until ${on(billing.trial_ends_at, timeZone)}. Subscribe at any time to keep going after that.${whoCan}`,
        icon: 'mdi-clock-outline',
        color: 'primary',
      }
    }
    case 'active': {
      return {
        ...base,
        title: 'Subscribed',
        text: billing.notice
          ?? `Paid through ${on(billing.covered_until, timeZone)}. Your card, receipts and cancellation are in the billing portal.${whoCan}`,
        icon: 'mdi-check-circle',
        color: 'success',
      }
    }
    case 'past_due': {
      return {
        ...base,
        title: 'Payment failed',
        text: billing.notice ?? `A payment for your subscription failed.${whoCan}`,
        icon: 'mdi-alert-outline',
        color: 'warning',
      }
    }
    case 'lapsed': {
      return {
        ...base,
        title: 'Lapsed',
        text: billing.notice ?? `Your coverage has ended.${whoCan}`,
        icon: 'mdi-alert-outline',
        color: 'warning',
      }
    }
    default: {
      return {
        ...base,
        title: 'Read-only',
        text: billing.notice ?? 'This organization has been deactivated.',
        icon: 'mdi-lock-outline',
        color: 'error',
      }
    }
  }
}
